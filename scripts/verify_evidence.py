"""Audit the published synthetic experiment without Docker or new traffic."""
from collections import Counter, defaultdict
import csv
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import re
import statistics
import compare

ROOT = Path(__file__).resolve().parents[1]


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    manifest = read_json(ROOT / 'SHA256SUMS.json')
    for name, expected in manifest.items():
        path = (ROOT / name).resolve()
        require(path.is_relative_to(ROOT), 'unsafe manifest path: ' + name)
        require(path.is_file(), 'missing published file: ' + name)
        require(hashlib.sha256(path.read_bytes()).hexdigest() == expected, 'published file changed: ' + name)

    bench = ROOT / 'evidence/benchmark'
    runs = read_json(bench / 'runs.json')
    pooled = read_json(bench / 'aggregate.json')
    protocol = read_json(bench / 'protocol.json')
    completion = read_json(bench / 'completion.json')
    index = {x['folder']: x for x in read_json(bench / 'raw-index.json')}
    modes = {'legacy', 'k3s', 'bluegreen', 'rolling'}
    require(completion['status'] == 'complete' and completion['initialServicesRestored'], 'experiment not complete')
    require(set(protocol['plannedModes']) == modes and not protocol['pilot'], 'wrong formal protocol')
    require(len(runs) == len(index) == len({r['resultFolder'] for r in runs}) == 40, 'forty unique results required')
    require(Counter(r['mode'] for r in runs) == dict.fromkeys(modes, 10), 'ten per mode required')
    require(compare.fingerprint() == protocol['sourceFingerprint'], 'measured source fingerprint differs')
    all_times = defaultdict(list)
    totals = defaultdict(Counter)
    durations = defaultdict(list)
    raw_rows = 0
    for run in runs:
        mode = run['mode']
        folder = ROOT / 'evidence/runs' / run['resultFolder']
        record = index[run['resultFolder']]
        require(record['mode'] == mode and record['run'] == run['run'], 'raw index mismatch')
        archive = (folder / 'requests.csv.gz').read_bytes()
        require(hashlib.sha256(archive).hexdigest() == record['archiveSha256'], 'archive hash differs')
        raw = gzip.decompress(archive)
        require(len(raw) == record['uncompressedBytes'], 'raw length differs')
        require(hashlib.sha256(raw).hexdigest() == record['requestsCsvSha256'], 'original CSV hash differs')
        summary = read_json(folder / 'summary.json')
        require(summary == run['summary'], 'stored summaries differ')
        require(all(summary[k] for k in ['loadValid', 'baselineValid', 'deploymentComplete', 'liveMonitorHealthy']), 'invalid run')
        for key in ['rps', 'warmupSeconds', 'baselineSeconds', 'postDeploySeconds', 'thresholdMs', 'requestTimeoutSeconds']:
            require(summary['protocol'][key] == protocol[key], 'protocol differs: ' + key)
        rows = list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
        raw_rows += len(rows)
        require(len(rows) == len({r['request_id'] for r in rows}), 'duplicate request identity')
        require(all(r['result'] != 'unsent' for r in rows), 'unsent request')
        begin = next(e['offset'] for e in summary['events'] if e['event'] == 'deploy_start')
        end = next(e['offset'] for e in summary['events'] if e['event'] == 'deploy_end')
        durations[mode].append(end - begin)
        for row in rows:
            sent = float(row['sent_offset'])
            expected_phase = 'warmup' if sent < protocol['warmupSeconds'] else 'before' if sent < begin else 'during' if sent < end else 'after'
            require(row['phase'] == expected_phase, 'phase not based on send time')
            elapsed = float(row['elapsed_ms'])
            require(elapsed >= 0 and float(row['complete_offset']) >= sent, 'invalid completion timing')
            if row['result'] in ('normal', 'delayed'):
                require(row['http_status'] == '200' and not row['error'], 'success classification mismatch')
                require((elapsed > protocol['thresholdMs']) == (row['result'] == 'delayed'), 'latency classification mismatch')
            require(row['version'] in (run['fromVersion'], run['toVersion']), 'unknown version')
            if row['phase'] == 'after':
                require(row['version'] == run['toVersion'], 'old version after completed deploy')
        for phase in ('warmup', 'before', 'during', 'after'):
            selected = [r for r in rows if r['phase'] == phase]
            counts = Counter(r['result'] for r in selected)
            require(len(selected) == summary[phase]['scheduled'] == summary[phase]['sent'], 'request count mismatch')
            for result in ('normal', 'delayed', 'failed'):
                require(counts[result] == summary[phase][result], 'phase counts differ')
            if phase == 'during':
                totals[mode].update(counts)
                all_times[mode].extend(float(r['elapsed_ms']) for r in selected if r['result'] in ('normal', 'delayed'))
    for mode, aggregate in pooled.items():
        values = sorted(all_times[mode])
        require(aggregate['sent'] == sum(totals[mode].values()), 'pooled request count mismatch')
        require(aggregate['failed'] == totals[mode]['failed'] and aggregate['delayed'] == totals[mode]['delayed'], 'pooled classifications mismatch')
        require(aggregate['p99'] == values[math.ceil(.99 * len(values)) - 1], 'pooled nearest-rank p99 mismatch')
        require(aggregate['medianDeploymentSeconds'] == statistics.median(durations[mode]), 'deployment median mismatch')
        require(aggregate['valid'] == aggregate['complete'] == aggregate['attempts'] == 10, 'invalid pooled counts')
    for doc in [ROOT / 'README.md', *sorted((ROOT / 'docs').glob('*.md'))]:
        for link in re.findall(r'\]\(([^)]+)\)', doc.read_text(encoding='utf-8')):
            if link.startswith(('http:', 'https:', '#')):
                continue
            target = (doc.parent / link.split('#', 1)[0]).resolve()
            require(target.is_relative_to(ROOT) and target.exists(), 'broken public link: ' + link)
    from verify_failover_evidence import verify as verify_failover
    causal=verify_failover()
    print(json.dumps({'diagnosticRunsVerified':causal['attempts'],'publishedFilesVerified': len(manifest), 'runs': len(runs), 'rawRows': raw_rows,
        'duringRequests': sum(a['sent'] for a in pooled.values()), 'hashesAndCountsAndP99': 'PASS'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
