"""Isolated companion experiments; share only the read-only demo DB."""
import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import re
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import poc

MODES={'legacy':(18701,18180,18191),'http':(18702,18280,18291),'ajp':(18703,18480,18491),
       'bluegreen':(18704,18580,18591),'rolling':(18705,18880,18891)}
DEADLINE=None

def invoke(*args,**kwargs):
    if DEADLINE is not None:
        remaining=DEADLINE-time.monotonic()
        if remaining<=0:raise TimeoutError('deployment deadline exceeded; inspect pending.json')
        kwargs['timeout']=min(kwargs.get('timeout',600),remaining)
    return poc.run(*args,**kwargs)

def directory(mode): return poc.RUNTIME/'variants'/mode
def project(mode): return 'deployment-poc-'+mode
def compose(mode,*args,**kwargs):
    files=['-f',str(directory(mode)/'compose.json')]
    if (directory(mode)/'versions.json').exists(): files+=['-f',str(directory(mode)/'versions.json')]
    return invoke('docker','compose','-p',project(mode),*files,*args,**kwargs)
def container(mode,name): return project(mode)+'-'+name+'-1'
def members(mode): return ['tomcat1','tomcat2','tomcat3','tomcat4'] if mode=='bluegreen' else ['tomcat1','tomcat2']

def configure(mode):
    root=directory(mode); root.mkdir(parents=True,exist_ok=True)
    creds=poc.credentials(); ui,front,http=MODES[mode]
    common={'POC_DB_URL':'jdbc:postgresql://postgres:5432/deployment_poc',
        'POC_DB_PASSWORD':creds['db_password'],'POC_API_KEY':creds['api_key'],'POC_AJP_SECRET':creds['ajp_secret'],
        'POC_PROCESSING_MS':'100','POC_VERSION':'v1'}
    services={}
    for i,name in enumerate(members(mode)):
        services[name]={'image':poc.IMAGE,'environment':dict(common,POC_INSTANCE=f'{mode}-{name}'),
            'ports':[f'127.0.0.1:{http+i}:8080'],'stop_grace_period':'45s','cpus':1.0,'mem_limit':'1g',
            'networks':{'default':{}}}
        if mode in ('http','ajp'):
            tree=ET.parse(poc.ROOT/'infra/server.xml')
            service=tree.getroot().find('Service'); service.attrib.pop('gracefulStopAwaitMillis',None)
            for child in list(service):
                if child.tag=='Listener' or mode=='http' and child.tag=='Connector' and child.get('port')=='8009': service.remove(child)
                elif child.tag=='Connector': child.set('bindOnInit','true')
            file=root/'server.xml'; tree.write(file,encoding='utf-8')
            services[name]['volumes']=[file.as_posix()+':/usr/local/tomcat/conf/server.xml:ro']
    # App containers also join the shared DB network, but their service aliases
    # are resolved only on each experiment's private front network.
    for i in (1,2):
        services[f'apache{i}']={'image':'deployment-poc-apache:local',
            'environment':{'POC_WORKERS':','.join(members(mode)),'POC_AJP_SECRET':creds['ajp_secret'],
                           'POC_STOPPED':'tomcat3,tomcat4' if mode=='bluegreen' else '',
                           'POC_PROXY_KIND':mode if mode in ('http','ajp') else 'jk'},
            'ports':[f'127.0.0.1:{front+i}:80']}
    services['haproxy']={'image':'haproxy:3.0.11-alpine','ports':[f'127.0.0.1:{front}:8080'],
        'volumes':[(poc.ROOT/'infra/haproxy.cfg').as_posix()+':/usr/local/etc/haproxy/haproxy.cfg:ro']}
    config={'services':services,'networks':{'default':{'external':True,'name':project(mode)+'_default'}}}
    file=root/'compose.json'
    if not file.exists(): file.write_text(json.dumps(config,indent=2))
    else:
        existing=json.loads(file.read_text());existing['networks']['default']=config['networks']['default'];file.write_text(json.dumps(existing,indent=2))
    compose(mode,'config','--quiet')

def status(mode,apache=1):
    url='http://127.0.0.1/jk-status?'+urllib.parse.urlencode({'cmd':'show','w':'balancer','mime':'xml'})
    raw=invoke('docker','exec',container(mode,f'apache{apache}'),'curl','--fail','--silent','--max-time','5',url,capture=True,timeout=10)
    root=ET.fromstring(raw)
    result=[]
    for e in root.iter():
        if e.tag.split('}')[-1]=='member': result.append({k:v for k,v in e.attrib.items() if k in ('name','host','port','address','activation','state','busy','elected','errors','client_errors')})
    if not result: raise RuntimeError('JK status returned no members')
    return result

def activation(mode,active):
    if not active: raise ValueError('cannot exclude all members')
    if not set(active)<=set(members(mode)): raise ValueError('unknown member')
    for apache in (1,2):
        observed=status(mode,apache)
        if {m['name'] for m in observed}!=set(members(mode)):raise RuntimeError('unexpected JK membership; no update applied')
        params={'cmd':'update','w':'balancer','att':'vwa','mime':'xml'}
        for i,member in enumerate(observed): params[f'val{i}']='0' if member['name'] in active else '2'
        url='http://127.0.0.1/jk-status?'+urllib.parse.urlencode(params)
        invoke('docker','exec',container(mode,f'apache{apache}'),'curl','--fail','--silent','--max-time','5',url,capture=True,timeout=10)
        after=status(mode,apache)
        for member in after:
            expected='ACT' if member['name'] in active else 'STP'
            if member['activation'] not in (expected,'Active' if expected=='ACT' else 'Stopped', 'ACTIVATED' if expected=='ACT' else 'STOPPED'):
                raise RuntimeError('JK activation confirmation failed: '+member['name']+' '+member['activation'])
        lines=[f'worker.{name}.activation='+('Active' if name in active else 'Stopped') for name in members(mode)]
        # Retain the live configuration if Apache itself is restarted.
        script='\n'.join([f"sed -i '/^worker\\.{name}\\.activation=/d' /run/poc/workers.properties" for name in members(mode)])+'\nprintf "%s\\n" '+ ' '.join("'"+line+"'" for line in lines)+' >> /run/poc/workers.properties'
        invoke('docker','exec',container(mode,f'apache{apache}'),'sh','-c',script,capture=True)
    (directory(mode)/'active.json').write_text(json.dumps(sorted(active)))

def active(mode):
    sets=[]
    for a in (1,2): sets.append({m['name'] for m in status(mode,a) if m['activation'] in ('ACT','Active','ACTIVATED')})
    if sets[0]!=sets[1]: raise RuntimeError('Apache activation states differ; reconcile before continuing')
    return sets[0]

def drain(mode,names):
    deadline=time.monotonic()+40; zero=None
    while time.monotonic()<deadline:
        states=[status(mode,a) for a in (1,2)]
        if any(not set(names)<={m['name'] for m in state} for state in states):raise RuntimeError('drain target missing from JK status')
        quiet=all(int(m.get('busy','0'))==0 and m['activation'] in ('STP','Stopped','STOPPED')
            for state in states for m in state if m['name'] in names)
        if quiet:
            zero=zero or time.monotonic()
            if time.monotonic()-zero>=2: return
        else: zero=None
        time.sleep(.2)
    raise TimeoutError('in-flight requests did not drain; process preserved')

def api_ready(mode,name,version=None):
    port=MODES[mode][2]+members(mode).index(name)
    poc.wait_http(port,version,timeout=min(90,max(0,DEADLINE-time.monotonic())) if DEADLINE else 90)
    request=urllib.request.Request(f'http://127.0.0.1:{port}/api/companies/1',headers={'X-API-Key':poc.credentials()['api_key']})
    with urllib.request.urlopen(request,timeout=5) as r: data=json.load(r)
    if data['company']['id']!=1 or version and data['version']!=version: raise RuntimeError('API not ready')
    return data

def refresh_endpoint(mode,name):
    api_ready(mode,name)
    ip=invoke('docker','inspect','-f','{{(index .NetworkSettings.Networks "'+project(mode)+'_default").IPAddress}}',container(mode,name),capture=True).strip()
    import ipaddress
    ipaddress.ip_address(ip)
    for apache in (1,2):
        params={'cmd':'update','w':'balancer','sw':name,'vahst':ip,'vaprt':'8009','mime':'xml'}
        url='http://127.0.0.1/jk-status?'+urllib.parse.urlencode(params)
        invoke('docker','exec',container(mode,f'apache{apache}'),'curl','--fail','--silent','--max-time','5',url,capture=True)
        observed=next(m for m in status(mode,apache) if m['name']==name)
        if observed.get('host')!=ip or observed.get('port')!='8009' or observed.get('address')=='invalid':raise RuntimeError('JK endpoint update was not applied')
        url='http://127.0.0.1/jk-status?'+urllib.parse.urlencode({'cmd':'recover','w':'balancer','sw':name,'mime':'xml'})
        invoke('docker','exec',container(mode,f'apache{apache}'),'curl','--fail','--silent','--max-time','5',url,capture=True)
        script=f"sed -i '/^worker\\.{name}\\.host=/d; /^worker\\.{name}\\.port=/d' /run/poc/workers.properties\nprintf '%s\\n' 'worker.{name}.host={ip}' 'worker.{name}.port=8009' >> /run/poc/workers.properties"
        invoke('docker','exec',container(mode,f'apache{apache}'),'sh','-c',script,capture=True)

def verify_front(mode,names,version=None):
    expected={mode+'-'+name for name in names}
    for apache in (1,2):
        seen=set();end=time.monotonic()+(120 if mode=='legacy' else 8);sample=0
        while time.monotonic()<end:
            request=urllib.request.Request(f'http://127.0.0.1:{MODES[mode][1]+apache}/api/companies/1',headers={'X-API-Key':poc.credentials()['api_key']})
            try:
                with urllib.request.urlopen(request,timeout=3) as response:data=json.load(response)
            except (OSError,TimeoutError):
                if mode!='legacy':raise
                time.sleep(.2);continue
            if data.get('company',{}).get('id')!=1 or data.get('instance') not in expected or version and data.get('version')!=version:raise RuntimeError('frontend returned an unexpected deployment')
            seen.add(data['instance'])
            if seen==expected:break
            sample+=1;time.sleep((0,.007,.023)[sample%3])
        if seen!=expected:raise RuntimeError('new group distribution not confirmed through both Apache servers')

def start(mode):
    configure(mode); poc.ensure_networks();poc.compose('up','-d','--wait','postgres')
    saved=directory(mode)/'active.json'
    names=json.loads(saved.read_text()) if saved.exists() else members(mode)[:2]
    compose(mode,'create',*names)
    network=project(mode)+'_default'
    attached=json.loads(poc.run('docker','network','inspect',network,capture=True))[0]['Containers']
    if not any(c['Name']=='deployment-poc-postgres-1' for c in attached.values()):
        poc.run('docker','network','connect','--alias','postgres',network,'deployment-poc-postgres-1')
    compose(mode,'up','-d',*names)
    for name in names: api_ready(mode,name)
    compose(mode,'up','-d','apache1','apache2','haproxy')
    if mode not in ('http','ajp'):
        for attempt in range(40):
            try: status(mode);break
            except subprocess.CalledProcessError:
                if attempt==39: raise
                time.sleep(.25)
        for name in names:refresh_endpoint(mode,name)
        activation(mode,set(names))
    poc.wait_http(MODES[mode][1])

def node(mode,name,action):
    if name not in members(mode): raise ValueError('unknown member')
    if action=='exclude':
        enabled=active(mode); enabled.discard(name)
        activation(mode,enabled); drain(mode,{name})
    elif action=='include':
        api_ready(mode,name);refresh_endpoint(mode,name);activation(mode,active(mode)|{name})
    elif action=='stop':
        if mode in ('rolling','bluegreen'):
            if name in active(mode): raise ValueError('exclude incoming traffic before stopping this member')
            drain(mode,{name})
        compose(mode,'stop',name)
    elif action=='start':
        compose(mode,'start',name); api_ready(mode,name)
    else: raise ValueError('unknown node action')

def replace(mode,name,version):
    compose(mode,'stop',name)
    file=directory(mode)/'versions.json'
    data=json.loads(file.read_text()) if file.exists() else {'services':{}}
    data['services'][name]={'environment':{'POC_VERSION':version}}
    file.write_text(json.dumps(data))
    invoke('docker','compose','-p',project(mode),'-f',str(directory(mode)/'compose.json'),'-f',str(file),
        'up','-d','--no-deps','--force-recreate',name)
    api_ready(mode,name,version)
    if mode in ('rolling','bluegreen'):refresh_endpoint(mode,name)

def roll(mode,version):
    global DEADLINE
    DEADLINE=time.monotonic()+180
    try:_roll(mode,version)
    except Exception:
        stage(mode,0,'배포 실패 · pending.json 및 실제 상태 확인',True);raise
    finally:DEADLINE=None

def stage(mode,number,label,failed=False):
    record={'stage':number,'label':label,'failed':failed,'time':time.time(),'strategy':'rolling-2plus2' if mode=='bluegreen' else 'rolling'}
    path=directory(mode)/'deployment.json';temp=path.with_suffix('.tmp')
    for attempt in range(20):
        try:
            temp.write_text(json.dumps(record),encoding='utf-8');temp.replace(path);break
        except OSError:time.sleep(.01)
    with (directory(mode)/'deployment-events.jsonl').open('a',encoding='utf-8') as stream:stream.write(json.dumps(record)+'\n')
    print(label,flush=True)

def _roll(mode,version):
    root=directory(mode); pending=root/'pending.json'
    if pending.exists(): raise RuntimeError('previous deployment incomplete: inspect pending.json and actual state before retry')
    pending.write_text(json.dumps({'version':version,'started':time.time()}))
    if mode=='bluegreen':
        live=active(mode)
        if len(live)!=2:raise RuntimeError('2+2 rolling requires exactly two active members')
        old=sorted(live);spares=sorted(set(members(mode))-live)
        for name in old:api_ready(mode,name)
        for index,(previous,replacement) in enumerate(zip(old,spares),1):
            prefix=f'{index}/2 · '
            stage(mode,1,prefix+'단일 인스턴스 교체 준비 · '+version)
            stage(mode,2,prefix+replacement+' 대체 인스턴스 기동')
            replace(mode,replacement,version)
            stage(mode,3,prefix+replacement+' readiness·API·AJP 주소 확인 완료')
            stage(mode,4,prefix+replacement+' 유입 투입 · 일시 3대')
            activation(mode,live|{replacement})
            verify_front(mode,live|{replacement})
            stage(mode,5,prefix+previous+' 유입 제외 · 진행 요청 완료 대기')
            next_live=(live-{previous})|{replacement}
            activation(mode,next_live)
            drain(mode,{previous})
            verify_front(mode,next_live)
            stage(mode,6,prefix+previous+' 종료 · 다음 인스턴스 교체')
            compose(mode,'stop',previous)
            live=next_live
        verify_front(mode,live,version)
    else:
        for name in members(mode):
            if mode=='rolling': node(mode,name,'exclude')
            replace(mode,name,version)
            if mode=='rolling': node(mode,name,'include')
        verify_front(mode,set(members(mode)),version)
    pending.unlink()
    stage(mode,7,'배포 완료 · '+version)

def main():
    p=argparse.ArgumentParser(); p.add_argument('action',choices=['start','stop','status','roll','node','measure']); p.add_argument('--mode',choices=list(MODES),required=True)
    p.add_argument('--version',choices=['v1','v2'],default='v2'); p.add_argument('--node',choices=['tomcat1','tomcat2','tomcat3','tomcat4'],default='tomcat1')
    p.add_argument('--node-action',choices=['start','stop','exclude','include'],default='exclude'); p.add_argument('--manual',action='store_true'); p.add_argument('--rps',type=float,default=100)
    a=p.parse_args(); mode=a.mode
    if not .1<=a.rps<=200:p.error('rps must be between .1 and 200')
    if a.action=='start': start(mode)
    elif a.action=='stop': compose(mode,'stop')
    elif a.action=='status':
        compose(mode,'ps')
        if mode not in ('http','ajp'): print(json.dumps([status(mode,i) for i in (1,2)],indent=2))
    elif a.action=='roll': roll(mode,a.version)
    elif a.action=='node': node(mode,a.node,a.node_action)
    else:
        import load
        asyncio.run(load.measure(rps=a.rps,seconds=330 if a.manual else 30,deploy=not a.manual,port=MODES[mode][1],
            version=a.version,deploy_action=lambda v:roll(mode,v),resource_names=[container(mode,x) for x in members(mode)+['apache1','apache2','haproxy']],
            control_dir=directory(mode)/'live'))

if __name__=='__main__': main()
