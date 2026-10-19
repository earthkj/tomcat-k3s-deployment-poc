"""Read-only audit of request-level causal evidence; no Docker or traffic."""
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from datetime import datetime, timezone
import diagnose_failover as diagnostic

ROOT=Path(__file__).resolve().parents[1]
DEFAULT=ROOT/'evidence/failover-20261019'


def read(path):return json.loads(path.read_text(encoding='utf-8'))


def verify(root=DEFAULT,check_summary=True):
    protocol=read(root/'protocol.json');completion=read(root/'completion.json')
    assert protocol['warSha256']==read(ROOT/'evidence/benchmark/runtime.json')['warSha256'],'app WAR differs from original comparison'
    assert completion['complete'] and completion['attempts']==len(protocol['cases'])*protocol['count']==9
    for name,digest in protocol['sourceHashes'].items():
        path=(ROOT/name).resolve();assert path.is_relative_to(ROOT)
        assert hashlib.sha256(path.read_bytes()).hexdigest()==digest,'diagnostic source changed: '+name
    original=read(root/'analysis.json');by_key={(r['case'],r['run']):r for r in original}
    assert len(original)==len(by_key)==9
    cases={case:Counter() for case in protocol['cases']};seen=Counter()
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or not (folder/'protocol.json').exists():continue
        saved=read(folder/'analysis.json');case=saved['case'];seen[case]+=1
        # Analyze a temporary copy so an audit cannot mutate published measurements.
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)
            for name in ['protocol.json','requests.json','events.json',*[s+'.log' for s in diagnostic.SERVICES]]:shutil.copyfile(folder/name,target/name)
            actual=diagnostic.analyze(target)
        assert saved==actual==by_key[(case,saved['run'])],'raw logs and stored analysis differ'
        assert not saved['error'] and not saved['background'].get('unsent',0)
        if saved['probe']:assert saved['probe']['confirmed']
        if case=='sigkill':
            events=read(folder/'events.json');state=next(e['state'] for e in events if e['event']=='kill_ack')
            finished=datetime.strptime(state['FinishedAt'][:26],'%Y-%m-%dT%H:%M:%S.%f').replace(tzinfo=timezone.utc).timestamp()*1000
            trace=saved['probe']['tomcatTrace']
            begin_a=min(float(t['wall_ms']) for t in trace if t['event']=='BEGIN' and t['instance']=='tomcat1')
            begin_b=min(float(t['wall_ms']) for t in trace if t['event']=='BEGIN' and t['instance']=='tomcat2')
            assert begin_a<finished<begin_b,'target process exit must occur between the two backend starts'
        c=cases[case];c['runs']+=1;c['confirmedProbeRuns']+=bool(saved['probe'].get('confirmed'))
        for key,value in saved['background'].items():c[key]+=value
        c['backgroundSent']+=sum(v for k,v in saved['background'].items() if k!='unsent')
        c['inFlightReplayedBackgroundRequests']+=saved['inFlightReplayedBackgroundRequests']
        c['newRequestsAfterExclude']+=saved['probe'].get('newRequestsSentAfterExclude',0)
        c['newRequestsOnExcludedTarget']+=saved['probe'].get('newRequestsReachedExcludedTarget',0)
        c['newRequestsCompletedOnSurvivor']+=saved['probe'].get('newRequestsCompletedOnSurvivor',0)
        access={};ends=defaultdict(list)
        for service in ['apache1','apache2']:
            for line in (folder/(service+'.log')).read_text(encoding='utf-8').splitlines():
                value=diagnostic.fields(line,'POC_APACHE')
                if value:access[value['id']]=value
        for service in ['tomcat1','tomcat2']:
            for line in (folder/(service+'.log')).read_text(encoding='utf-8').splitlines():
                value=diagnostic.fields(line,'POC_TRACE')
                if value and value['event']=='END':ends[value['id']].append(value)
        events=read(folder/'events.json');baseline=next(e['offset'] for e in events if e['event']=='baseline_end')
        rows=read(folder/'requests.json');assert len(rows)==len({row['id'] for row in rows}),'duplicate client invocation ID'
        for row in rows:
            if row['role']!='background':continue
            if row.get('sent_offset') is not None and row['sent_offset']<baseline:assert row['valid'] and row['elapsed_ms']<=300,'unstable diagnostic baseline'
            if not row.get('valid') or row.get('elapsed_ms',0)<=300:continue
            a=access.get(row['id'],{})
            changed=a.get('first') not in (None,'-') and a.get('last') not in (None,'-',a.get('first'))
            slow=any(float(t['elapsed_ms'])>300 and t['instance']==row['instance'] for t in ends[row['id']])
            c['delayedWithWorkerChange']+=changed;c['delayedWithServerSpanOver300Ms']+=slow
            c['delayedWithBothMarkers']+=changed and slow;c['delayedWithoutEitherMarker']+=not changed and not slow
    assert seen==Counter({case:protocol['count'] for case in protocol['cases']})
    summary={'attempts':9,'backgroundOnly':True,'probeExcludedFromLatencyMetrics':True,'cases':{case:dict(values) for case,values in cases.items()}}
    if check_summary:assert read(root/'summary.json')==summary,'summary differs'
    return summary


if __name__=='__main__':
    print(json.dumps(verify(),ensure_ascii=False,indent=2))
