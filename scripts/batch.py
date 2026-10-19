"""A bounded deployment-run protocol; keeps invalid runs, never retries to improve results."""
import argparse
import asyncio
import json
from pathlib import Path
import time
import urllib.request
import uuid
import load
import poc
import variants

def main():
    p=argparse.ArgumentParser();p.add_argument('--mode',choices=['core-legacy','k3s',*variants.MODES],required=True);p.add_argument('--count',type=int,default=5);a=p.parse_args()
    if not 1<=a.count<=10:p.error('count must be 1..10')
    root=poc.ROOT/'results'/('batch-'+str(uuid.uuid4()));root.mkdir(parents=True);records=[]
    for number in range(1,a.count+1):
        before=set((poc.ROOT/'results').iterdir())
        try:
            if a.mode in ('core-legacy','k3s'):
                poc.start('legacy' if a.mode=='core-legacy' else 'k3s');port=18080;action=poc.roll;names=None
            else:
                variants.start(a.mode);port=variants.MODES[a.mode][1];action=lambda version:variants.roll(a.mode,version)
                request=urllib.request.Request(f'http://127.0.0.1:{port}/health/ready')
                with urllib.request.urlopen(request,timeout=5) as r:version=json.load(r)['version']
                if version!='v1':action('v1')
                names=[variants.container(a.mode,n) for n in variants.members(a.mode)+['apache1','apache2','haproxy']]
            result=asyncio.run(load.measure(rps=100,deploy=True,version='v2',deploy_action=action,resource_names=names))
            after=set((poc.ROOT/'results').iterdir())-before
            records.append({'run':number,'resultFolders':[p.name for p in after], 'summary':result})
            (root/'comparison.json').write_text(json.dumps({'mode':a.mode,'runs':records},indent=2))
            if not result['deploymentComplete']:break
        except Exception as e:
            records.append({'run':number,'error':type(e).__name__});(root/'comparison.json').write_text(json.dumps({'mode':a.mode,'runs':records},indent=2));raise
    print(root)

if __name__=='__main__':main()
