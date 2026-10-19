"""Loopback-only control console. One active operation; bounded commands."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import poc

TOKEN=secrets.token_hex(24)
LOCK=threading.Lock()
STATE={'busy':False,'operation':'','exitCode':None,'log':''}
PORT=18700
FIXED=None
STATE_DIR=poc.RUNTIME
MEASURE_LOCK=threading.Lock()
EVENT_LOCK=threading.Lock()
BACKEND_LOCK=threading.Lock()
BACKEND_CACHE={'at':0,'data':None}
STATE['events']=[]

def record(label):
    with EVENT_LOCK:
        STATE['events']=(STATE['events']+[{'time':time.time(),'label':label}])[-100:]

def node_snapshot():
    if not BACKEND_LOCK.acquire(blocking=False):return BACKEND_CACHE['data'] or {'error':'조회 중'}
    try:
        if time.monotonic()-BACKEND_CACHE['at']<1:return BACKEND_CACHE['data']
        if not FIXED:
            pods=json.loads(poc.kubectl('get','pods','-l','app=demo-api','-o','json',capture=True,timeout=5))['items']
            deploy=json.loads(poc.kubectl('get','deployment','demo-api','-o','json',capture=True,timeout=5))
            data={'pods':[{'name':p['metadata']['name'],'uid':p['metadata']['uid'],
              'ready':any(c['type']=='Ready' and c['status']=='True' for c in p.get('status',{}).get('conditions',[])),
              'version':next((e.get('value') for e in p['spec']['containers'][0].get('env',[]) if e['name']=='POC_VERSION'),None),
              'phase':p.get('status',{}).get('phase','Pending'),'terminating':bool(p['metadata'].get('deletionTimestamp'))} for p in pods],
              'replicas':deploy['spec'].get('replicas',0),
              'targetVersion':next((e.get('value') for e in deploy['spec']['template']['spec']['containers'][0].get('env',[]) if e['name']=='POC_VERSION'),None)}
        else:
            import variants,urllib.request
            from concurrent.futures import ThreadPoolExecutor
            def health(name):
                port=variants.MODES[FIXED][2]+variants.members(FIXED).index(name)
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/health/ready',timeout=1) as response:return name,json.load(response)
                except OSError:return name,{'ready':False}
            with ThreadPoolExecutor(max_workers=4) as pool:data={'tomcat':dict(pool.map(health,variants.members(FIXED)))}
            if FIXED not in ('http','ajp'):data.update(apache1=variants.status(FIXED,1),apache2=variants.status(FIXED,2))
        data['observedAt']=time.time();BACKEND_CACHE.update(at=time.monotonic(),data=data);return data
    finally:BACKEND_LOCK.release()

def pod_control(action,value):
    try:
        STATE.update(busy=True,operation=action,exitCode=None)
        if action=='scale':poc.kubectl('scale','deployment/demo-api','--replicas='+str(value),timeout=15)
        else:
            pod=json.loads(poc.kubectl('get','pod',value,'-o','json',capture=True,timeout=5))
            if pod['metadata'].get('labels',{}).get('app')!='demo-api':raise ValueError('unexpected pod')
            poc.kubectl('delete','pod',value,'--wait=false',timeout=15)
        STATE['exitCode']=0;record(action+' 명령 반영 · '+str(value))
    except Exception as e:STATE['exitCode']=-1;record(action+' 실패 · '+type(e).__name__)
    finally:STATE['busy']=False;LOCK.release()


def marker(event):
    if MEASURE_LOCK.locked():
        with (STATE_DIR/'live'/'markers.jsonl').open('a') as f: f.write(json.dumps({'event':event,'absolute':time.perf_counter()})+'\n')

def measurement(manual,version,rps=100):
    record('API 송신 시작 · '+str(rps)+' RPS'+(' · 자동 배포 관측' if not manual else ' · 최대 330초'))
    try:
        live=STATE_DIR/'live'; live.mkdir(exist_ok=True)
        (live/'control.json').write_text('{"stop":false}')
        (live/'markers.jsonl').write_text('')
        if FIXED:
            args=['scripts/variants.py','measure','--mode',FIXED,'--version',version,'--rps',str(rps)]+(['--manual'] if manual else [])
        else:
            args=['scripts/load.py','--rps',str(rps),'--version',version,'--control-dir',str(live)]+(['--seconds','330'] if manual else ['--deploy'])
        with (STATE_DIR/'measurement.log').open('w',encoding='utf-8') as f:
            result=subprocess.run([sys.executable,*args],cwd=poc.ROOT,stdout=f,stderr=subprocess.STDOUT,timeout=600)
        STATE['measurementExitCode']=result.returncode
        record('API 관측 완료 · CSV/요약 저장' if result.returncode==0 else 'API 관측 실패 · 로그 확인')
    except Exception as e:
        STATE['measurementExitCode']=-1;record('측정 실패 · '+type(e).__name__)
    finally:
        MEASURE_LOCK.release()
        if not manual: LOCK.release()

def execute(args,label):
    try:
        STATE.update(busy=True,operation=label,exitCode=None,log='')
        if label=='roll': marker('deploy_start')
        with (STATE_DIR/'dashboard.log').open('w',encoding='utf-8') as log:
            result=subprocess.run([sys.executable,*args],cwd=poc.ROOT,stdout=log,stderr=subprocess.STDOUT,timeout=1200)
        STATE['exitCode']=result.returncode
        record(label+(' 완료' if result.returncode==0 else ' 실패 · 로그 확인'))
        if label=='roll': marker('deploy_end' if result.returncode==0 else 'deploy_failed')
    except Exception as e: STATE['log']=type(e).__name__; STATE['exitCode']=-1;record(label+' 실패 · '+type(e).__name__)
    finally: STATE['busy']=False; LOCK.release()

class Handler(BaseHTTPRequestHandler):
    def send(self,data,kind='application/json',status=200):
        encoded=data.encode('utf-8'); self.send_response(status); self.send_header('Content-Type',kind+'; charset=utf-8')
        self.send_header('Content-Length',str(len(encoded))); self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff'); self.end_headers(); self.wfile.write(encoded)
    def do_GET(self):
        if self.headers.get('Host')!=f'127.0.0.1:{PORT}': self.send('{}',status=403); return
        if self.path=='/':
            page=(poc.ROOT/'web/index.html').read_text(encoding='utf-8').replace('__TOKEN__',TOKEN)
            page=page.replace('__CONFIG__',json.dumps({'mode':FIXED or 'k3s','port':PORT}))
            self.send(page,'text/html'); return
        if self.path=='/state':
            data=dict(STATE)
            file=STATE_DIR/'dashboard.log'
            if file.exists():
                with file.open('rb') as stream:
                    stream.seek(max(0,file.stat().st_size-6000)); data['log']=stream.read().decode('utf-8',errors='replace')
            measurement_log=STATE_DIR/'measurement.log'
            if measurement_log.exists():
                with measurement_log.open('rb') as stream:
                    stream.seek(max(0,measurement_log.stat().st_size-3000));data['log']+='\n[측정 로그]\n'+stream.read().decode('utf-8',errors='replace')
            data['mode']=FIXED or ((poc.RUNTIME/'mode').read_text() if (poc.RUNTIME/'mode').exists() else '미기동')
            data['measureBusy']=MEASURE_LOCK.locked();data['serverTime']=time.time()
            experiment=poc.RUNTIME/'comparison-lock.json'
            if experiment.exists():
                try:
                    data['experiment']=json.loads(experiment.read_text(encoding='utf-8'))
                    if data['experiment'].get('mode')==data['mode']:
                        data['busy']=True;data['operation']='10회 비교 실험'
                        data['measureBusy']=data['experiment'].get('phase')=='measurement'
                except (OSError,ValueError):pass
            stage_history=STATE_DIR/'deployment-events.jsonl'
            if stage_history.exists():
                with stage_history.open('rb') as stream:
                    stream.seek(max(0,stage_history.stat().st_size-16000));lines=stream.read().decode('utf-8',errors='replace').splitlines()
                for line in lines[-30:]:
                    try:
                        e=json.loads(line);data['events']=data['events']+[{'time':e['time'],'label':e['label']}]
                    except (ValueError,KeyError):pass
            deployment=STATE_DIR/'deployment.json'
            if deployment.exists():
                try:data['deployment']=json.loads(deployment.read_text())
                except (OSError,ValueError):pass
            live=STATE_DIR/'live'/'live.json'
            if live.exists():
                try:data['live']=json.loads(live.read_text())
                except (OSError,ValueError):data['live']=None
            self.send(json.dumps(data,ensure_ascii=False)); return
        if self.path in ('/live.js','/live.css'):
            self.send((poc.ROOT/'web'/self.path[1:]).read_text(encoding='utf-8'),'text/javascript' if self.path.endswith('.js') else 'text/css');return
        if self.path=='/backends':
            try:self.send(json.dumps(node_snapshot()))
            except Exception:self.send(json.dumps({'error':'상태 조회 실패 · Docker 및 실행 상태 확인','observedAt':time.time()}))
            return
        if self.path.startswith('/download?'):
            name=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('file',[''])[0]
            if name not in ('requests.csv','summary.json','events.json','resources.jsonl'): self.send('{}',status=400); return
            pointer=STATE_DIR/'live'/'result-path'
            if MEASURE_LOCK.locked() or not pointer.exists(): self.send('{}',status=409); return
            folder=Path(pointer.read_text()).resolve()
            if not folder.is_relative_to((poc.ROOT/'results').resolve()): self.send('{}',status=403); return
            file=folder/name
            if not file.exists(): self.send('{}',status=404); return
            body=file.read_bytes(); self.send_response(200);self.send_header('Content-Type','application/octet-stream')
            self.send_header('Content-Disposition','attachment; filename="'+name+'"');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
        self.send('{}',status=404)
    def do_POST(self):
        if self.path!='/action' or self.headers.get('Host')!=f'127.0.0.1:{PORT}' or self.headers.get('X-PoC-Token')!=TOKEN or self.headers.get('Origin',f'http://127.0.0.1:{PORT}')!=f'http://127.0.0.1:{PORT}':
            self.send('{}',status=403); return
        if (poc.RUNTIME/'comparison-lock.json').exists():self.send('{"error":"comparison in progress"}',status=409);return
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=4096: raise ValueError()
            data=json.loads(self.rfile.read(size)); action=data['action']
            mode=data.get('mode','legacy'); version=data.get('version','v2')
            rps=data.get('rps',100)
            if type(rps) not in (int,float) or not .1<=rps<=200:raise ValueError()
            if mode not in ('legacy','k3s','http','ajp','bluegreen','rolling') or version not in ('v1','v2'): raise ValueError()
            if FIXED and mode!=FIXED: raise ValueError()
            if not FIXED and mode!='k3s':raise ValueError()
            if MEASURE_LOCK.locked() and action in ('start','stop'):self.send('{"error":"stop measurement first"}',status=409);return
            if action in ('scale','pod-replace'):
                if FIXED:raise ValueError()
                value=data.get('replicas') if action=='scale' else data.get('pod','')
                if action=='scale' and (type(value) is not int or value not in (0,1,2)):raise ValueError()
                if action=='pod-replace':
                    import re
                    if not isinstance(value,str) or not re.fullmatch(r'demo-api-[a-z0-9-]{1,100}',value):raise ValueError()
                if not LOCK.acquire(blocking=False):self.send('{}',status=409);return
                record(action+' 요청 · '+str(value))
                threading.Thread(target=pod_control,args=(action,value),daemon=True).start();self.send('{"accepted":true}',status=202);return
            if action=='send-stop':
                live=STATE_DIR/'live'; live.mkdir(exist_ok=True)
                temp=live/'control.tmp';temp.write_text('{"stop":true}');temp.replace(live/'control.json')
                record('API 송신 종료 요청 · 진행 중 응답 수집');self.send('{"accepted":true}');return
            if action in ('mark-start','mark-end'):
                record('배포 시작 표시' if action=='mark-start' else '배포 종료 표시');marker('deploy_start' if action=='mark-start' else 'deploy_end');self.send('{"accepted":true}');return
            if action in ('send-start','measure'):
                if not MEASURE_LOCK.acquire(blocking=False): self.send('{"error":"measurement running"}',status=409);return
                if action=='measure' and not LOCK.acquire(blocking=False): MEASURE_LOCK.release();self.send('{}',status=409);return
                threading.Thread(target=measurement,args=(action=='send-start',version,rps),daemon=True).start()
                self.send('{"accepted":true}',status=202);return
            if action in ('start','roll','stop','status'):
                args=['scripts/variants.py' if FIXED else 'scripts/poc.py',action,'--mode',mode,'--version',version]
            elif action=='node' and FIXED:
                node=data.get('node');operation=data.get('nodeAction')
                if node not in ('tomcat1','tomcat2','tomcat3','tomcat4') or operation not in ('start','stop','exclude','include'): raise ValueError()
                args=['scripts/variants.py','node','--mode',mode,'--node',node,'--node-action',operation]
            else: raise ValueError()
        except (ValueError,KeyError,TypeError): self.send('{}',status=400); return
        if not LOCK.acquire(blocking=False): self.send('{"error":"busy"}',status=409); return
        label=(data.get('node','')+' '+data.get('nodeAction','')) if action=='node' else action
        record(label+' 요청 · '+version)
        threading.Thread(target=execute,args=(args,label),daemon=True).start()
        self.send('{"accepted":true}',status=202)
    def log_message(self,*args): pass

if __name__=='__main__':
    import argparse,variants
    parser=argparse.ArgumentParser();parser.add_argument('--mode',choices=list(variants.MODES));args=parser.parse_args()
    FIXED=args.mode
    if FIXED: PORT=variants.MODES[FIXED][0];STATE_DIR=variants.directory(FIXED)
    STATE_DIR.mkdir(parents=True,exist_ok=True)
    print(f'http://127.0.0.1:{PORT}/',flush=True)
    ThreadingHTTPServer(('127.0.0.1',PORT),Handler).serve_forever()
