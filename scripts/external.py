"""Portable external API monitor. Standard library only; no credentials on disk."""
import argparse
import csv
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import http.client
import io
import json
import math
from pathlib import Path
import re
import secrets
import socket
import ssl
import threading
import time
import urllib.parse
import uuid
import zipfile

BASE=Path(__file__).resolve().parents[1]
TOKEN=secrets.token_hex(24)
PORT=18706
CURRENT=None
CONTROL=threading.Lock()
FIELDS=['request_id','scheduled_utc','sent_utc','completed_utc','scheduled_offset','sent_offset','complete_offset','send_lag_ms','elapsed_ms','http_status','result','error','rps','send_phase','complete_phase']

def decode_json(text):
    def invalid(value): raise ValueError('non-finite JSON number')
    return json.loads(text,parse_int=Decimal,parse_float=Decimal,parse_constant=invalid)

def json_value(value,path):
    position=0
    while position<len(path):
        if path[position]=='[':
            match=re.match(r'\[(\d+)\]',path[position:])
            if not match or not isinstance(value,list): raise KeyError(path)
            value=value[int(match[1])];position+=len(match[0])
        else:
            if position:
                if path[position]!='.': raise KeyError(path)
                position+=1
            match=re.match(r'[^.\[\]]+',path[position:])
            if not match or not isinstance(value,dict): raise KeyError(path)
            value=value[match[0]];position+=len(match[0])
    return value

def equal_json(actual,expected):
    return type(actual) is type(expected) and actual==expected

def exchange(settings,context=None):
    started=time.perf_counter(); deadline=started+settings.get('timeoutMs',5000)/1000
    parsed=urllib.parse.urlsplit(settings['url']); port=parsed.port or (443 if parsed.scheme=='https' else 80)
    proxy=settings.get('proxyHost','').strip(); proxy_port=int(settings.get('proxyPort',8080))
    timeout=max(.01,deadline-time.perf_counter())
    if parsed.scheme=='https':
        context=context or ssl.create_default_context(cafile=settings.get('caFile') or None)
        connection=http.client.HTTPSConnection(proxy or parsed.hostname,proxy_port if proxy else port,timeout=timeout,context=context)
        if proxy: connection.set_tunnel(parsed.hostname,port)
    else: connection=http.client.HTTPConnection(proxy or parsed.hostname,proxy_port if proxy else port,timeout=timeout)
    sockets=[]; expired=threading.Event()
    def expire():
        expired.set()
        for sock in [connection.sock,*sockets]:
            if sock:
                try: sock.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                try: sock.close()
                except OSError: pass
    timer=threading.Timer(max(.001,deadline-time.perf_counter()),expire);timer.daemon=True;timer.start()
    status=None
    try:
        connection.connect()
        if expired.is_set(): raise TimeoutError()
        sockets.append(connection.sock)
        connection.sock.settimeout(max(.001,deadline-time.perf_counter()))
        target=urllib.parse.urlunsplit(('', '', parsed.path or '/',parsed.query,''))
        if proxy and parsed.scheme=='http': target=urllib.parse.urlunsplit((parsed.scheme,parsed.netloc,parsed.path or '/',parsed.query,''))
        body=settings.get('body','').encode('utf-8')
        connection.request(settings.get('method','GET'),target,body=body if body else None,headers=settings.get('headers',{}))
        response=connection.getresponse();status=response.status
        data=bytearray()
        while True:
            chunk=response.read(8192)
            if expired.is_set() or time.perf_counter()>deadline: raise TimeoutError()
            if not chunk: break
            data.extend(chunk)
            if len(data)>256*1024: raise ValueError('response_too_large')
        if response.length not in (None,0): raise ValueError('incomplete_body')
        if status!=settings.get('expectedStatus',200): return status,False,'http_status'
        path=settings.get('jsonPath','').strip()
        if path:
            try: actual=json_value(decode_json(data.decode('utf-8')),path)
            except (ValueError,UnicodeError): return status,False,'invalid_json'
            except (KeyError,IndexError,TypeError): return status,False,'json_path'
            if not equal_json(actual,decode_json(settings.get('expectedValue','null'))): return status,False,'json_value'
        return status,True,''
    except Exception as e:
        reason='timeout' if expired.is_set() or isinstance(e,(TimeoutError,socket.timeout)) else str(e) if isinstance(e,ValueError) and str(e) in ('response_too_large','incomplete_body') else 'tls_error' if isinstance(e,ssl.SSLError) else 'connection_error'
        return status,False,reason
    finally:
        timer.cancel();connection.close()

class Run:
    def __init__(self,settings,output=None):
        self.settings=settings;self.id=str(uuid.uuid4());self.folder=Path(output or BASE/'results'/'external')/self.id;self.folder.mkdir(parents=True)
        self.ssl_context=ssl.create_default_context(cafile=settings.get('caFile') or None) if settings['url'].startswith('https:') else None
        self.lock=threading.RLock();self.condition=threading.Condition(self.lock);self.stopping=False;self.done=False;self.started=time.perf_counter();self.utc=datetime.now(timezone.utc)
        self.finished=None
        self.rps=float(settings.get('rps',1));self.generation=0;self.inflight=0;self.counts={k:0 for k in ('sent','normal','delayed','failed','unsent')};self.times=[];self.events=[];self.begin=None;self.end=None
        self.phase_counts={p:{k:0 for k in ('sent','normal','delayed','failed','unsent')} for p in ('before','during','after')}
        self.csv=self.folder.joinpath('requests.csv').open('w',newline='',encoding='utf-8-sig');self.writer=csv.DictWriter(self.csv,fieldnames=FIELDS);self.writer.writeheader()
        self.event('start',rps=self.rps)
        safe={k:settings.get(k) for k in ('rps','duration','timeoutMs','thresholdMs','maxInflight','expectedStatus')}
        (self.folder/'settings.json').write_text(json.dumps(safe,indent=2))
        self.thread=threading.Thread(target=self.schedule,daemon=False)
    def event(self,name,**extra):
        with self.lock: self.events.append({'event':name,'offset':time.perf_counter()-self.started,**extra})
    def phase(self,offset):
        return 'before' if self.begin is None or offset<self.begin else 'during' if self.end is None or offset<self.end else 'after'
    def timestamp(self,offset): return (self.utc+timedelta(seconds=offset)).isoformat() if offset is not None else ''
    def mark(self,start):
        with self.lock:
            now=time.perf_counter()-self.started
            if start:
                if self.begin is not None: raise ValueError('one deployment interval per run')
                self.begin=now;self.event('deploy_start')
            else:
                if self.begin is None or self.end is not None: raise ValueError('no open deployment interval')
                self.end=now;self.event('deploy_end')
    def change_rps(self,value):
        if not .1<=value<=200: raise ValueError('RPS must be .1..200')
        with self.condition: self.rps=value;self.generation+=1;self.event('rps_change',rps=value);self.condition.notify_all()
    def stop(self):
        with self.condition: self.stopping=True;self.condition.notify_all()
    def record(self,target,sent,completed,status,result,error,rps):
        elapsed=(completed-sent)*1000 if sent is not None else None
        phase=self.phase(sent if sent is not None else target)
        row=dict(zip(FIELDS,[str(uuid.uuid4()),self.timestamp(target),self.timestamp(sent),self.timestamp(completed),target,sent,completed,(sent-target)*1000 if sent is not None else None,elapsed,status,result,error,rps,phase,self.phase(completed)]))
        with self.lock:
            self.writer.writerow(row);self.counts[result]+=1;self.phase_counts[phase][result]+=1
            if sent is not None: self.phase_counts[phase]['sent']+=1
            if result in ('normal','delayed'): self.times.append(elapsed)
    def one(self,target,rps):
        sent=time.perf_counter()-self.started
        try:
            status,valid,error=exchange(self.settings,self.ssl_context);end=time.perf_counter()-self.started
            result='failed' if not valid else 'normal' if (end-sent)*1000<=self.settings.get('thresholdMs',300) else 'delayed'
            self.record(target,sent,end,status,result,error,rps)
        finally:
            with self.lock: self.inflight-=1
    def schedule(self):
        limit=int(self.settings.get('maxInflight',64));duration=float(self.settings.get('duration',60));next_time=self.started;generation=0
        try:
            with ThreadPoolExecutor(max_workers=limit) as executor:
                while True:
                    with self.condition:
                        if self.stopping: break
                        now=time.perf_counter()
                        if self.generation!=generation: generation=self.generation;next_time=now+1/self.rps
                        if next_time>=self.started+duration:
                            remaining=self.started+duration-now
                            if remaining>0:self.condition.wait(remaining);continue
                            break
                        if now<next_time: self.condition.wait(next_time-now);continue
                        target=next_time-self.started;rps=self.rps;next_time+=1/rps
                        if now-self.started>=duration or now-(self.started+target)>.1:
                            self.record(target,None,now-self.started,None,'unsent','schedule_lag',rps);continue
                        if self.inflight>=limit:
                            self.record(target,None,now-self.started,None,'unsent','inflight_limit',rps);continue
                        self.inflight+=1;self.counts['sent']+=1
                        executor.submit(self.one,target,rps)
                self.event('send_stop')
        finally:
            with self.lock:
                self.event('collection_end');self.finished=time.perf_counter();self.done=True;self.csv.flush();self.csv.close()
                summary=self.snapshot();self.folder.joinpath('summary.json').write_text(json.dumps(summary,indent=2))
                with self.folder.joinpath('events.csv').open('w',newline='',encoding='utf-8-sig') as f:
                    writer=csv.DictWriter(f,fieldnames=['event','offset','rps']);writer.writeheader();writer.writerows(self.events)
                self.settings={} # Drop URL, headers and body after collection.
    def snapshot(self):
        with self.lock:
            times=sorted(self.times)
            result={'id':self.id,'done':self.done,'elapsed':(self.finished or time.perf_counter())-self.started,'rps':self.rps,'inflight':self.inflight,**self.counts,
                    'loadValid':self.counts['unsent']==0,'phases':{p:dict(values) for p,values in self.phase_counts.items()},'events':list(self.events),
                    'p95_ms':times[max(0,math.ceil(len(times)*.95)-1)] if times else None,'p99_ms':times[max(0,math.ceil(len(times)*.99)-1)] if times else None}
            return result

def validate(data):
    if not isinstance(data,dict) or not isinstance(data.get('url'),str):raise ValueError('URL required')
    if any(not isinstance(data.get(k,''),str) for k in ('body','jsonPath','expectedValue','proxyHost','caFile')):raise ValueError('text field required')
    parsed=urllib.parse.urlsplit(data['url'])
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or len(data['url'])>8192: raise ValueError('HTTP(S) URL required; put credentials in headers')
    if data.get('method','GET') not in ('GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS'): raise ValueError('invalid method')
    for k,default,low,high in [('rps',1,.1,200),('duration',60,1,3600),('timeoutMs',5000,100,60000),('thresholdMs',300,1,60000),('maxInflight',64,1,256),('expectedStatus',200,100,599)]:
        value=float(data.get(k,default))
        if not math.isfinite(value) or not low<=value<=high: raise ValueError(k+' out of range')
        if k not in ('rps','duration') and not value.is_integer():raise ValueError(k+' must be an integer')
        data[k]=value if k in ('rps','duration') else int(value)
    if data.get('proxyHost') and not 1<=int(data.get('proxyPort',8080))<=65535:raise ValueError('invalid proxy port')
    headers=data.get('headers',{})
    if not isinstance(headers,dict) or not all(isinstance(k,str) and isinstance(v,str) and '\r' not in k+v and '\n' not in k+v for k,v in headers.items()): raise ValueError('invalid headers')
    if len(data.get('body','').encode('utf-8'))>256*1024 or len(json.dumps(headers).encode())>256*1024: raise ValueError('request too large')
    if data.get('jsonPath'): decode_json(data.get('expectedValue','null'))
    return data

class Handler(BaseHTTPRequestHandler):
    def reply(self,value,status=200,kind='application/json'):
        body=(json.dumps(value,ensure_ascii=False) if not isinstance(value,str) else value).encode('utf-8')
        self.send_response(status);self.send_header('Content-Type',kind+'; charset=utf-8');self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body)
    def allowed(self): return self.headers.get('Host')==f'127.0.0.1:{PORT}' and self.headers.get('Origin',f'http://127.0.0.1:{PORT}')==f'http://127.0.0.1:{PORT}'
    def do_GET(self):
        if not self.allowed(): self.reply({},403);return
        if self.path=='/': self.reply((BASE/'web/external.html').read_text(encoding='utf-8').replace('__TOKEN__',TOKEN),kind='text/html');return
        if self.path=='/state': self.reply(CURRENT.snapshot() if CURRENT else {'done':True});return
        if self.path.startswith('/download?') and CURRENT and CURRENT.done:
            name=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('file',[''])[0]
            if name=='results.zip':
                path=CURRENT.folder/name
                if not path.exists():
                    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
                        for n in ('requests.csv','events.csv','summary.json','settings.json'):z.write(CURRENT.folder/n,n)
            elif name in ('requests.csv','events.csv','summary.json'):path=CURRENT.folder/name
            else: self.reply({},400);return
            self.send_response(200);self.send_header('Content-Type','application/octet-stream');self.send_header('Content-Disposition','attachment; filename="'+name+'"');self.send_header('Content-Length',str(path.stat().st_size));self.end_headers()
            with path.open('rb') as f:
                while chunk:=f.read(65536):self.wfile.write(chunk)
            return
        self.reply({},404)
    def do_POST(self):
        global CURRENT
        if not self.allowed() or self.headers.get('X-PoC-Token')!=TOKEN: self.reply({},403);return
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=1024*1024: raise ValueError('request too large')
            data=json.loads(self.rfile.read(size))
            with CONTROL:
                if self.path=='/start':
                    if CURRENT and not CURRENT.done: raise ValueError('measurement already running')
                    CURRENT=Run(validate(data));CURRENT.thread.start()
                elif CURRENT and not CURRENT.done:
                    if self.path=='/stop':CURRENT.stop()
                    elif self.path=='/rps':CURRENT.change_rps(float(data['rps']))
                    elif self.path=='/mark-start':CURRENT.mark(True)
                    elif self.path=='/mark-end':CURRENT.mark(False)
                    else:raise ValueError('unknown action')
                else:raise ValueError('no active measurement')
            self.reply({'accepted':True})
        except (ValueError,KeyError,TypeError,OSError): self.reply({'error':'입력 범위·URL·헤더·현재 실행 상태를 확인하세요.'},400)
    def log_message(self,*args):pass

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=18706);PORT=p.parse_args().port
    print(f'http://127.0.0.1:{PORT}/',flush=True)
    try:ThreadingHTTPServer(('127.0.0.1',PORT),Handler).serve_forever()
    except KeyboardInterrupt:
        if CURRENT and not CURRENT.done:CURRENT.stop();CURRENT.thread.join(timeout=65)
