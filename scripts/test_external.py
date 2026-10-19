from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import csv
import json
import shutil
import ssl
import subprocess
from pathlib import Path
import tempfile
import threading
import time
import unittest
import external

class Synthetic(BaseHTTPRequestHandler):
    hits=0
    last_target=''
    def do_GET(self):
        type(self).hits+=1
        type(self).last_target=self.path
        if self.path=='/redirect':self.send_response(302);self.send_header('Location','/ok');self.send_header('Content-Length','0');self.end_headers();return
        if self.path=='/drop':self.connection.close();return
        body=b'{"messageBody":{"resultCode":0,"optional":null},"success":true,"items":[{"success":true}]}'
        if self.path=='/bad':body=b'not-json'
        if self.path=='/large':body=b'x'*(256*1024+1)
        self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers()
        try:
            if self.path=='/slow':
                for b in body:self.wfile.write(bytes([b]));self.wfile.flush();time.sleep(.02)
            else:self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError,OSError):pass
    def do_POST(self):
        type(self).hits+=1
        body=self.rfile.read(int(self.headers.get('Content-Length','0')))
        ok=self.headers.get('Authorization')=='Bearer TEST_SECRET' and json.loads(body)=={'test':1}
        data=json.dumps({'ok':ok}).encode();self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def log_message(self,*args):pass

class ExternalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Synthetic);threading.Thread(target=cls.server.serve_forever,daemon=True).start()
        cls.url=f'http://127.0.0.1:{cls.server.server_port}'
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close()
    def settings(self,path='/ok',**kw):return dict(url=self.url+path,rps=20,duration=.3,timeoutMs=1000,thresholdMs=300,maxInflight=4,**kw)
    def test_nested_number_and_array_boolean(self):
        for path,value in [('messageBody.resultCode','0.0'),('items[0].success','true'),('messageBody.optional','null')]:
            self.assertEqual(external.exchange(self.settings(jsonPath=path,expectedValue=value)),(200,True,''))
    def test_type_and_missing_field_are_not_equal(self):
        for path,value in [('messageBody.resultCode','"0"'),('success','1'),('messageBody.absent','null')]:
            self.assertFalse(external.exchange(self.settings(jsonPath=path,expectedValue=value))[1])
    def test_post_headers_and_body(self):
        self.assertTrue(external.exchange(self.settings(method='POST',headers={'Authorization':'Bearer TEST_SECRET'},body='{"test":1}',jsonPath='ok',expectedValue='true'))[1])
    def test_redirect_is_not_followed(self):
        before=Synthetic.hits;self.assertEqual(external.exchange(self.settings('/redirect')),(302,False,'http_status'));self.assertEqual(Synthetic.hits-before,1)
    def test_disconnect_is_not_retried(self):
        before=Synthetic.hits;self.assertFalse(external.exchange(self.settings('/drop'))[1]);self.assertEqual(Synthetic.hits-before,1)
    def test_json_error(self):self.assertEqual(external.exchange(self.settings('/bad',jsonPath='success',expectedValue='true'))[2],'invalid_json')
    def test_body_limit(self):self.assertEqual(external.exchange(self.settings('/large'))[2],'response_too_large')
    def test_explicit_http_proxy(self):
        s=self.settings();s.update(url='http://example.invalid/ok',proxyHost='127.0.0.1',proxyPort=self.server.server_port)
        self.assertTrue(external.exchange(s)[1]);self.assertEqual(Synthetic.last_target,'http://example.invalid/ok')
    def test_tls_validation_and_private_ca(self):
        openssl=shutil.which('openssl') or 'C:/Program Files/Git/usr/bin/openssl.exe'
        if not Path(openssl).exists():self.skipTest('OpenSSL unavailable')
        with tempfile.TemporaryDirectory() as folder:
            key=Path(folder)/'key.pem';cert=Path(folder)/'cert.pem'
            subprocess.run([openssl,'req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=poc-test','-addext','subjectAltName=IP:127.0.0.1'],check=True,capture_output=True)
            server=ThreadingHTTPServer(('127.0.0.1',0),Synthetic)
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,key);server.socket=context.wrap_socket(server.socket,server_side=True)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            try:
                s=self.settings();s['url']=f'https://127.0.0.1:{server.server_port}/ok'
                self.assertEqual(external.exchange(s)[2],'tls_error')
                s['caFile']=str(cert);self.assertTrue(external.exchange(s)[1])
            finally:server.shutdown();server.server_close()
    def test_full_body_deadline(self):
        s=self.settings('/slow');s['timeoutMs']=100;begin=time.perf_counter()
        self.assertEqual(external.exchange(s)[2],'timeout');self.assertLess(time.perf_counter()-begin,.6)
    def test_run_stop_rps_markers_and_redaction(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=self.settings(headers={'Authorization':'Bearer TEST_SECRET'},body='SECRET_PAYLOAD',jsonPath='success',expectedValue='true')
            settings['duration']=3
            run=external.Run(settings,folder);run.thread.start();time.sleep(.1);run.mark(True);run.change_rps(30);time.sleep(.1);run.mark(False);run.stop();run.thread.join(3)
            self.assertTrue(run.done);summary=run.snapshot()
            self.assertEqual(summary['sent'],summary['normal']+summary['delayed']+summary['failed'])
            with (run.folder/'requests.csv').open(encoding='utf-8-sig') as stream:rows=list(csv.DictReader(stream))
            self.assertEqual(len(rows),summary['sent']+summary['unsent'])
            text=''.join(p.read_text(encoding='utf-8-sig') for p in run.folder.iterdir())
            for secret in ['TEST_SECRET','SECRET_PAYLOAD',self.url]:self.assertNotIn(secret,text)
            self.assertTrue(any(e['event']=='rps_change' for e in summary['events']))
    def test_inflight_limit_is_unsent(self):
        with tempfile.TemporaryDirectory() as folder:
            s=self.settings('/slow');s.update(rps=100,duration=.15,maxInflight=1,timeoutMs=200)
            run=external.Run(s,folder);run.thread.start();run.thread.join(3)
            self.assertGreater(run.counts['unsent'],0);self.assertFalse(run.snapshot()['loadValid'])
    def test_duration_and_finished_elapsed_are_stable(self):
        with tempfile.TemporaryDirectory() as folder:
            s=self.settings();s.update(rps=1,duration=.25)
            run=external.Run(s,folder);run.thread.start();run.thread.join(3)
            self.assertTrue(run.done);elapsed=run.snapshot()['elapsed'];self.assertGreaterEqual(elapsed,.25)
            time.sleep(.03);self.assertEqual(run.snapshot()['elapsed'],elapsed)

if __name__=='__main__':unittest.main()
