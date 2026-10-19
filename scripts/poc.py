"""Windows orchestration; only resources named deployment-poc are managed."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.runtime'
COMPOSE = RUNTIME / 'compose.json'
CLUSTER = 'deployment-poc-k3s'
IMAGE = 'deployment-poc-api:local'
EXPERIMENTS=('legacy','http','ajp','bluegreen','rolling')

def ensure_networks():
    existing=set(run('docker','network','ls','--format','{{.Name}}',capture=True).splitlines())
    for name in EXPERIMENTS:
        network=f'deployment-poc-{name}_default'
        if network not in existing: run('docker','network','create',network,capture=True)

def run(*args, capture=False, input=None, timeout=600):
    result = subprocess.run(list(args), cwd=ROOT, input=input, text=True, encoding='utf-8', errors='replace',
                            stdout=subprocess.PIPE if capture else None, check=True, timeout=timeout)
    return result.stdout if capture else ''

def compose(*args, **kwargs):
    return run('docker', 'compose', '-p', 'deployment-poc', '-f', str(COMPOSE), *args, **kwargs)

def kubectl(*args, **kwargs):
    return run('docker', 'exec', '-i', CLUSTER, 'kubectl', '-n', 'deployment-poc', *args, **kwargs)

def credentials():
    return json.loads((RUNTIME / 'secrets.json').read_text())

def configure():
    RUNTIME.mkdir(exist_ok=True)
    if os.name == 'nt':
        run('icacls',str(RUNTIME),'/inheritance:r','/grant:r',f'{os.environ["USERDOMAIN"]}\\{os.environ["USERNAME"]}:(OI)(CI)(F)',capture=True)
    else: RUNTIME.chmod(0o700)
    path = RUNTIME / 'secrets.json'
    if not path.exists():
        path.write_text(json.dumps({k: secrets.token_hex(24) for k in ('api_key','db_password','admin_password','ajp_secret')}))
    c = credentials()
    # Restrict generated secrets to current Windows account, or owner on POSIX.
    for file in [path]:
        if os.name == 'nt':
            run('icacls', str(file), '/inheritance:r', '/grant:r', f'{os.environ["USERDOMAIN"]}\\{os.environ["USERNAME"]}:(F)', capture=True)
        else:
            file.chmod(0o600)
    sql = f"""CREATE ROLE poc_app LOGIN PASSWORD '{c['db_password']}';
CREATE TABLE companies(id bigint PRIMARY KEY, name text NOT NULL, registration_number text NOT NULL);
INSERT INTO companies VALUES (1,'Demo Company A','DEMO-001'),(2,'Demo Company B','DEMO-002'),(3,'Demo Company C','DEMO-003');
GRANT CONNECT ON DATABASE deployment_poc TO poc_app;
GRANT USAGE ON SCHEMA public TO poc_app;
GRANT SELECT ON companies TO poc_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER ROLE poc_app SET default_transaction_read_only=on;
"""
    (RUNTIME / 'init.sql').write_text(sql, encoding='utf-8')
    common = {'POC_DB_URL':'jdbc:postgresql://postgres:5432/deployment_poc', 'POC_DB_PASSWORD':c['db_password'],
              'POC_API_KEY':c['api_key'], 'POC_AJP_SECRET':c['ajp_secret'], 'POC_VERSION':'v1', 'POC_PROCESSING_MS':'100'}
    services = {
        'postgres': {'image':'postgres:13.23-bookworm', 'command':['postgres','-c','max_connections=600'],
                     'networks':{'default':{},**{'db-'+m:{} for m in EXPERIMENTS}},
                     'environment':{'POSTGRES_DB':'deployment_poc','POSTGRES_PASSWORD':c['admin_password']},
                     'ports':['127.0.0.1:15432:5432'], 'volumes':['db:/var/lib/postgresql/data',f'{(RUNTIME / "init.sql").as_posix()}:/docker-entrypoint-initdb.d/01.sql:ro'],
                     'healthcheck':{'test':['CMD-SHELL','pg_isready -U postgres -d deployment_poc'],'interval':'2s','timeout':'3s','retries':60}},
        'k3s': {'image':'rancher/k3s:v1.36.5-k3s1@sha256:81fc7349ba265f842f9b67311fabd9e7d72698798ba513b7cdf41ffdf1e923e4','container_name':CLUSTER,'hostname':'poc-k3s','privileged':True,'profiles':['k3s'],
                'command':['server','--disable=traefik,servicelb,metrics-server,local-storage','--write-kubeconfig-mode=600'],
                'tmpfs':['/run','/var/run'], 'volumes':['k3s:/var/lib/rancher/k3s'],
                'networks':{'default':{'aliases':['poc-k3s']}}},
    }
    for i in (1,2):
        services[f'tomcat{i}'] = {'image':IMAGE,'profiles':['legacy'],'environment':dict(common,POC_INSTANCE=f'tomcat{i}'),
            'ports':[f'127.0.0.1:{18090+i}:8080'], 'stop_grace_period':'45s','mem_limit':'1g','cpus':1.0}
        services[f'apache{i}'] = {'image':'deployment-poc-apache:local','environment':{'POC_WORKERS':'tomcat1,tomcat2','POC_AJP_SECRET':c['ajp_secret']},
            'ports':[f'127.0.0.1:{18080+i}:80']}
    services['haproxy'] = {'image':'haproxy:3.0.11-alpine','ports':['127.0.0.1:18080:8080'],
        'volumes':[f'{(ROOT / "infra/haproxy.cfg").as_posix()}:/usr/local/etc/haproxy/haproxy.cfg:ro']}
    config = {'services':services,'volumes':{'db':{},'k3s':{}},'networks':{'default':{'name':'deployment-poc'},
              **{'db-'+m:{'external':True,'name':f'deployment-poc-{m}_default'} for m in EXPERIMENTS}}}
    COMPOSE.write_text(json.dumps(config,indent=2),encoding='utf-8')
    for file in (RUNTIME / 'init.sql', COMPOSE):
        if os.name == 'nt':
            run('icacls',str(file),'/inheritance:r','/grant:r',f'{os.environ["USERDOMAIN"]}\\{os.environ["USERNAME"]}:(F)',capture=True)
        else: file.chmod(0o600)

def frontend(mode):
    # An override changes only the same two frontends; HAProxy is shared.
    c=credentials()
    override={'services':{f'apache{i}':{'environment':{'POC_WORKERS':'tomcat1,tomcat2' if mode=='legacy' else 'poc-k3s',
              'POC_AJP_PORT':'8009' if mode=='legacy' else '30080','POC_AJP_SECRET':c['ajp_secret']}} for i in (1,2)}}
    file=RUNTIME/'front.json'
    file.write_text(json.dumps(override))
    run('docker','compose','-p','deployment-poc','-f',str(COMPOSE),'-f',str(file),'up','-d','--force-recreate','apache1','apache2','haproxy')
    (RUNTIME/'mode').write_text(mode)

def wait_http(port=18080, version=None, timeout=180):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/health/ready',timeout=4) as r:
                data=json.load(r)
                if r.status==200 and (version is None or data.get('version')==version): return
        except Exception: pass
        time.sleep(.25)
    raise TimeoutError(f'readiness failed on {port}')

def manifest(version):
    c=credentials()
    ip=run('docker','inspect','-f','{{(index .NetworkSettings.Networks "deployment-poc").IPAddress}}','deployment-poc-postgres-1',capture=True).strip()
    env={'POC_DB_URL':f'jdbc:postgresql://{ip}:5432/deployment_poc','POC_DB_PASSWORD':c['db_password'],'POC_API_KEY':c['api_key'],'POC_AJP_SECRET':c['ajp_secret']}
    probe={'httpGet':{'path':'/health/ready','port':8080},'periodSeconds':2,'timeoutSeconds':4,'failureThreshold':3}
    container={'name':'api','image':IMAGE,'imagePullPolicy':'Never','ports':[{'containerPort':8009},{'containerPort':8080}],
               'envFrom':[{'secretRef':{'name':'poc'}}], 'env':[{'name':'POC_VERSION','value':version}],
               'readinessProbe':probe,'startupProbe':dict(probe,failureThreshold=90),
               'resources':{'requests':{'cpu':'250m','memory':'768Mi'},'limits':{'cpu':'1','memory':'1Gi'}},
               'securityContext':{'runAsNonRoot':True,'runAsUser':10001,'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']}},
               'lifecycle':{'preStop':{'exec':{'command':['sh','-c','echo POC_PRESTOP_BEGIN; sleep 5; echo POC_PRESTOP_END']}}}}
    return {'apiVersion':'v1','kind':'List','items':[
      {'apiVersion':'v1','kind':'Namespace','metadata':{'name':'deployment-poc'}},
      {'apiVersion':'v1','kind':'Secret','metadata':{'name':'poc','namespace':'deployment-poc'},'stringData':env},
      {'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':'demo-api','namespace':'deployment-poc'},'spec':{
        'replicas':2,'minReadySeconds':5,'progressDeadlineSeconds':300,'strategy':{'type':'RollingUpdate','rollingUpdate':{'maxSurge':1,'maxUnavailable':0}},
        'selector':{'matchLabels':{'app':'demo-api'}},'template':{'metadata':{'labels':{'app':'demo-api'}},'spec':{'terminationGracePeriodSeconds':45,'containers':[container]}}}},
      {'apiVersion':'v1','kind':'Service','metadata':{'name':'demo-api','namespace':'deployment-poc'},'spec':{
        'type':'NodePort','selector':{'app':'demo-api'},'ports':[{'name':'ajp','port':8009,'targetPort':8009,'nodePort':30080}]}}]}

def wait_roll(version, old=(), deadline=None):
    deadline=deadline or time.monotonic()+300
    remaining=max(1,int(deadline-time.monotonic()))
    kubectl('rollout','status','deployment/demo-api',f'--timeout={remaining}s',timeout=remaining+5)
    while time.monotonic()<deadline:
        pods=json.loads(kubectl('get','pods','-l','app=demo-api','-o','json',capture=True))['items']
        if len(pods)==2 and not any(p['metadata']['uid'] in old for p in pods):
            for p in pods:
                if not any(c['type']=='Ready' and c['status']=='True' for c in p.get('status',{}).get('conditions',[])):break
                try:
                    text=kubectl('exec',p['metadata']['name'],'--','bash','-c',
                        'exec 3<>/dev/tcp/127.0.0.1/8080; printf "GET /health/ready HTTP/1.0\r\nHost: localhost\r\n\r\n" >&3; cat <&3',capture=True,timeout=10)
                except (subprocess.CalledProcessError,subprocess.TimeoutExpired):break
                if '200 ' not in text or f'"version":"{version}"' not in text: break
            else: return
        time.sleep(1)
    raise TimeoutError('old Pod removal / new Pod readiness not confirmed')

def start(mode):
    ensure_networks()
    compose('up','-d','--wait','postgres')
    if mode=='legacy':
        existing=compose('--profile','k3s','ps','-q','k3s',capture=True).strip()
        if existing and kubectl('get','deployment','demo-api','--ignore-not-found','-o','name',capture=True).strip():
            kubectl('scale','deployment/demo-api','--replicas=0')
            kubectl('wait','--for=delete','pod','-l','app=demo-api','--timeout=90s')
        compose('--profile','legacy','up','-d','tomcat1','tomcat2')
        wait_http(18091); wait_http(18092)
    else:
        compose('--profile','legacy','stop','tomcat1','tomcat2')
        compose('--profile','k3s','up','-d','k3s')
        end=time.monotonic()+180
        while True:
            try:
                run('docker','exec',CLUSTER,'kubectl','get','nodes',capture=True,timeout=10)
                break
            except subprocess.CalledProcessError:
                if time.monotonic()>end: raise
                time.sleep(2)
        archive=RUNTIME/'api.tar'
        run('docker','save','-o',str(archive),IMAGE)
        run('docker','cp',str(archive),f'{CLUSTER}:/tmp/api.tar')
        run('docker','exec',CLUSTER,'ctr','-n','k8s.io','images','import','/tmp/api.tar')
        run('docker','exec',CLUSTER,'rm','/tmp/api.tar')
        kubectl('apply','-f','-',input=json.dumps(manifest('v1')))
        wait_roll('v1')
    frontend(mode)
    wait_http()

def roll(version):
    deadline=time.monotonic()+300
    mode=(RUNTIME/'mode').read_text().strip()
    if mode=='legacy':
        for i in (1,2):
            compose('stop',f'tomcat{i}',timeout=max(1,deadline-time.monotonic()))
            override=RUNTIME/'versions.json'
            versions=json.loads(override.read_text()) if override.exists() else {'services':{}}
            versions['services'][f'tomcat{i}']={'environment':{'POC_VERSION':version}}
            override.write_text(json.dumps(versions))
            run('docker','compose','-p','deployment-poc','-f',str(COMPOSE),'-f',str(override),'--profile','legacy','up','-d','--no-deps','--force-recreate',f'tomcat{i}',timeout=max(1,deadline-time.monotonic()))
            wait_http(18090+i,version,timeout=max(0,deadline-time.monotonic()))
    else:
        data=json.loads(kubectl('get','deployment','demo-api','-o','json',capture=True))
        current=next(e['value'] for e in data['spec']['template']['spec']['containers'][0]['env'] if e['name']=='POC_VERSION')
        if current==version: raise ValueError('same version; no rollout requested')
        old=[p['metadata']['uid'] for p in json.loads(kubectl('get','pods','-l','app=demo-api','-o','json',capture=True))['items']]
        kubectl('set','env','deployment/demo-api',f'POC_VERSION={version}')
        wait_roll(version,old,deadline)

def main():
    p=argparse.ArgumentParser()
    p.add_argument('action',choices=['prepare','build','start','roll','stop','status'])
    p.add_argument('--mode',choices=['legacy','k3s'],default='legacy')
    p.add_argument('--version',choices=['v1','v2'],default='v2')
    a=p.parse_args()
    if a.action=='prepare': configure(); compose('--profile','legacy','--profile','k3s','config','--quiet')
    elif a.action=='build':
        run('docker','build','-f','infra/Dockerfile.app','-t',IMAGE,'.')
        run('docker','build','-f','infra/Dockerfile.apache','-t','deployment-poc-apache:local','.')
    elif a.action=='start': start(a.mode)
    elif a.action=='roll': roll(a.version)
    elif a.action=='stop': compose('--profile','legacy','--profile','k3s','stop')
    else: compose('--profile','legacy','--profile','k3s','ps')

if __name__=='__main__': main()
