"""Separate causal diagnostics. Preserved 40-run source/configuration is never changed."""
import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import load

ROOT=Path(__file__).resolve().parents[1]
DIAG=ROOT/'scripts/diagnostics'
IMAGE='deployment-poc-api:local'
SERVICES=['tomcat1','tomcat2','apache1','apache2','haproxy']


def write(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def command(*args,timeout=90):
    r=subprocess.run(list(args),capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout)
    if r.returncode:raise RuntimeError('command failed: '+' '.join(args[:4])+' '+r.stderr[-1000:])
    return r.stdout


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


def fields(line,prefix):
    if prefix not in line:return None
    return dict(re.findall(r'([A-Za-z_]+)=([^\s]+)',line.split(prefix,1)[1]))


class Capture:
    def __init__(self,lab,folder):
        self.lab=lab;self.folder=folder;self.lines=defaultdict(list);self.processes=[];self.threads=[]
        for name in SERVICES:self.attach(name)
    def attach(self,name):
        process=subprocess.Popen(['docker','logs','--follow',self.lab.container(name)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace')
        self.processes.append(process)
        thread=threading.Thread(target=self.read,args=(name,process),daemon=True);thread.start();self.threads.append(thread)
    def read(self,name,process):
        with (self.folder/(name+'.log')).open('a',encoding='utf-8') as stream:
            for line in process.stdout:
                for value in self.lab.creds.values():line=line.replace(value,'[REDACTED]')
                self.lines[name].append(line);stream.write(line);stream.flush()
    def begins(self,request_id):
        return [x for line in self.lines['tomcat1'] for x in [fields(line,'POC_TRACE')] if x and x.get('id')==request_id and x.get('event')=='BEGIN']
    def close(self):
        time.sleep(.3)
        for p in self.processes:
            if p.poll() is None:p.terminate()
        for p in self.processes:
            try:p.wait(timeout=5)
            except subprocess.TimeoutExpired:p.kill();p.wait()
        for t in self.threads:t.join(timeout=3)


class Lab:
    def __init__(self,output):
        self.output=output;self.project='poc-diagnostic-'+uuid.uuid4().hex[:8]
        self.private=ROOT/'.runtime'/self.project;self.private.mkdir(parents=True)
        if os.name=='nt':command('icacls',str(self.private),'/inheritance:r','/grant:r',f'{os.environ["USERDOMAIN"]}\\{os.environ["USERNAME"]}:(OI)(CI)(F)')
        self.creds={k:secrets.token_hex(24) for k in ['api_key','db_password','admin_password','ajp_secret']}
        self.ports={name:free_port() for name in ['haproxy','tomcat1','tomcat2']}
        self.compose_file=self.private/'compose.json'
        sql="CREATE ROLE poc_app LOGIN PASSWORD '"+self.creds['db_password']+"';\nCREATE TABLE companies(id bigint PRIMARY KEY,name text NOT NULL,registration_number text NOT NULL);\nINSERT INTO companies VALUES(1,'Synthetic Company','DEMO-001');\nGRANT CONNECT ON DATABASE deployment_poc TO poc_app; GRANT USAGE ON SCHEMA public TO poc_app; GRANT SELECT ON companies TO poc_app; REVOKE CREATE ON SCHEMA public FROM PUBLIC; ALTER ROLE poc_app SET default_transaction_read_only=on;\n"
        (self.private/'init.sql').write_text(sql,encoding='utf-8')
        (self.private/'trace-classes').mkdir()
        server=ET.parse(ROOT/'infra/server.xml');host=server.getroot().find('./Service/Engine/Host');host.insert(0,ET.Element('Valve',{'className':'local.poc.TraceValve'}));server.write(self.private/'server.xml',encoding='utf-8')
        common={'POC_DB_URL':'jdbc:postgresql://postgres:5432/deployment_poc','POC_DB_PASSWORD':self.creds['db_password'],'POC_API_KEY':self.creds['api_key'],'POC_AJP_SECRET':self.creds['ajp_secret'],'POC_PROCESSING_MS':'100','POC_VERSION':'v1'}
        services={'postgres':{'image':'postgres:13.23-bookworm','environment':{'POSTGRES_DB':'deployment_poc','POSTGRES_PASSWORD':self.creds['admin_password']},'command':['postgres','-c','max_connections=600'],'volumes':[f'{(self.private/"init.sql").as_posix()}:/docker-entrypoint-initdb.d/01.sql:ro'],'healthcheck':{'test':['CMD-SHELL','pg_isready -U postgres -d deployment_poc'],'interval':'1s','timeout':'3s','retries':60}}}
        for name in ['tomcat1','tomcat2']:
            services[name]={'image':IMAGE,'environment':dict(common,POC_INSTANCE=name),'cpus':1.0,'mem_limit':'1g','stop_grace_period':'45s','ports':[f'127.0.0.1:{self.ports[name]}:8080'],'volumes':[f'{(self.private/"server.xml").as_posix()}:/usr/local/tomcat/conf/server.xml:ro',f'{(self.private/"trace.jar").as_posix()}:/usr/local/tomcat/lib/poc-trace.jar:ro']}
        for name in ['apache1','apache2']:
            services[name]={'image':'deployment-poc-apache:local','environment':{'POC_WORKERS':'tomcat1,tomcat2','POC_AJP_SECRET':self.creds['ajp_secret']},'volumes':[f'{(DIAG/"apache.conf").as_posix()}:/etc/apache2/sites-enabled/poc.conf:ro']}
        services['haproxy']={'image':'haproxy:3.0.11-alpine','ports':[f'127.0.0.1:{self.ports["haproxy"]}:8080'],'volumes':[f'{(DIAG/"haproxy.cfg").as_posix()}:/usr/local/etc/haproxy/haproxy.cfg:ro']}
        write(self.compose_file,{'services':services})
    def container(self,name):return self.project+'-'+name+'-1'
    def compose(self,*args,timeout=100):return command('docker','compose','-p',self.project,'-f',str(self.compose_file),*args,timeout=timeout)
    def prepare(self):
        command('docker','run','--rm','--user','0','--entrypoint','sh','-v',f'{DIAG.as_posix()}:/source:ro','-v',f'{self.private.as_posix()}:/build',IMAGE,'-c','javac -cp "/usr/local/tomcat/lib/*" -d /build/trace-classes /source/TraceValve.java && jar cf /build/trace.jar -C /build/trace-classes .')
        self.compose('up','-d','--wait','postgres')
    def start(self):
        self.compose('up','-d','--force-recreate',*SERVICES)
        for name in ['tomcat1','tomcat2','haproxy']:self.ready(name)
    def ready(self,name,timeout=90):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{self.ports[name]}/health/ready',timeout=2) as r:
                    if r.status==200:return
            except OSError:pass
            time.sleep(.2)
        raise TimeoutError('readiness: '+name)
    def jk(self,apache,params):
        url='http://127.0.0.1/jk-status?'+urllib.parse.urlencode(params)
        return command('docker','exec',self.container(apache),'curl','--fail','--silent','--max-time','4',url,timeout=8)
    def snapshot(self):
        result={}
        for apache in ['apache1','apache2']:
            root=ET.fromstring(self.jk(apache,{'cmd':'show','w':'balancer','mime':'xml'}))
            result[apache]=[{k:v for k,v in e.attrib.items() if k in ('name','activation','state','busy','elected','errors','client_errors')} for e in root.iter() if e.tag.split('}')[-1]=='member']
        return result
    def exclude(self):
        for apache in ['apache1','apache2']:
            root=ET.fromstring(self.jk(apache,{'cmd':'show','w':'balancer','mime':'xml'}))
            members=[e for e in root.iter() if e.tag.split('}')[-1]=='member']
            params={'cmd':'update','w':'balancer','att':'vwa','mime':'xml'}
            for i,m in enumerate(members):params['val'+str(i)]='2' if m.get('name')=='tomcat1' else '0'
            self.jk(apache,params)
        observed=self.snapshot()
        assert all(m['activation'] in ('STP','Stopped','STOPPED') for values in observed.values() for m in values if m['name']=='tomcat1')
        return observed
    def state(self,name):return json.loads(command('docker','inspect','-f','{{json .State}}',self.container(name)))
    def stop(self):self.compose('down','--volumes',timeout=90)


async def trial(lab,folder,case,number):
    folder.mkdir(parents=True);capture=Capture(lab,folder);rows=[];events=[];tasks=set();start=time.perf_counter();stop=asyncio.Event()
    def event(name,**data):
        row={'event':name,'wall_ms':time.time_ns()/1e6,'offset':time.perf_counter()-start,**data};events.append(row);write(folder/'events.json',events)
    async def request(request_id,role):
        begin=time.perf_counter();row={'id':request_id,'role':role,'sent_offset':begin-start,'wall_ms':time.time_ns()/1e6}
        try:
            status,valid,version,instance,error=await load.request(lab.ports['haproxy'],lab.creds['api_key'],request_id)
            row.update(status=status,valid=valid,version=version,instance=instance,error=error)
        except Exception as e:row.update(status=None,valid=False,error=type(e).__name__)
        row['elapsed_ms']=(time.perf_counter()-begin)*1000;row['complete_offset']=time.perf_counter()-start;rows.append(row);return row
    async def sender():
        index=0
        while not stop.is_set() and time.perf_counter()-start<90:
            target=index/100;remaining=target-(time.perf_counter()-start)
            if remaining>0:await asyncio.sleep(remaining)
            if stop.is_set():break
            if time.perf_counter()-start-target>.1:
                rows.append({'id':'unsent-'+uuid.uuid4().hex,'role':'background','valid':False,'error':'schedule_lag','sent_offset':None})
            else:
                t=asyncio.create_task(request('bg-'+uuid.uuid4().hex,'background'));tasks.add(t);t.add_done_callback(tasks.discard)
            index+=1
    send_task=asyncio.create_task(sender());error=None;probe=None
    try:
        await asyncio.sleep(5);event('baseline_end')
        if case in ('sigkill','drain_stop'):
            for _ in range(12):
                request_id='probe-'+uuid.uuid4().hex
                probe_task=asyncio.create_task(request(request_id,'probe'))
                deadline=time.monotonic()+4
                while not capture.begins(request_id) and not probe_task.done() and time.monotonic()<deadline:await asyncio.sleep(.01)
                if capture.begins(request_id):probe=request_id;break
                await probe_task
            if probe is None:raise RuntimeError('no in-flight target probe observed')
            event('probe_begin_observed',request_id=probe)
            snap=await asyncio.to_thread(lab.snapshot);event('before_action',jk=snap)
            if case=='sigkill':
                event('kill_request',request_id=probe)
                await asyncio.to_thread(command,'docker','kill','--signal','KILL',lab.container('tomcat1'))
                event('kill_ack',state=await asyncio.to_thread(lab.state,'tomcat1'))
            else:
                event('exclude_request');snap=await asyncio.to_thread(lab.exclude);event('exclude_ack',jk=snap)
                deadline=time.monotonic()+35;zero=None
                while time.monotonic()<deadline:
                    snap=await asyncio.to_thread(lab.snapshot)
                    busy=sum(int(m['busy']) for members in snap.values() for m in members if m['name']=='tomcat1')
                    event('drain_sample',busy=busy,jk=snap)
                    zero=(zero or time.monotonic()) if busy==0 else None
                    if zero and time.monotonic()-zero>=2:break
                    await asyncio.sleep(.2)
                else:raise TimeoutError('drain')
                event('drain_complete');await asyncio.to_thread(lab.compose,'stop','tomcat1');event('stop_ack',state=await asyncio.to_thread(lab.state,'tomcat1'))
            await probe_task
        else:
            event('stop_request');await asyncio.to_thread(lab.compose,'stop','tomcat1');event('stop_ack',state=await asyncio.to_thread(lab.state,'tomcat1'))
            await asyncio.to_thread(lab.compose,'up','-d','--no-deps','--force-recreate','tomcat1');capture.attach('tomcat1');await asyncio.to_thread(lab.ready,'tomcat1');event('restart_ready')
        await asyncio.sleep(5)
    except BaseException as e:
        error=type(e).__name__+': '+str(e);event('diagnostic_error',message=error)
    finally:
        stop.set();await send_task
        if tasks:await asyncio.gather(*list(tasks))
        capture.close();write(folder/'requests.json',rows);write(folder/'protocol.json',{'case':case,'run':number,'rps':100,'probeId':probe,'probeHoldTargetMs':2500 if probe else 0,'thresholdMs':300,'clientRetries':0,'error':error,'probeExcludedFromBackgroundMetrics':True})
    if error:raise RuntimeError(error)


def analyze(folder):
    protocol=json.loads((folder/'protocol.json').read_text());rows=json.loads((folder/'requests.json').read_text());events=json.loads((folder/'events.json').read_text())
    proxies=[];front=[];trace=[]
    for name,prefix,dest in [('apache1','POC_APACHE',proxies),('apache2','POC_APACHE',proxies),('haproxy','POC_HAPROXY',front),('tomcat1','POC_TRACE',trace),('tomcat2','POC_TRACE',trace)]:
        for line in (folder/(name+'.log')).read_text(encoding='utf-8').splitlines():
            parsed=fields(line,prefix)
            if parsed:dest.append(dict(parsed,source=name))
    by_id=defaultdict(list)
    for r in trace:by_id[r.get('id')].append(r)
    background=[r for r in rows if r['role']=='background'];counts=Counter('unsent' if r.get('sent_offset') is None else 'failed' if not r['valid'] else 'delayed' if r['elapsed_ms']>300 else 'normal' for r in background)
    client={r['id']:r for r in rows}
    changed=[r for r in proxies if r.get('first') not in (None,'-') and r.get('last') not in (None,'-',r['first']) and r.get('id') in client and client[r['id']]['valid']]
    replayed=[r for r in changed if any(t['event']=='BEGIN' and t['instance']==r['first'] for t in by_id[r['id']]) and any(t['event']=='END' and t['instance']==r['last'] and t['status']=='200' for t in by_id[r['id']])]
    probe=protocol['probeId'];facts={}
    if probe:
        target_trace=by_id[probe];access=[r for r in proxies if r.get('id')==probe];upstream=[r for r in front if r.get('id')==probe]
        facts={'requestId':probe,'clientCalls':sum(r['id']==probe for r in rows),'clientSuccess':client[probe]['valid'],'clientElapsedMs':client[probe]['elapsed_ms'],'clientInstance':client[probe].get('instance'),'tomcatTrace':sorted(target_trace,key=lambda t:float(t['wall_ms'])),'apacheAccess':access,'haproxyAccess':upstream,'oneFrontRequestNoRetry':len(upstream)==1 and upstream[0].get('retries')=='0'}
        if protocol['case']=='sigkill':
            kill=next(e for e in events if e['event']=='kill_request')
            facts['killRequestWallMs']=kill['wall_ms']
            facts['targetBeginBeforeKill']=any(t['event']=='BEGIN' and t['instance']=='tomcat1' and float(t['wall_ms'])<kill['wall_ms'] for t in target_trace)
            facts['confirmed']=bool(facts['targetBeginBeforeKill'] and len(access)==1 and any(r['id']==probe for r in replayed) and facts['clientCalls']==1 and facts['clientSuccess'] and facts['clientInstance']=='tomcat2' and facts['oneFrontRequestNoRetry'] and not any(t['event']=='END' and t['instance']=='tomcat1' for t in target_trace) and any(e['event']=='kill_ack' and e['state']['ExitCode']==137 for e in events))
        else:
            excluded=next(e for e in events if e['event']=='exclude_ack');drained=next(e for e in events if e['event']=='drain_complete')
            new_ids={r['id'] for r in background if r.get('sent_offset') is not None and r['wall_ms']>excluded['wall_ms']}
            facts['newRequestsSentAfterExclude']=len(new_ids)
            facts['newRequestsCompletedOnSurvivor']=len({t['id'] for t in trace if t['event']=='END' and t['instance']=='tomcat2' and t.get('status')=='200' and t['id'] in new_ids})
            facts['newRequestsReachedExcludedTarget']=sum(t['event']=='BEGIN' and t['instance']=='tomcat1' and t['id'] in new_ids for t in trace)
            facts['confirmed']=bool(len(access)==1 and len(new_ids)>0 and facts['newRequestsCompletedOnSurvivor']==len(new_ids) and facts['clientCalls']==1 and facts['clientSuccess'] and facts['clientInstance']=='tomcat1' and facts['oneFrontRequestNoRetry'] and any(t['event']=='END' and t['instance']=='tomcat1' and float(t['wall_ms'])<drained['wall_ms'] for t in target_trace) and not facts['newRequestsReachedExcludedTarget'] and all(a.get('first')==a.get('last')=='tomcat1' for a in access))
    result={'case':protocol['case'],'run':protocol['run'],'background':dict(counts),'successfulChangedWorkerRequests':len(changed),'inFlightReplayedRequests':len(replayed),'inFlightReplayedBackgroundRequests':sum(r['id'].startswith('bg-') for r in replayed),'delayedChangedWorkerBackgroundRequests':sum(r['id'].startswith('bg-') and client[r['id']]['elapsed_ms']>300 for r in changed),'probe':facts,'error':protocol['error'],'examples':replayed[:6]}
    write(folder/'analysis.json',result);return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['run','audit']);parser.add_argument('--root',type=Path);parser.add_argument('--count',type=int,default=3);parser.add_argument('--cases',nargs='+',choices=['sequential_stop','sigkill','drain_stop'],default=['sequential_stop','sigkill','drain_stop']);a=parser.parse_args()
    if a.action=='audit':
        assert a.root;results=[analyze(p) for p in sorted(a.root.iterdir()) if p.is_dir() and (p/'protocol.json').exists()];write(a.root/'analysis.json',results);assert results and all(not r['error'] and not r['background'].get('unsent',0) and (not r['probe'] or r['probe']['confirmed']) for r in results),'causal evidence incomplete';print(json.dumps([{'case':r['case'],'run':r['run'],'background':r['background'],'confirmed':r['probe'].get('confirmed')} for r in results],ensure_ascii=False));return
    if not 1<=a.count<=3:parser.error('count must be 1..3')
    output=a.root or ROOT/'results'/('failover-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]);output.mkdir(parents=True,exist_ok=False)
    lab=Lab(output);records=[]
    protocol={'createdAt':datetime.now(timezone.utc).isoformat(),'cases':a.cases,'count':a.count,'rps':100,'appImageId':command('docker','image','inspect',IMAGE,'--format','{{.Id}}').strip(),'apacheImageId':command('docker','image','inspect','deployment-poc-apache:local','--format','{{.Id}}').strip(),'probePolicy':'Only marked probe on tomcat1 waits 2500ms before application. Probe metrics excluded from normal 100ms background. No client retries.','original40RunsUnchanged':True,'warSha256':command('docker','run','--rm','--entrypoint','sha256sum',IMAGE,'/opt/releases/demo-api.war').split()[0],'sourceHashes':{p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__),ROOT/'scripts/load.py',ROOT/'infra/apache-start.sh',ROOT/'infra/server.xml',*sorted(DIAG.glob('*'))] if p.is_file()}}
    write(output/'protocol.json',protocol)
    try:
        lab.prepare()
        for number in range(1,a.count+1):
            for case in a.cases:
                print('START',case,number,flush=True);lab.start();folder=output/(str(number)+'-'+case);asyncio.run(trial(lab,folder,case,number));result=analyze(folder);records.append(result);write(output/'analysis.json',records);print('DONE',case,number,json.dumps({'background':result['background'],'workerChanges':result['successfulChangedWorkerRequests'],'probeConfirmed':result['probe'].get('confirmed')}),flush=True)
        write(output/'completion.json',{'complete':True,'finishedAt':datetime.now(timezone.utc).isoformat(),'attempts':len(records)})
    except BaseException as e:
        write(output/'completion.json',{'complete':False,'attempts':len(records),'error':str(e)});raise
    finally:
        lab.stop();print('DIAGNOSTIC_ROOT',output,flush=True)


if __name__=='__main__':main()
