(()=>{'use strict';
const cfg=window.POC_CONFIG,mode=cfg.mode,$=s=>document.querySelector(s),all=s=>[...document.querySelectorAll(s)];
const names={k3s:'k3s 컨테이너 롤링',legacy:'Tomcat 순차 배포(현행)',http:'HTTP 프록시 실험',ajp:'AJP 프록시 실험',bluegreen:'Tomcat 롤링 — 추가 인스턴스 방식',rolling:'Tomcat 롤링 — 기존 인스턴스 방식'};
const ports={k3s:18700,legacy:18701,http:18702,ajp:18703,bluegreen:18704,rolling:18705};
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=n=>Number(n||0).toLocaleString('ko-KR'),clock=t=>{const d=new Date(t);return [d.getHours(),d.getMinutes(),d.getSeconds()].map(n=>String(n).padStart(2,'0')).join(':');};
let state={},backends={},runId='',points=new Map(),events=[],slots=[[],[]],knownPods=new Map(),structure='',noticeTimer,lastSample=0;
$('#title').textContent=names[mode];document.title=names[mode];$('#logo').textContent=mode==='k3s'?'K↗':'L↗';
$('#nav').innerHTML=Object.entries(ports).filter(([m])=>m!==mode).map(([m,p])=>`<a href="http://127.0.0.1:${p}/">${esc(names[m])} ↗</a>`).join('');
$('#scale-controls').hidden=mode!=='k3s';$('#routing').hidden=!['rolling','bluegreen'].includes(mode);$('#deploy').hidden=mode!=='bluegreen';
$('#roll').hidden=mode==='rolling';$('#roll').textContent=mode==='k3s'?'롤링 배포':mode==='bluegreen'?'Tomcat 롤링 — 추가 인스턴스 방식 배포':'순차 배포';
if(mode==='rolling'){$('#intro').hidden=false;$('#intro').textContent='중지 전에 유입을 빼면 어떨까요?';$('#help').textContent='유입 제외 → 처리 중 0 확인 → 중지 → 시작 → 준비 완료 → 유입 복귀. 다른 Tomcat도 같은 순서로 진행합니다. 한 대가 전체 트래픽을 감당할 수 있어야 합니다.';}
else $('#help').textContent=mode==='k3s'?'Pod 1·2 구역은 교체 중 위치를 유지합니다. 기존 Pod의 종료와 새 Pod의 준비 상태·실제 응답을 함께 관측합니다.':'HAProxy → Apache 두 대 → Tomcat → PostgreSQL. 그래프는 실제 클라이언트 응답이며 미발송과 API 실패는 구분합니다.';
if(mode==='bluegreen'){
 $('#deploy .row b').textContent='대체 인스턴스를 한 대씩 준비하고 기존 인스턴스를 순차 교체합니다.';
 $('#deploy-status').textContent='Tomcat 롤링 — 추가 인스턴스 방식 배포 대기';
 $('.traffic .actions').append($('#version').parentElement,$('#roll'));$('#control').hidden=true;
 $('#deploy').append($('#routing'));
}else if(mode!=='k3s'){
 if(mode!=='rolling')document.querySelector('details').append($('#version').parentElement,$('#roll'));
 $('#control').hidden=true;
}
const stepLabels=['① 한 대 교체 준비','② 대체 1대 기동','③ 준비·API 확인','④ 대체 1대 투입','⑤ 기존 1대 제외·대기','⑥ 기존 1대 종료','⑦ 두 대 교체 완료'];
$('#steps').innerHTML=stepLabels.map(x=>`<span>${x}</span>`).join('');
function toast(text){$('#notice').textContent=text;$('#notice').hidden=false;clearTimeout(noticeTimer);noticeTimer=setTimeout(()=>$('#notice').hidden=true,6500);}
async function action(action,extra={}){try{if(action==='send-start'&&mode==='k3s'&&!(backends.pods||[]).some(p=>p.ready))toast('준비된 Pod가 없습니다. 실제 오류가 관측될 수 있습니다.');
const rps=Number($('#rps').value);if(!Number.isFinite(rps)||rps<.1||rps>200){toast('요청률은 0.1~200 RPS 범위로 입력하세요.');return;}
const r=await fetch('/action',{method:'POST',headers:{'Content-Type':'application/json','X-PoC-Token':token},body:JSON.stringify({action,mode,version:$('#version').value,rps:action==='measure'?100:rps,...extra})});
if(!r.ok){toast(r.status===409?'진행 중인 제어·측정이 끝난 뒤 실행하세요.':`제어 요청 실패 (${r.status})`);return;}await poll();}catch{toast('콘솔 연결이 끊겼습니다.');}}
all('[data-action]').forEach(b=>b.onclick=()=>action(b.dataset.action));all('[data-scale]').forEach(b=>b.onclick=()=>action('scale',{replicas:Number(b.dataset.scale)}));
document.addEventListener('click',e=>{const b=e.target.closest('[data-node-action]');if(b)action('node',{node:b.dataset.node,nodeAction:b.dataset.nodeAction});const p=e.target.closest('[data-pod]');if(p)action('pod-replace',{pod:p.dataset.pod});});
function card(name,label,caption=''){return `<article class="card server" data-instance="${esc(name)}"><div class="row"><div><small>${esc(caption)}</small><div><span class="name">${esc(label)}</span><span class="badge" data-health></span></div></div><div class="actions" data-controls></div></div><div class="node-flow" data-flow></div><div class="stats" data-stats>누적 0건 · 최근 5초 0.0 응답/s · 성공 평균 —</div><canvas aria-label="${esc(label)} 실제 응답시간"></canvas></article>`;}
function layout(){if(!backends.observedAt){$('#control-status').textContent=backends.error||'실제 서버 상태 확인 중';$('#servers').innerHTML='<article class="card waiting">실제 서버 상태 확인 중…</article>';structure='';return;}let html='';if(mode==='k3s'){
const ps=backends.pods||[];for(const p of ps)knownPods.set(p.name,p);
if(slots.every(x=>!x.length))ps.slice(0,2).forEach((p,i)=>slots[i]=[p.name]);
const assigned=new Set(slots.flat());const fresh=ps.filter(p=>!assigned.has(p.name));
if(fresh.length&&slots.every(x=>x.length===2)&&slots.every(x=>!ps.some(p=>p.name===x[0]))){slots=slots.map(x=>[x[1]]);}
for(const p of fresh){const row=slots.find(x=>x.length<2);if(row)row.push(p.name);}
html=slots.map((row,i)=>`<section class="pod-zone"><div class="zone-title">Pod ${i+1} 구역</div><div class="server-grid">${row[0]?card(row[0],row[0],row.length===2?'기존 Pod':'현재 Pod'):'<article class="card waiting">Pod 대기</article>'}${row[1]?card(row[1],row[1],'새 Pod'):'<article class="card waiting">새 Pod 대기</article>'}</div></section>`).join('');
}else if(mode==='bluegreen')html=`<div class="server-grid">${['교체 구역 1','교체 구역 2'].map((g,i)=>`<section class="group ${i?'green':''}"><div class="group-heading"><b>${g}</b><span data-group="${g}"></span></div><div class="server-grid">${[1+i,3+i].map(n=>card(mode+'-tomcat'+n,'Tomcat '+n)).join('')}</div></section>`).join('')}</div>`;
else html='<div class="server-grid">'+[1,2].map(n=>card(mode+'-tomcat'+n,'Tomcat '+n)).join('')+'</div>';
if(html!==structure){structure=html;$('#servers').innerHTML=html;}
all('[data-instance]').forEach(el=>{const name=el.dataset.instance;let h;if(mode==='k3s')h=(backends.pods||[]).find(p=>p.name===name)||{...knownPods.get(name),ready:false,ended:true};else h=backends.tomcat?.[name.slice(mode.length+1)]||{};
const badge=el.querySelector('[data-health]');badge.textContent=(h.ended?'교체 완료':h.terminating?'종료 중':h.ready?'준비 완료':h.phase==='Pending'?'시작 중':'준비 안 됨')+' · '+(h.version||'—');badge.className='badge '+(h.terminating?'warn':!h.ready?'off':'');
const node=name.slice(mode.length+1),members=[1,2].flatMap(i=>(backends['apache'+i]||[]).filter(x=>x.name===node));
const active=members.length===2&&members.every(m=>['ACT','Active','ACTIVATED'].includes(m.activation));const excluded=members.length===2&&members.every(m=>['STP','Stopped','STOPPED'].includes(m.activation));
const busy=members.length?members.reduce((n,m)=>n+Number(m.busy||0),0):null;
const controls=el.querySelector('[data-controls]');
if(mode==='k3s')controls.innerHTML=`<button data-pod="${esc(name)}" ${h.ended||h.terminating||state.busy?'disabled':''}>Pod 교체</button>`;
else if(mode==='bluegreen')controls.innerHTML='';
else controls.innerHTML=(['rolling','bluegreen'].includes(mode)?`<button data-node="${node}" data-node-action="${excluded?'include':'exclude'}" ${state.busy?'disabled':''}>${excluded?'유입 복귀':'유입 제외'}</button>`:'')+`<button data-node="${node}" data-node-action="start" ${state.busy||h.ready?'disabled':''}>시작</button><button data-node="${node}" data-node-action="stop" ${state.busy||!h.ready?'disabled':''}>중지</button>`;
el.querySelector('[data-flow]').textContent=members.length?(active?'신규 요청 수신 중':excluded?'신규 유입 제외':'Apache별 유입 상태 확인 필요')+' · 처리 중 '+busy:'';
const stats=state.live?.instances?.[name]||{},recent=[...points.values()].filter(p=>p.instance===name&&p.time>=sampleTime()-5000&&p.time<=sampleTime());
el.querySelector('[data-stats]').textContent=`누적 ${fmt(stats.count)}건 · ${state.live?.runId&&!state.measureBusy?'종료 직전 5초':'최근 5초'} ${(recent.length/5).toFixed(1)} 응답/s · 성공 평균 ${stats.success?(stats.total_ms/stats.success).toFixed(0)+' ms':'—'}`;
});
if(mode==='k3s')$('#control-status').textContent=`k3s 제어 · ${backends.targetVersion||'—'} · Ready ${(backends.pods||[]).filter(p=>p.ready&&!p.terminating).length}/${backends.replicas??'—'} · ${state.busy?'제어 진행 중':'대기'}`;
else $('#control-status').textContent=state.busy?'제어 진행 중 · '+state.operation:'수동 제어 대기';
if(['rolling','bluegreen'].includes(mode)){$('#routing').innerHTML='<b>JK Status · 실제 배분 대상</b>'+[1,2].map(i=>`<span>Apache ${i} → ${esc((backends['apache'+i]||[]).filter(m=>['ACT','Active','ACTIVATED'].includes(m.activation)).map(m=>m.name).join(' + ')||'없음')}</span>`).join('');}
if(mode==='bluegreen')for(const [g,ns] of [['교체 구역 1',['tomcat1','tomcat3']],['교체 구역 2',['tomcat2','tomcat4']]]){const enabled=ns.filter(n=>(backends.apache1||[]).some(m=>m.name===n&&['ACT','Active','ACTIVATED'].includes(m.activation)));$(`[data-group="${g}"]`).textContent=enabled.length?enabled.join(' + ')+' 유입 중':'유입 대기';}
}
function sampleTime(){return state.live?.startedAt?Date.parse(state.live.startedAt)+state.live.elapsed*1000:Date.now();}
function now(){const x=state.live;return x&&!state.measureBusy?Date.parse(x.startedAt)+x.elapsed*1000:Date.now();}
function graph(canvas,samples){const rect=canvas.getBoundingClientRect(),dpr=devicePixelRatio||1,w=rect.width,h=rect.height;if(!w)return;canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);const g=canvas.getContext('2d');g.scale(dpr,dpr);const left=42,right=w-10,top=17,bottom=h-24,t=now(),span=Number($('#window').value)*1000;
const filtered=samples.filter(p=>p.time>=t-span&&p.time<=t),max=Math.max(500,...filtered.map(p=>p.elapsed_ms||0)),limit=Math.ceil(max/250)*250;g.font='10px "Segoe UI"';
const ticks=h<100?2:4;for(let i=0;i<=ticks;i++){const y=bottom-(bottom-top)*i/ticks;g.strokeStyle='#f0f1f6';g.beginPath();g.moveTo(left,y);g.lineTo(right,y);g.stroke();g.fillStyle='#9aa0ae';g.fillText(i?((limit*i/ticks>=1000)?(limit*i/ticks/1000).toFixed(1)+'s':Math.round(limit*i/ticks)+'ms'):'0ms',0,y+3);}
const threshold=bottom-(bottom-top)*300/limit;g.setLineDash([3,3]);g.strokeStyle='#dcc592';g.beginPath();g.moveTo(left,threshold);g.lineTo(right,threshold);g.stroke();g.setLineDash([]);g.fillStyle='#b4a16e';g.fillText('300ms',Math.max(left,right-38),threshold-4);
let labelX=-100;for(const e of events.filter(e=>e.time>=t-span&&e.time<=t&&(!canvas.closest('[data-instance]')||!/^tomcat[1-4] /.test(e.label)||canvas.closest('[data-instance]').dataset.instance.endsWith(e.label.split(' ')[0])))){const x=left+(e.time-(t-span))/span*(right-left);g.setLineDash([2,3]);g.strokeStyle='#cbc9df';g.beginPath();g.moveTo(x,top);g.lineTo(x,bottom);g.stroke();g.setLineDash([]);g.fillStyle='#a4a1ba';if(x-labelX>48){const word=e.label.match(/tomcat[1-4] (exclude|include|stop|start)/);g.fillText(word?({exclude:'제외',include:'복귀',stop:'중지',start:'시작'})[word[1]]:e.label.slice(0,6),x+2,11);labelX=x;}}
for(const p of filtered){const x=left+(p.time-(t-span))/span*(right-left),y=bottom-(bottom-top)*Math.min(limit,p.elapsed_ms)/limit;g.fillStyle=p.result==='failed'?'#d85b7c':p.result==='delayed'?'#d6a449':'#42a995';g.beginPath();g.arc(x,y,2,0,Math.PI*2);g.fill();}
g.fillStyle='#9ba0ae';const timeTicks=w<400?2:4;for(let i=0;i<=timeTicks;i++){const x=left+(right-left)*i/timeTicks;g.fillText(clock(t-span+span*i/timeTicks),Math.min(w-51,Math.max(left,x-18)),h-5);}if(!filtered.length){g.textAlign='center';g.fillStyle='#b0b3be';g.fillText('이 구간에 완료된 응답이 없습니다.',(left+right)/2,(top+bottom)/2);g.textAlign='left';}}
function draw(){const values=[...points.values()];all('[data-instance]').forEach(el=>graph(el.querySelector('canvas'),values.filter(p=>p.instance===el.dataset.instance)));const unknown=values.filter(p=>!p.instance&&p.result==='failed');graph($('#unknown-chart'),unknown);$('#unknown-count').textContent='현재 구간 '+fmt(unknown.filter(p=>p.time>=now()-Number($('#window').value)*1000).length)+'건';}
async function poll(){try{const r=await fetch('/state');if(!r.ok)throw Error();state=await r.json();const x=state.live;$('#connection').textContent='● 로컬 환경 연결됨 · '+clock(Date.now());$('#connection').style.color='#438c7e';lastSample=Date.now();
if(x?.runId){if(runId!==x.runId){runId=x.runId;points.clear();}const base=Date.parse(x.startedAt);for(const p of x.recent||[])if(p.request_id&&Number.isFinite(p.complete_offset))points.set(p.request_id,{...p,time:base+p.complete_offset*1000});for(const [id,p] of points)if(p.time<now()-310000)points.delete(id);
for(const kind of ['normal','delayed','failed','unsent'])$('#'+kind).textContent=fmt(x[kind]);const total=(x.normal||0)+(x.delayed||0)+(x.failed||0);$('#completed').textContent=fmt(total);$('#failure-rate').textContent=(total?100*x.failed/total:0).toFixed(2)+'% · 완료 기준';$('#sending').textContent=state.measureBusy?`송신·수집 중 · ${x.rps} RPS · 진행 ${x.inflight} · ${x.elapsed.toFixed(1)}초`:'송신 종료 · 수집 완료';$('#result').textContent='관측 결과 · results/'+runId;
$('#errors').innerHTML=(x.errors||[]).slice(-8).reverse().map(p=>`<tr><td>${clock(base+p.complete_offset*1000)}</td><td title="${esc(p.request_id)}">${esc(p.request_id.slice(0,8))}</td><td>${esc(p.http_status||'—')}</td><td>${p.elapsed_ms.toFixed(1)} ms</td><td title="${esc(p.instance||'인스턴스 미확인')}">${esc(p.error||p.result)}</td></tr>`).join('')||'<tr><td colspan="5" class="empty">아직 실패 응답이 없습니다.</td></tr>';
}
events=(state.events||[]).map(e=>({...e,time:e.time*1000}));if(x?.startedAt)for(const e of x.events||[]){if(['deploy_start','deploy_end','deploy_failed'].includes(e.event))events.push({time:Date.parse(x.startedAt)+e.offset*1000,label:({deploy_start:'ROLL 시작',deploy_end:'ROLL 완료',deploy_failed:'ROLL 실패'})[e.event]});}
events.sort((a,b)=>a.time-b.time);$('#events').innerHTML=events.slice(-40).reverse().map(e=>`<div class="event"><time>${clock(e.time)}</time><span>${esc(e.label)}</span></div>`).join('')||'<p class="empty">아직 제어 이벤트가 없습니다.</p>';$('#log').textContent=state.log||'';
all('[data-action]').forEach(b=>{b.disabled=['send-start','measure'].includes(b.dataset.action)?state.measureBusy||state.busy:b.dataset.action==='send-stop'?!state.measureBusy:state.busy;});all('[data-scale]').forEach(b=>b.disabled=state.busy);$('#rps').disabled=state.measureBusy;
$('#experiment').hidden=!state.experiment;if(state.experiment){const e=state.experiment;$('#experiment').textContent=`${e.pilot?'예비 확인':'배포 비교 실험'} · ${e.mode} ${e.run}/${e.count||10} · ${e.phase} · 완료 ${e.completed||0}회 · 조건 유지를 위해 수동 제어 잠금`;all('button').forEach(b=>b.disabled=true);}
if(state.deployment&&(mode!=='bluegreen'||state.deployment.strategy==='rolling-2plus2')){const d=state.deployment;$('#deploy-status').textContent=d.failed?'배포 실패 · 실제 상태 확인':d.label;all('#steps span').forEach((el,i)=>el.className=i+1<d.stage?'done':i+1===d.stage?'current':'');}
layout();draw();}catch{$('#connection').textContent='● 콘솔 연결 끊김 · 마지막 수신 '+(lastSample?clock(lastSample):'없음');$('#connection').style.color='#cc5776';all('button').forEach(b=>b.disabled=true);}}
async function observe(){try{const r=await fetch('/backends');if(!r.ok)throw Error();const data=await r.json();if(data.error){if(!backends.observedAt)backends=data;$('#control-status').textContent=data.error;return;}backends=data;layout();draw();}catch{$('#control-status').textContent='실제 서버 상태 조회 실패 · 마지막 확인 상태를 유지합니다.';}}
async function pollLoop(){await poll();setTimeout(pollLoop,500);}async function observeLoop(){await observe();setTimeout(observeLoop,1000);}$('#window').onchange=draw;window.addEventListener('resize',draw);pollLoop();observeLoop();
})();
