import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import batch

class BatchProtocolTests(unittest.TestCase):
    def simulate(self,complete,count=5):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'results').mkdir();calls=[]
            async def measurement(**kwargs):
                calls.append(kwargs);(root/'results'/f'run-{len(calls)}').mkdir()
                return {'deploymentComplete':complete,'loadValid':False,'overall':{'unsent':1}}
            with patch.object(batch.poc,'ROOT',root),patch.object(batch.variants,'start'),patch.object(batch.load,'measure',side_effect=measurement),patch.object(batch.urllib.request,'urlopen',side_effect=lambda *a,**k:io.StringIO('{"version":"v1"}')),patch.object(sys,'argv',['batch.py','--mode','http','--count',str(count)]):
                batch.main()
            report=next((root/'results').glob('batch-*/comparison.json'))
            data=json.loads(report.read_text());return calls,data
    def test_invalid_load_is_retained_without_replacement(self):
        calls,data=self.simulate(True)
        self.assertEqual(len(calls),5);self.assertEqual(len(data['runs']),5)
        self.assertTrue(all(not r['summary']['loadValid'] for r in data['runs']))
    def test_ten_requested_runs_are_retained_without_replacement(self):
        calls,data=self.simulate(True,10)
        self.assertEqual(len(calls),10);self.assertEqual([r['run'] for r in data['runs']],list(range(1,11)))
        self.assertTrue(all(not r['summary']['loadValid'] for r in data['runs']))
    def test_incomplete_deployment_stops_remaining_runs(self):
        calls,data=self.simulate(False)
        self.assertEqual(len(calls),1);self.assertEqual(len(data['runs']),1)

if __name__=='__main__':unittest.main()
