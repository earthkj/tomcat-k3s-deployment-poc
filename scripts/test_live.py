import asyncio
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import load

class LiveCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_completion_is_visible_with_unique_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'results').mkdir();control=root/'control'
            call=0
            async def response(port,key,request_id):
                nonlocal call
                call+=1;first=call==1
                await asyncio.sleep(.8 if first else .001)
                return 200,True,'v1','late' if first else 'fast',''
            class Process:
                returncode=0
                async def communicate(self):return b'',b''
            async def spawn(*args,**kwargs):return Process()
            original_replace=Path.replace
            denied=False
            def transient_busy(path,target):
                nonlocal denied
                if path.name=='live.tmp' and not denied:
                    denied=True;raise PermissionError('simulated Windows reader')
                return original_replace(path,target)
            with patch.object(Path,'replace',transient_busy),patch.object(load.poc,'ROOT',root),patch.object(load,'request',side_effect=response),patch.object(load.asyncio,'create_subprocess_exec',side_effect=spawn):
                await load.measure(rps=200,seconds=.65,key='test',control_dir=control,resource_names=[])
            snapshot=json.loads((control/'live.json').read_text())
            self.assertTrue(denied)
            self.assertEqual(snapshot['inflight'],0)
            self.assertEqual(snapshot['instances']['late']['count'],1)
            self.assertEqual(snapshot['instances']['fast']['count'],129)
            self.assertEqual(len(snapshot['recent']),130)
            self.assertEqual(len({r['request_id'] for r in snapshot['recent']}),130)
            self.assertEqual(snapshot['recent'][-1]['instance'],'late')
            self.assertGreater(snapshot['recent'][-1]['complete_offset'],.75)
            self.assertTrue(snapshot['startedAt'].endswith('+00:00'))
            self.assertTrue(snapshot['runId'])
            result=Path((control/'result-path').read_text())
            with (result/'requests.csv').open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
            self.assertEqual(rows[0]['request_id'],snapshot['recent'][-1]['request_id'])
            self.assertEqual(snapshot['normal']+snapshot['delayed'],130)
            self.assertEqual(snapshot['failed'],0)
            self.assertEqual(snapshot['rps'],200)

if __name__=='__main__':unittest.main()
