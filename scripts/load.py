"""Fixed arrival-rate local HTTP/1.1 measurements. No client retries."""
import argparse
from collections import deque
import asyncio
import csv
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import time
import uuid
import poc

LIMIT=3100
MAX_BODY=256*1024

def invalid_json_number(value):raise ValueError('non-finite JSON number')

async def wait_until(deadline):
    while True:
        remaining=deadline-time.perf_counter()
        if remaining<=0:return
        await asyncio.sleep(remaining)

async def request(port, key, request_id, timeout=30):
    async def exchange():
        reader,writer=await asyncio.open_connection('127.0.0.1',port)
        try:
            writer.write((f'GET /api/companies/1 HTTP/1.1\r\nHost: localhost\r\nX-API-Key: {key}\r\nX-Request-ID: {request_id}\r\nConnection: close\r\n\r\n').encode())
            await writer.drain()
            status_line=await reader.readline()
            status=int(status_line.split()[1])
            headers={}
            for _ in range(128):
                line=await reader.readline()
                if line==b'\r\n': break
                if not line: raise ValueError('incomplete_headers')
                k,v=line.decode('iso-8859-1').split(':',1); headers[k.lower()]=v.strip()
            else: raise ValueError('too_many_headers')
            body=b''
            if headers.get('transfer-encoding','').lower()=='chunked':
                while True:
                    size=int((await reader.readline()).split(b';')[0],16)
                    if not size: break
                    if len(body)+size>MAX_BODY: raise ValueError('response_too_large')
                    body+=await reader.readexactly(size)
                    if await reader.readexactly(2)!=b'\r\n': raise ValueError('invalid_chunk')
            elif 'content-length' in headers:
                size=int(headers['content-length'])
                if size<0 or size>MAX_BODY: raise ValueError('response_too_large')
                body=await reader.readexactly(size)
            else:
                while True:
                    chunk=await reader.read(8192)
                    if not chunk: break
                    body+=chunk
                    if len(body)>MAX_BODY: raise ValueError('response_too_large')
            if status!=200:return status,False,'','','http_status'
            try:data=json.loads(body,parse_constant=invalid_json_number)
            except (ValueError,UnicodeError):return status,False,'','','invalid_json'
            if not isinstance(data,dict):return status,False,'','','invalid_response'
            company=data.get('company')
            valid=(isinstance(company,dict) and type(company.get('id')) is int and company['id']==1 and data.get('version') in ('v1','v2') and isinstance(data.get('instance'),str) and type(data.get('serverProcessingMs')) in (int,float))
            return status,valid,data.get('version',''),data.get('instance',''),'' if valid else 'invalid_response'
        finally:
            writer.close()
            try: await writer.wait_closed()
            except ConnectionError: pass
    return await asyncio.wait_for(exchange(),timeout)

def percentile(values,p):
    return sorted(values)[max(0,math.ceil(len(values)*p)-1)] if values else None

def summarize(rows,events,warmup_seconds=10):
    def group(selected):
        sent=[r for r in selected if r['result']!='unsent']
        successes=[r for r in sent if r['result'] in ('normal','delayed')]
        times=[r['elapsed_ms'] for r in successes]
        return {'scheduled':len(selected),'sent':len(sent),'normal':sum(r['result']=='normal' for r in sent),
                'delayed':sum(r['result']=='delayed' for r in sent),'failed':sum(r['result']=='failed' for r in sent),
                'unsent':len(selected)-len(sent),'failureRatePercent':100*sum(r['result']=='failed' for r in sent)/len(sent) if sent else None,
                'p95_ms':percentile(times,.95),'p99_ms':percentile(times,.99),'max_ms':max(times) if times else None}
    begin=next((e['offset'] for e in events if e['event']=='deploy_start'),None)
    end=next((e['offset'] for e in events if e['event']=='deploy_end'),None)
    for r in rows:
        sent=r['sent_offset']
        r['phase']='unsent' if sent is None else 'warmup' if sent<warmup_seconds else 'before' if begin is None or sent<begin else 'during' if end is None or sent<end else 'after'
    result={phase:group([r for r in rows if r['phase']==phase]) for phase in ('warmup','before','during','after')}
    result['overall']=group([r for r in rows if r['phase']!='warmup'])
    result['loadValid']=not any(r['result']=='unsent' for r in rows)
    result['events']=events
    result['transport']='HTTP/1.1, fresh TCP per request, no retries or redirects'
    baseline=result['before']
    result['baselineValid']=bool(baseline['sent']) and baseline['failed']==0 and baseline['p99_ms'] is not None and baseline['p99_ms']<=300
    result['deploymentComplete']=end is not None
    return result

async def measure(rps=100,seconds=30,deploy=False,port=18080,version='v2',key=None,deploy_action=None,resource_names=None,control_dir=None,warmup_seconds=10,baseline_seconds=10,post_seconds=20):
    if any(not isinstance(v,(int,float)) or not math.isfinite(v) or v<=0 for v in (warmup_seconds,baseline_seconds,post_seconds)):raise ValueError('positive finite phase durations required')
    key=key or poc.credentials()['api_key']
    folder=poc.ROOT/'results'/str(uuid.uuid4()); folder.mkdir(parents=True)
    completed=deque(maxlen=1200); instance_totals={}
    rows=[]; events=[]; tasks=set(); start=time.perf_counter(); wall=datetime.now(timezone.utc).isoformat()
    if control_dir:
        control_dir=Path(control_dir); control_dir.mkdir(parents=True,exist_ok=True)
        (control_dir/'result-path').write_text(str(folder))
    stop_at=warmup_seconds+baseline_seconds+300+post_seconds if deploy else seconds
    def event(name,**extra): events.append({'event':name,'offset':time.perf_counter()-start,**extra})
    async def resources():
        names=resource_names
        if names is None:
            names=['deployment-poc-postgres-1','deployment-poc-apache1-1','deployment-poc-apache2-1','deployment-poc-haproxy-1']
            mode=(poc.RUNTIME/'mode').read_text().strip()
            names+=['deployment-poc-tomcat1-1','deployment-poc-tomcat2-1'] if mode=='legacy' else [poc.CLUSTER]
        with (folder/'resources.jsonl').open('w',encoding='utf-8') as f:
            while True:
                process=None
                sample_start=time.perf_counter()
                try:
                    options={'creationflags':0x08000000} if os.name=='nt' else {}
                    process=await asyncio.create_subprocess_exec('docker','stats','--no-stream','--format','{{json .}}',*names,
                        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,**options)
                    stdout,_=await asyncio.wait_for(process.communicate(),timeout=8)
                    record={'offset':time.perf_counter()-start,'containers':[json.loads(line) for line in stdout.decode().splitlines() if line]}
                    f.write(json.dumps(record)+'\n'); f.flush()
                    await asyncio.sleep(max(0,1-(time.perf_counter()-sample_start)))
                except asyncio.CancelledError:
                    if process and process.returncode is None: process.kill(); await process.wait()
                    raise
                except Exception as e:
                    if process and process.returncode is None: process.kill(); await process.wait()
                    event('resource_monitor_error',error=type(e).__name__)
                    await asyncio.sleep(1)
    async def send(row):
        begin=time.perf_counter(); row['sent_offset']=begin-start; row['send_lag_ms']=(row['sent_offset']-row['scheduled_offset'])*1000
        try:
            status,valid,ver,instance,error=await request(port,key,row['request_id'])
            row.update(http_status=status,version=ver,instance=instance)
            elapsed=(time.perf_counter()-begin)*1000
            row['result']='failed' if not valid else 'normal' if elapsed<=300 else 'delayed'
            row['error']=error
        except Exception as e:
            row['result']='failed'; row['error']='timeout' if isinstance(e,asyncio.TimeoutError) else type(e).__name__
        row['complete_offset']=time.perf_counter()-start; row['elapsed_ms']=(time.perf_counter()-begin)*1000
        completed.append(row)
        name=row['instance'] or '__unknown__'
        aggregate=instance_totals.setdefault(name,{'count':0,'success':0,'total_ms':0})
        aggregate['count']+=1
        if row['result']!='failed':
            aggregate['success']+=1;aggregate['total_ms']+=row['elapsed_ms']
    async def deploy_task():
        nonlocal stop_at
        await wait_until(start+warmup_seconds+baseline_seconds)
        event('deploy_start')
        try:
            await asyncio.to_thread(deploy_action or poc.roll,version)
            event('deploy_end')
        except Exception as e:
            event('deploy_failed',error=type(e).__name__)
        stop_at=time.perf_counter()-start+post_seconds
    event('start',utc=wall,rps=rps)
    deployment=asyncio.create_task(deploy_task()) if deploy else None
    monitor=asyncio.create_task(resources()) if deploy or control_dir else None
    def live_details():
        fields=('request_id','result','elapsed_ms','instance','version','complete_offset','sent_offset','http_status','error')
        return {'runId':folder.name,'startedAt':wall,'rps':rps,
                'recent':[{k:r[k] for k in fields} for r in completed],
                'instances':instance_totals,'events':list(events),
                'errors':[{k:r[k] for k in fields} for r in rows if r['result']=='failed'][-8:]}
    async def publish_live(payload):
        # Windows can briefly deny replacement while a dashboard reads the file.
        for attempt in range(20):
            try:
                temp=control_dir/'live.tmp';temp.write_text(json.dumps(payload),encoding='utf-8');temp.replace(control_dir/'live.json')
                return True
            except OSError:
                await asyncio.sleep(.01)
        event('live_publish_error',error='file_busy')
        return False
    async def live_control():
        nonlocal stop_at
        while True:
            file=control_dir/'control.json'
            if file.exists():
                try:
                    control=json.loads(file.read_text())
                    if control.get('stop'):stop_at=min(stop_at,time.perf_counter()-start)
                except (OSError,ValueError):pass
            current={'elapsed':time.perf_counter()-start,'sent':sum(r['sent_offset'] is not None for r in rows),
                     **{kind:sum(r['result']==kind for r in rows) for kind in ('normal','delayed','failed')},
                     'unsent':sum(r['result']=='unsent' and r['error'] in ('schedule_lag','inflight_limit') for r in rows),'inflight':len(tasks)}
            current.update(live_details())
            await publish_live(current)
            await asyncio.sleep(.5)
    controller=asyncio.create_task(live_control()) if control_dir else None
    index=0;control_failed=False
    try:
        while True:
            if controller and controller.done() and not control_failed:
                control_failed=True;event('live_monitor_failed',error=type(controller.exception()).__name__)
                stop_at=min(stop_at,time.perf_counter()-start)
            target=index/rps
            now=time.perf_counter()-start
            if target>=stop_at:
                if now>=stop_at:break
                await asyncio.sleep(min(.5,stop_at-now));continue
            if now<target:
                await asyncio.sleep(min(.5,target-now) if control_dir else target-now);continue
            row={'request_id':str(uuid.uuid4()),'scheduled_offset':target,'sent_offset':None,'send_lag_ms':None,'complete_offset':None,
                 'elapsed_ms':None,'http_status':None,'result':'unsent','error':'','version':'','instance':''}
            rows.append(row)
            if now-target>.1: row['error']='schedule_lag'
            elif len(tasks)>=LIMIT: row['error']='inflight_limit'
            else:
                task=asyncio.create_task(send(row)); tasks.add(task); task.add_done_callback(tasks.discard)
            index+=1
        event('send_stop')
        if tasks: await asyncio.gather(*list(tasks))
        if deployment: await deployment
    finally:
        if controller:
            controller.cancel(); await asyncio.gather(controller,return_exceptions=True)
            marker=control_dir/'markers.jsonl'
            if marker.exists():
                for line in marker.read_text().splitlines():
                    e=json.loads(line); e['offset']=e.pop('absolute')-start; events.append(e)
                events.sort(key=lambda e:e['offset'])
        if monitor:
            monitor.cancel()
            await asyncio.gather(monitor,return_exceptions=True)
        event('collection_end')
        summary=summarize(rows,events,warmup_seconds)
        summary['liveMonitorHealthy']=not control_failed
        summary['protocol']={'rps':rps,'warmupSeconds':warmup_seconds,'baselineSeconds':baseline_seconds,'postDeploySeconds':post_seconds,'thresholdMs':300,'requestTimeoutSeconds':30}
        if control_dir:
            final={'elapsed':time.perf_counter()-start,'sent':sum(r['sent_offset'] is not None for r in rows),'inflight':0,
                   **{kind:sum(r['result']==kind for r in rows) for kind in ('normal','delayed','failed','unsent')}}
            final.update(live_details())
            await publish_live(final)
        (folder/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
        with (folder/'requests.csv').open('w',newline='',encoding='utf-8-sig') as f:
            writer=csv.DictWriter(f,fieldnames=list(rows[0]) if rows else ['request_id']); writer.writeheader(); writer.writerows(rows)
        (folder/'events.json').write_text(json.dumps(events,indent=2))
        print(folder)
    return summary

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--rps',type=float,default=100); p.add_argument('--seconds',type=float,default=30)
    p.add_argument('--deploy',action='store_true'); p.add_argument('--version',choices=['v1','v2'],default='v2')
    p.add_argument('--control-dir',type=Path)
    args=p.parse_args()
    if not .1<=args.rps<=200 or not 1<=args.seconds<=3600: p.error('RPS .1..200, seconds 1..3600')
    data=asyncio.run(measure(args.rps,args.seconds,args.deploy,version=args.version,control_dir=args.control_dir))
    print(json.dumps(data,indent=2))
