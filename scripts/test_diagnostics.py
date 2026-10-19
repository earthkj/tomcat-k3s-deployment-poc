"""Ensure causal conclusions fail closed when request-level evidence is missing."""
import json
from pathlib import Path
import tempfile
import unittest
import diagnose_failover as diagnostic


class CausalEvidenceTests(unittest.TestCase):
    def fixture(self,folder,case):
        probe='probe-fixture'
        def put(name,value): (folder/name).write_text(json.dumps(value),encoding='utf-8')
        put('protocol.json',{'case':case,'run':1,'probeId':probe,'error':None})
        put('requests.json',[{'id':probe,'role':'probe','valid':True,'instance':'tomcat2' if case=='sigkill' else 'tomcat1','elapsed_ms':210,'sent_offset':0,'wall_ms':990}, {'id':'bg-new','role':'background','valid':True,'instance':'tomcat2','elapsed_ms':100,'sent_offset':1,'wall_ms':1200}])
        if case=='sigkill':
            put('events.json',[{'event':'kill_request','wall_ms':1050},{'event':'kill_ack','wall_ms':1150,'state':{'ExitCode':137}}])
            first='POC_TRACE event=BEGIN wall_ms=1000 id=probe-fixture instance=tomcat1 active=1\n'
            second='POC_TRACE event=BEGIN wall_ms=1100 id=probe-fixture instance=tomcat2 active=1\nPOC_TRACE event=END wall_ms=1200 id=probe-fixture instance=tomcat2 active=0 status=200\n'
            last='tomcat2'
        else:
            put('events.json',[{'event':'exclude_ack','wall_ms':1100},{'event':'drain_complete','wall_ms':4000}])
            first='POC_TRACE event=BEGIN wall_ms=1000 id=probe-fixture instance=tomcat1 active=1\nPOC_TRACE event=END wall_ms=3500 id=probe-fixture instance=tomcat1 active=0 status=200\n'
            second='';last='tomcat1'
        second+='POC_TRACE event=BEGIN wall_ms=1201 id=bg-new instance=tomcat2 active=1\nPOC_TRACE event=END wall_ms=1301 id=bg-new instance=tomcat2 active=0 status=200\n'
        for name,text in {'tomcat1.log':first,'tomcat2.log':second,'apache1.log':'POC_APACHE id=probe-fixture status=200 first=tomcat1 last='+last+'\n','apache2.log':'','haproxy.log':'POC_HAPROXY id=probe-fixture status=200 retries=0\n'}.items():(folder/name).write_text(text,encoding='utf-8')

    def test_complete_sigkill_chain_is_confirmed(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);self.fixture(p,'sigkill');self.assertTrue(diagnostic.analyze(p)['probe']['confirmed'])

    def test_missing_survivor_completion_cannot_prove_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);self.fixture(p,'sigkill');f=p/'tomcat2.log';f.write_text('\n'.join(x for x in f.read_text().splitlines() if 'END' not in x or 'probe-fixture' not in x));self.assertFalse(diagnostic.analyze(p)['probe']['confirmed'])

    def test_duplicate_client_call_cannot_prove_no_client_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);self.fixture(p,'sigkill');f=p/'requests.json';rows=json.loads(f.read_text());rows.append(dict(rows[0]));f.write_text(json.dumps(rows));self.assertFalse(diagnostic.analyze(p)['probe']['confirmed'])

    def test_complete_drain_chain_is_confirmed(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);self.fixture(p,'drain_stop');self.assertTrue(diagnostic.analyze(p)['probe']['confirmed'])

    def test_missing_post_exclusion_completion_cannot_prove_safe_routing(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);self.fixture(p,'drain_stop');(p/'tomcat2.log').write_text('');self.assertFalse(diagnostic.analyze(p)['probe']['confirmed'])

    def test_missing_apache_access_is_not_vacuous_success(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);self.fixture(p,'drain_stop');(p/'apache1.log').write_text('');self.assertFalse(diagnostic.analyze(p)['probe']['confirmed'])


if __name__=='__main__':unittest.main()
