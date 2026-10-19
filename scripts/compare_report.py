"""Generate an auditable Markdown report from preserved comparison CSVs."""
import argparse
import csv
import json
import math
from pathlib import Path
import statistics
import load
import poc

LABELS={'legacy':'Tomcat 순차 배포(현행)','k3s':'k3s 컨테이너 롤링','bluegreen':'Tomcat 롤링 — 추가 인스턴스 방식','rolling':'Tomcat 롤링 — 기존 인스턴스 방식'}

def number(value,digits=2):return '—' if value is None else f'{value:,.{digits}f}'
def duration(summary):
    start=next((e['offset'] for e in summary['events'] if e['event']=='deploy_start'),None)
    end=next((e['offset'] for e in summary['events'] if e['event']=='deploy_end'),None)
    return None if start is None or end is None else end-start

def valid(s):return s['deploymentComplete'] and s['loadValid'] and s['baselineValid'] and not s['warmup']['failed'] and s.get('liveMonitorHealthy',True)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('root',type=Path);parser.add_argument('--output',type=Path,default=poc.ROOT/'docs/deployment-comparison-20261017.md');a=parser.parse_args();root=a.root.resolve()
    protocol=json.loads((root/'protocol.json').read_text(encoding='utf-8'));completion=json.loads((root/'completion.json').read_text());runs=json.loads((root/'runs.json').read_text())
    assert completion['status']=='complete' and not protocol.get('pilot'),'formal experiment must finish first'
    assert set(protocol['plannedModes'])==set(LABELS)
    assert all(sum(r['mode']==m for r in runs)==10 for m in LABELS),'ten attempts per mode required'
    runtime=json.loads((root/'runtime.json').read_text())
    from datetime import datetime
    wall_minutes=(datetime.fromisoformat(completion['finishedAt'])-datetime.fromisoformat(protocol['createdAt'])).total_seconds()/60
    rows=[];groups={m:[] for m in LABELS};totals={m:{'times':[],'validTimes':[]} for m in LABELS}
    for r in runs:
        s=r['summary'];during=s['during'];good=valid(s);elapsed=duration(s)
        for k in ['rps','warmupSeconds','baselineSeconds','postDeploySeconds']:assert s['protocol'][k]==protocol[k],(r['mode'],r['run'],k)
        row={'environment':r['mode'],'run':r['run'],'from':r['fromVersion'],'to':r['toVersion'],'deploymentComplete':s['deploymentComplete'],'loadValid':s['loadValid'],'baselineValid':s['baselineValid'],'warmupFailures':s['warmup']['failed'],'valid':good,
          'duringSent':during['sent'],'duringFailed':during['failed'],'duringDelayed':during['delayed'],'overallUnsent':s['overall']['unsent'],'warmupUnsent':s['warmup']['unsent'],
          'duringP95Ms':during['p95_ms'],'duringP99Ms':during['p99_ms'],'deploymentSeconds':elapsed,'resultFolder':r['resultFolder']}
        rows.append(row);groups[r['mode']].append((r,row))
        with (poc.ROOT/'results'/r['resultFolder']/'requests.csv').open(encoding='utf-8-sig',newline='') as f:
            times=[float(x['elapsed_ms']) for x in csv.DictReader(f) if x['phase']=='during' and x['result'] in ('normal','delayed')]
        totals[r['mode']]['times']+=times
        if good:totals[r['mode']]['validTimes']+=times
    with (root/'summary.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    rel='../results/'+root.name
    lines=['# 배포 방식 비교 실험 — 환경별 10회','',f"실험 종료: {completion['finishedAt']} (UTC). 네 환경 총 {len(runs)}회. 요청 조건과 산출물을 고정하고, 실패·미발송 회차도 대체하지 않고 보존했다.",'',f'전체 실행 시간: {number(wall_minutes)}분. 환경 전환·준비·측정·초기 서비스 복원을 포함한다.','', '## 결과 요약','',
      '| 환경 | 완료/시도 | 유효 회차 | 배포 중 발송 | API 실패 | 지연 성공 (>300ms) | 배포 중 성공 p99 (ms) | 배포시간 중앙값 (초) |',
      '|---|---:|---:|---:|---:|---:|---:|---:|']
    pooled={}
    for mode,pairs in groups.items():
        records=[p[1] for p in pairs];sent=sum(x['duringSent'] for x in records);failed=sum(x['duringFailed'] for x in records);delayed=sum(x['duringDelayed'] for x in records)
        durations=[x['deploymentSeconds'] for x in records if x['deploymentSeconds'] is not None];p99=load.percentile(totals[mode]['times'],.99)
        pooled[mode]={'attempts':len(records),'complete':sum(x['deploymentComplete'] for x in records),'valid':sum(x['valid'] for x in records),'sent':sent,'failed':failed,'delayed':delayed,'failureRatePercent':100*failed/sent if sent else None,'delayedRatePercent':100*delayed/sent if sent else None,'zeroImpactRuns':sum(x['valid'] and not x['duringFailed'] and not x['duringDelayed'] for x in records),'p99':p99,'medianDeploymentSeconds':statistics.median(durations) if durations else None,
          'minDeploymentSeconds':min(durations) if durations else None,'maxDeploymentSeconds':max(durations) if durations else None}
        g=pooled[mode];lines.append(f"| {LABELS[mode]} | {g['complete']}/{g['attempts']} | {g['valid']}/10 | {sent:,} | {failed:,} ({number(g['failureRatePercent'])}%) | {delayed:,} ({number(g['delayedRatePercent'])}%) | {number(p99)} | {number(g['medianDeploymentSeconds'])} |")
    lines+=['','위 표는 **무효 회차를 포함한 전체 시도**의 배포 중 구간이다. p99는 회차별 p99의 평균이 아니라 모든 성공 요청의 원본 CSV를 합쳐 nearest-rank로 다시 계산했다. 배포시간은 배포 호출부터 기존 인스턴스 종료·목표 버전 준비 확인까지다. 서로 배포시간이 달라 발송 수가 다르므로 실패 건수와 실패율을 함께 본다.','','### 유효 회차만 집계','', '| 환경 | 유효 회차 | 배포 중 발송 | 실패 | 지연 | 성공 p99 (ms) |','|---|---:|---:|---:|---:|---:|']
    for mode,pairs in groups.items():
        accepted=[x for _,x in pairs if x['valid']]
        lines.append(f"| {LABELS[mode]} | {len(accepted)}/10 | {sum(x['duringSent'] for x in accepted):,} | {sum(x['duringFailed'] for x in accepted):,} | {sum(x['duringDelayed'] for x in accepted):,} | {number(load.percentile(totals[mode]['validTimes'],.99))} |")
    lines+=['','유효 조건은 배포 완료·전체 미발송 0·기준 구간 실패 0 및 p99≤300ms·예열 실패 0·실시간 수집기 정상이다. API 실패/지연이 발생했다고 회차를 숨기거나 재실행하지 않는다. 미발송·예열 오류도 아래 회차별 기록과 CSV에 남긴다.','','## 회차별 결과','', '| 환경 | 회차 | 버전 | 배포 완료 | 유효 | 배포 중 발송 | 실패 | 지연 | 미발송(예열 포함) | p99 (ms) | 배포 (초) | 원본 |','|---|---:|---|---|---|---:|---:|---:|---:|---:|---:|---|']
    for mode,pairs in groups.items():
        for r,x in pairs:
            link='../results/'+x['resultFolder']
            lines.append(f"| {LABELS[mode]} | {x['run']} | {x['from']}→{x['to']} | {'완료' if x['deploymentComplete'] else '미완료'} | {'유효' if x['valid'] else '무효'} | {x['duringSent']:,} | {x['duringFailed']} | {x['duringDelayed']} | {x['overallUnsent']+x['warmupUnsent']} | {number(x['duringP99Ms'])} | {number(x['deploymentSeconds'])} | [CSV]({link}/requests.csv) · [요약]({link}/summary.json) |")
    lines+=['','## 해석과 적용 범위','','유효 회차는 측정 조건 충족을 뜻한다. 별도로 배포 중 **실패 0·300ms 초과 지연 0** 조건을 모두 만족한 유효 회차는 '+', '.join(f"{LABELS[m]} {g['zeroImpactRuns']}/10" for m,g in pooled.items())+'이다.','']
    if pooled['legacy']['delayed'] and all(pooled[m]['zeroImpactRuns']==10 for m in ('k3s','bluegreen','rolling')):
        lines.append('이 부하에서는 k3s 컨테이너 롤링·Tomcat 롤링 — 추가 인스턴스 방식·Tomcat 롤링 — 기존 인스턴스 방식 모두 연속성과 응답 기준을 만족했다. k3s만 가능한 결과라고 해석할 근거는 없으며, 운영 자동화·장애 복구·용량은 별도 평가가 필요하다. Tomcat 순차 배포(현행)는 API 실패가 없어도 지연 기준을 반복 초과했다.')
    if all(x['failed']==0 for x in pooled.values()):lines.append('이번 실행에서는 네 환경 모두 배포 중 API 실패가 0건이었다. 실패가 재현되지 않았다는 관측이며, 모든 운영 조건의 무중단을 보장하는 결론은 아니다.')
    else:lines.append('배포 중 API 실패가 발생한 환경이 있다. 실패율과 지연을 구분하고 원본 요청의 HTTP 상태·오류 분류·배포 이벤트를 함께 확인해야 한다.')
    lines.append('')
    for mode,g in pooled.items():
        lines.append(f"- **{LABELS[mode]}**: 유효 {g['valid']}/10, 실패 {g['failed']:,}건, 지연 {g['delayed']:,}건. 배포시간 중앙값 {number(g['medianDeploymentSeconds'])}초, 범위 {number(g['minDeploymentSeconds'])}~{number(g['maxDeploymentSeconds'])}초.")
    lines+=['','배포 중 실패·지연은 서비스 연속성 지표이고, 배포 완료시간은 교체·검증·종료 절차의 소요시간이다. 두 수치를 같은 의미로 해석하지 않는다. 인스턴스별 1 CPU/1GiB 제한을 사용하지만 종료 중 Pod·surge·k3s 제어 서비스의 자원까지 동등한 총량으로 맞춘 시험은 아니다. Tomcat 롤링 — 추가 인스턴스 방식은 정상 2대·배포 중 최대 3대이고, k3s는 종료 중 Pod가 남을 수 있어 maxSurge=1만으로 전체 Pod 수 3개 상한을 보장하지 않는다.','','동일 PC에서 시험했으며 회사망·세션·쓰기 트랜잭션·DB 스키마 변경·노드 장애·자동 롤백·운영 최대 부하를 검증하지 않았다. 버전은 식별값만 다르고 WAR 내용은 동일하다. 작은 지연 차이를 플랫폼 일반 성능이나 비용 우위로 확대하지 않는다.','', '## 시간 최적화와 공통 조건','',
      f"- 100 RPS. 예열 {protocol['warmupSeconds']}초 → 기준 {protocol['baselineSeconds']}초 → 실제 배포 → 후속 {protocol['postDeploySeconds']}초. 송신 종료 후 남은 요청은 개별 30초 한도까지 수집하며 고정 30초를 추가로 쉬지 않는다.",
      '- GET /api/companies/1 · API 키 인증 · 공통 PostgreSQL 읽기 전용 조회 · 목표 처리시간 100ms(대기 포함)를 사용한다. 현행 순차 배포도 Windows의 Linux Tomcat 컨테이너 순차 재생성 방식이며 Rocky의 파일 복사 시간이나 운영 배포 자체를 복제한 측정은 아니다.',
      '- 환경별 10회이며 v1/v2를 교대로 배포한다. 반복 사이 별도 v1 복원 배포를 하지 않는다. 네 환경의 위치와 직전 환경 조합을 균형화한 Williams 순서를 사용했다. 순서는 protocol.json과 runs.json에 고정·기록한다. 비측정 앱·앞단은 중지하고 k3s 제어 서비스와 공통 PostgreSQL은 유지한다.',
      '- AJP keepAliveTimeout=1000ms를 모든 비교 환경에 공통 적용했다. 처리 중 요청의 gracefulStopAwaitMillis=30000, 요청 제한 30초, JK busy=0 연속 2초 확인, Pod preStop 5초·종료 유예 45초·minReadySeconds 5초는 유지했다.',
      '- readiness는 성공하면 즉시 통과하며 불필요한 고정 대기를 두지 않는다. 이미지 빌드·전달과 초기 준비는 측정 밖에서 수행했다. 외부 설정은 본 실험 중 변경하지 않았다.',
      '- 유휴 제한 최적화의 별도 확인에서 3초 처리 중 요청이 정상 완료됐고, 드레이닝 후 종료는 약 0.69초였다. [검증 원본](../results/optimized-drain-validation-20261017.json).',
      '- 설정 변경 전 예비 두 회차 및 quick 프로토콜 예비 네 회차는 본 실험 40회에 합치지 않았다. 설정·대상 변경에 따른 별도 사전 검증이며 원본은 보존했다.',
      '- 공식 의미: [Tomcat AJP keepAliveTimeout](https://tomcat.apache.org/tomcat-10.1-doc/config/ajp.html)은 다음 AJP 요청을 기다리는 유휴 시간이고, [Service 종료 대기](https://tomcat.apache.org/tomcat-10.1-doc/config/service.html)는 연결 처리·종료를 기다리는 별도 설정이다.',
      '', '## 재현과 증거','',
      f"- [프로토콜]({rel}/protocol.json) · [실행 순서·회차 요약]({rel}/runs.json) · [종료 상태]({rel}/completion.json)",
      f"- [요약 CSV]({rel}/summary.csv) · [런타임·WAR 해시]({rel}/runtime.json)",
      f"- Java: `{runtime['java'].replace(chr(10),' / ')}`. PostgreSQL: `{runtime['postgres']}`. max_connections={runtime['maxConnections']}.",
      f"- 앱 이미지 ID: `{runtime['imageId']}`.",f"- WAR SHA-256: `{runtime['warSha256']}`.",
      '', '```powershell','python scripts/compare.py --modes legacy k3s bluegreen rolling --count 10 --profile quick','```','',
      '내부 모드 bluegreen은 기존 포트·데이터 호환을 위한 식별자이며 현재 전략은 2+2 순차 롤링이다. 과거 Blue/Green 성적을 롤링 결과로 바꾸지 않았다.']
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.font_manager import FontProperties
        font=Path('C:/Windows/Fonts/malgun.ttf')
        if font.exists():
            from matplotlib import font_manager
            font_manager.fontManager.addfont(str(font));plt.rcParams['font.family']=FontProperties(fname=str(font)).get_name()
        plt.rcParams['axes.unicode_minus']=False
        fig,axes=plt.subplots(1,2,figsize=(12,4.6),layout='constrained');palette=['#447c9b','#7362c2','#368e7a','#c08840']
        for index,(mode,pairs) in enumerate(groups.items()):
            xs=[x[1] for x in pairs]
            for panel,key in [(0,'deploymentSeconds'),(1,'duringP99Ms')]:
                values=[x[key] for x in xs if x[key] is not None]
                for j,x in enumerate(xs):
                    if x[key] is not None:axes[panel].scatter(index+(j-4.5)*.025,x[key],color=palette[index] if x['valid'] else '#c53f56',marker='o' if x['valid'] else 'x',s=32,alpha=.8)
                if values:axes[panel].hlines(statistics.median(values),index-.2,index+.2,color='#222',linewidth=2)
        for ax,title,ylabel in [(axes[0],'회차별 배포 완료시간','초'),(axes[1],'회차별 배포 중 성공 p99','ms')]:
            ax.set_xticks(range(4),['Tomcat 순차 배포\n(현행)','k3s 컨테이너\n롤링','Tomcat 롤링\n추가 인스턴스','Tomcat 롤링\n기존 인스턴스']);ax.set_title(title);ax.set_ylabel(ylabel);ax.set_ylim(bottom=0);ax.grid(axis='y',alpha=.18);ax.spines[['top','right']].set_visible(False)
        fig.suptitle('100 RPS · 각 10회 · 검은 선=중앙값 · 빨간 ×=무효 회차',fontsize=12);fig.savefig(root/'comparison.png',dpi=160);plt.close(fig)
        lines+=['',f'![회차별 배포시간과 p99]({rel}/comparison.png)']
    except ImportError:pass
    (root/'aggregate.json').write_text(json.dumps(pooled,ensure_ascii=False,indent=2),encoding='utf-8')
    a.output.write_text('\n'.join(lines)+'\n',encoding='utf-8');print(a.output)

if __name__=='__main__':main()
