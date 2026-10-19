"""Sequential, bounded comparison. Preserve every attempted measurement."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.request
import uuid
import load
import poc
import variants

LOCK=poc.RUNTIME/'comparison-lock.json'
LABELS={'bluegreen':'2+2 rolling','k3s':'k3s','rolling':'1+1 rolling','legacy':'Legacy mod_jk','http':'mod_proxy HTTP','ajp':'mod_proxy AJP'}

def utc():return datetime.now(timezone.utc).isoformat()
def write(path,data):
    temp=path.with_suffix('.tmp')
    for _ in range(40):
        try:temp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(path);return
        except PermissionError:time.sleep(.01)
    raise PermissionError('file busy: '+str(path))

def fingerprint():
    digest=hashlib.sha256()
    files=[poc.ROOT/'pom.xml',poc.ROOT/'scripts/load.py',poc.ROOT/'scripts/variants.py',poc.ROOT/'scripts/poc.py',poc.ROOT/'scripts/compare.py',*sorted((poc.ROOT/'src').rglob('*')),*sorted((poc.ROOT/'infra').rglob('*'))]
    for p in files:
        if p.is_file():digest.update(p.relative_to(poc.ROOT).as_posix().encode());digest.update(p.read_bytes())
    return digest.hexdigest()

def stop_core():
    poc.kubectl('scale','deployment/demo-api','--replicas=0',timeout=15)
    poc.kubectl('wait','--for=delete','pod','-l','app=demo-api','--timeout=90s',timeout=100)
    poc.compose('--profile','legacy','stop','tomcat1','tomcat2','apache1','apache2','haproxy')

def round_order(modes,number):
    if len(modes)==4:
        # A balanced Williams design: each mode appears once per position in four rounds.
        indices=((0,1,3,2),(1,2,0,3),(2,3,1,0),(3,0,2,1))[(number-1)%4]
        return [modes[i] for i in indices]
    offset=(number-1)%len(modes)
    return modes[offset:]+modes[:offset]

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--modes',nargs='+',choices=list(LABELS),default=['legacy','k3s','bluegreen','rolling']);parser.add_argument('--profile',choices=['quick','standard'],default='quick');parser.add_argument('--pilot',action='store_true');parser.add_argument('--count',type=int,default=10);a=parser.parse_args()
    if len(set(a.modes))!=len(a.modes):parser.error('duplicate modes')
    if a.count!=(1 if a.pilot else 10):parser.error('formal: ten attempts per mode; pilot: one attempt per mode')
    for port in [18700,18701,18702,18703,18704,18705,18706]:
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/state',timeout=2) as r:s=json.load(r)
        except OSError:continue
        if s.get('busy') or s.get('measureBusy') or s.get('running'):raise RuntimeError('another operation or sender is active')
    with LOCK.open('x',encoding='utf-8') as f:json.dump({'pid':os.getpid(),'phase':'initializing'},f)
    root=poc.ROOT/'results'/(('pilot-' if a.pilot else 'comparison-')+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]);root.mkdir()
    warmup,baseline,post=(5,5,5) if a.profile=='quick' else (10,10,20)
    protocol={'createdAt':utc(),'plannedModes':a.modes,'count':a.count,'pilot':a.pilot,'rps':100,'warmupSeconds':warmup,'baselineSeconds':baseline,'postDeploySeconds':post,'profile':a.profile,'ajpKeepAliveTimeoutMs':1000,'gracefulStopAwaitMillis':30000,'requestTimeoutSeconds':30,'thresholdMs':300,
      'versionPolicy':'alternate v1/v2 per mode; only identification markers differ','orderPolicy':'balanced four-mode Williams order; no adjacent identical modes; all modes receive each position once per four rounds','preserveInvalidRuns':True,'noReplacementRuns':True,'sourceFingerprint':fingerprint()}
    write(root/'protocol.json',protocol)
    records=[];counts={};current=None;initial=[];success=False
    def progress(mode='',run=0,phase='setup'):
        write(LOCK,{'pid':os.getpid(),'mode':mode,'run':run,'count':a.count,'pilot':a.pilot,'phase':phase,'completed':len(records),'root':root.name,'updatedAt':utc()})
        write(root/'progress.json',{'mode':mode,'run':run,'phase':phase,'completed':len(records),'counts':counts,'updatedAt':utc()})
    def activate(mode):
        nonlocal current
        if current!=mode:
            if current=='k3s':stop_core()
            elif current:variants.compose(current,'stop')
            if mode=='k3s':
                data=json.loads(poc.kubectl('get','deployment','demo-api','-o','json',capture=True))
                version=next(e['value'] for e in data['spec']['template']['spec']['containers'][0]['env'] if e['name']=='POC_VERSION')
                poc.kubectl('scale','deployment/demo-api','--replicas=2');poc.wait_roll(version);poc.frontend('k3s');poc.wait_http(version=version)
            else:variants.start(mode)
            current=mode
        if mode=='k3s':
            port=18080;control=poc.RUNTIME/'live';action=poc.roll
            resources=['deployment-poc-postgres-1','deployment-poc-apache1-1','deployment-poc-apache2-1','deployment-poc-haproxy-1',poc.CLUSTER]
        else:
            port=variants.MODES[mode][1];control=variants.directory(mode)/'live';action=lambda v:variants.roll(mode,v)
            resources=['deployment-poc-postgres-1',poc.CLUSTER,*[variants.container(mode,n) for n in variants.members(mode)+['apache1','apache2','haproxy']]]
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/health/ready',timeout=5) as r:version=json.load(r)['version']
        if mode!='k3s':variants.verify_front(mode,variants.active(mode) if mode not in ('http','ajp') else set(variants.members(mode)),version)
        control.mkdir(exist_ok=True);(control/'control.json').write_text('{"stop":false}');(control/'markers.jsonl').write_text('')
        return port,control,action,resources,version
    try:
        initial=poc.run('docker','ps','--filter','name=deployment-poc','--format','{{.Names}}',capture=True).splitlines()
        write(root/'initial-running.json',initial)
        for m in variants.MODES:
            if (variants.directory(m)/'compose.json').exists():variants.compose(m,'stop')
        poc.start('k3s');current='k3s'
        metadata={'docker':json.loads(poc.run('docker','info','--format','{"version":"{{.ServerVersion}}","cpus":{{.NCPU}},"memoryBytes":{{.MemTotal}}}',capture=True)),
          'imageId':poc.run('docker','image','inspect',poc.IMAGE,'--format','{{.Id}}',capture=True).strip(),
          'java':poc.run('docker','run','--rm','--entrypoint','java',poc.IMAGE,'--version',capture=True).strip(),
          'warSha256':poc.run('docker','run','--rm','--entrypoint','sha256sum',poc.IMAGE,'/opt/releases/demo-api.war',capture=True).split()[0],
          'k3s':poc.run('docker','exec',poc.CLUSTER,'k3s','--version',capture=True).strip(),
          'postgres':poc.compose('exec','-T','postgres','postgres','--version',capture=True).strip(),
          'maxConnections':poc.compose('exec','-T','postgres','psql','-U','postgres','-tAc','SHOW max_connections',capture=True).strip()}
        metadata['serverXmlSha256']=poc.run('docker','run','--rm','--entrypoint','sha256sum',poc.IMAGE,'/usr/local/tomcat/conf/server.xml',capture=True).split()[0]
        for pod in json.loads(poc.kubectl('get','pods','-l','app=demo-api','-o','json',capture=True))['items']:
            name=pod['metadata']['name'];actual=poc.kubectl('exec',name,'--','sha256sum','/opt/releases/demo-api.war',capture=True).split()[0]
            assert actual==metadata['warSha256'],'WAR mismatch'
            assert poc.kubectl('exec',name,'--','sha256sum','/usr/local/tomcat/conf/server.xml',capture=True).split()[0]==metadata['serverXmlSha256'],'Tomcat configuration mismatch'
        write(root/'runtime.json',metadata)
        round_number=1
        while True:
            plan=json.loads((root/'protocol.json').read_text(encoding='utf-8'));modes=plan['plannedModes']
            if not set(modes)<=set(LABELS):raise ValueError('unsupported mode in protocol')
            if plan.get('cancelRequested'):raise RuntimeError('comparison stopped at user request')
            for key in ('plannedModes','rps','count','warmupSeconds','baselineSeconds','postDeploySeconds','sourceFingerprint'):
                if plan[key]!=protocol[key]:raise RuntimeError('protocol changed: '+key)
            pending=[m for m in round_order(modes,round_number) if counts.get(m,0)<a.count]
            if not pending:break
            for mode in pending:
                if json.loads((root/'protocol.json').read_text(encoding='utf-8')).get('cancelRequested'):raise RuntimeError('comparison stopped at user request')
                number=counts.get(mode,0)+1;progress(mode,number,'setup')
                assert fingerprint()==protocol['sourceFingerprint'],'measurement configuration changed'
                port,control,action,resources,version=activate(mode)
                target='v2' if version=='v1' else 'v1'
                sample={'mode':mode,'run':number,'round':round_number,'fromVersion':version,'toVersion':target,'startedAt':utc()}
                print('MEASURE '+mode+' '+str(number)+'/'+str(a.count)+' '+version+'->'+target,flush=True)
                progress(mode,number,'measurement')
                summary=asyncio.run(load.measure(rps=100,deploy=True,port=port,version=target,deploy_action=action,resource_names=resources,control_dir=control,warmup_seconds=warmup,baseline_seconds=baseline,post_seconds=post))
                result=Path((control/'result-path').read_text()).resolve();assert result.is_relative_to((poc.ROOT/'results').resolve())
                sample.update(summary=summary,resultFolder=result.name,finishedAt=utc());records.append(sample);counts[mode]=number
                write(root/'runs.json',records)
                print('DONE '+mode+' '+str(number)+'/'+str(a.count)+' '+json.dumps({k:summary[k] for k in ('deploymentComplete','loadValid','baselineValid')}),flush=True)
                if not summary['deploymentComplete']:raise RuntimeError('deployment incomplete; remaining attempts stopped')
                if not summary.get('liveMonitorHealthy',True):raise RuntimeError('live monitor failed; remaining attempts stopped')
            round_number+=1
        success=True;progress(phase='restoring')
        # Restore the initially running experiment, preserving new raw results.
        if current=='k3s':stop_core()
        elif current:variants.compose(current,'stop')
        for mode in variants.MODES:
            if any(n.startswith(variants.project(mode)+'-') for n in initial):variants.start(mode)
        if poc.CLUSTER not in initial:poc.compose('--profile','k3s','stop','k3s')
        write(root/'completion.json',{'status':'complete','counts':counts,'finishedAt':utc(),'initialServicesRestored':True})
    except BaseException as e:
        write(root/'completion.json',{'status':'incomplete','counts':counts,'finishedAt':utc(),'error':type(e).__name__,'message':str(e)})
        raise
    finally:
        write(root/'runs.json',records)
        for _ in range(40):
            try:LOCK.unlink();break
            except PermissionError:time.sleep(.01)
        print('COMPARISON_ROOT '+str(root),flush=True)
    return root

if __name__=='__main__':main()
