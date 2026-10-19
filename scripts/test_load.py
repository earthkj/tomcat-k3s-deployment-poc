import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import load

class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def serve(self,response,delay=0):
        async def handler(reader,writer):
            try:
                await reader.readuntil(b'\r\n\r\n')
                await asyncio.sleep(delay)
                writer.write(response);await writer.drain()
            except (asyncio.IncompleteReadError,ConnectionError):pass
            finally:
                writer.close()
                try:await writer.wait_closed()
                except ConnectionError:pass
        server=await asyncio.start_server(handler,'127.0.0.1',0)
        self.addAsyncCleanup(server.wait_closed); self.addCleanup(server.close)
        return server.sockets[0].getsockname()[1]

    def body(self):
        return json.dumps({'company':{'id':1},'version':'v1','instance':'synthetic','serverProcessingMs':100}).encode()

    async def test_content_length_and_contract(self):
        b=self.body(); port=await self.serve(b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(b)).encode()+b'\r\n\r\n'+b)
        status,valid,version,instance,error=await load.request(port,'test','trace')
        self.assertEqual((status,valid,version,instance,error),(200,True,'v1','synthetic',''))

    async def test_chunked_body(self):
        b=self.body(); port=await self.serve(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'+f'{len(b):x}'.encode()+b'\r\n'+b+b'\r\n0\r\n\r\n')
        self.assertTrue((await load.request(port,'test','trace'))[1])

    async def test_wrong_body_is_failure(self):
        for body in (b'{}',b'[]',b'{"company":null}'):
            port=await self.serve(b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
            result=await load.request(port,'test','trace')
            self.assertEqual(result[0],200);self.assertFalse(result[1])

    async def test_html_error_preserves_http_status(self):
        port=await self.serve(b'HTTP/1.1 503 Unavailable\r\nContent-Length: 5\r\n\r\nERROR')
        result=await load.request(port,'test','trace')
        self.assertEqual((result[0],result[1],result[4]),(503,False,'http_status'))

    async def test_entire_body_timeout(self):
        port=await self.serve(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n',delay=.2)
        with self.assertRaises(asyncio.TimeoutError): await load.request(port,'test','trace',timeout=.02)

    async def test_oversize_rejected(self):
        port=await self.serve(b'HTTP/1.1 200 OK\r\nContent-Length: 9999999\r\n\r\n')
        with self.assertRaisesRegex(ValueError,'response_too_large'): await load.request(port,'test','trace')

    async def test_fixed_arrivals_do_not_wait_for_response(self):
        starts=[]
        async def slow(*args):
            starts.append(asyncio.get_running_loop().time()); await asyncio.sleep(.15)
            return 200,True,'v1','fake',''
        with tempfile.TemporaryDirectory() as folder, patch.object(load,'request',side_effect=slow), patch.object(load.poc,'ROOT',Path(folder)):
            result=await load.measure(rps=20,seconds=.25,key='synthetic')
            self.assertEqual(result['warmup']['sent'],5)
            self.assertLess(starts[1]-starts[0],.12)
            self.assertTrue(result['loadValid'])

class SummaryTests(unittest.TestCase):
    def test_short_protocol_keeps_phase_boundaries_and_failures(self):
        rows=[{'sent_offset':t,'result':kind,'elapsed_ms':ms} for t,kind,ms in [(4,'normal',100),(5,'normal',100),(10,'delayed',400),(11,'failed',30000)]]
        summary=load.summarize(rows,[{'event':'deploy_start','offset':10},{'event':'deploy_end','offset':11}],warmup_seconds=5)
        self.assertEqual([r['phase'] for r in rows],['warmup','before','during','after'])
        self.assertEqual(summary['during']['delayed'],1);self.assertEqual(summary['after']['failed'],1)
        self.assertEqual(summary['overall']['sent'],3)
    def test_send_boundaries_and_unsent(self):
        rows=[{'sent_offset':t,'result':r,'elapsed_ms':ms} for t,r,ms in [(9,'normal',100),(10,'normal',110),(20,'delayed',400),(30,'failed',30000),(None,'unsent',None)]]
        result=load.summarize(rows,[{'event':'deploy_start','offset':20},{'event':'deploy_end','offset':30}])
        self.assertEqual([r['phase'] for r in rows],['warmup','before','during','after','unsent'])
        self.assertEqual(result['overall']['sent'],3)
        self.assertEqual(result['during']['delayed'],1)
        self.assertFalse(result['loadValid'])
        self.assertAlmostEqual(result['overall']['failureRatePercent'],100/3)

if __name__=='__main__': unittest.main()
