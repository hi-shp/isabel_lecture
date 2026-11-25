'use strict';
const $=id=>document.getElementById(id);
let state=null,client=null,ws=null,tool='route',route=[],area=null,corner=null,busy=false,first=true,driveGeneration=0;
const keys=['cell_m','dwell_s','radius_m','throttle','obstacle_m','zigzag_deg'];
const liveKeys=keys.filter(k=>k!=='cell_m'),dirtySettings=new Set();
let livePending={},waypointPending=[],liveSending=false,liveTimer=null,draggingMarker=false;
const home=[35.1777,129.1198];
const map=L.map('map',{doubleClickZoom:false}).setView(home,16);
const tiles=L.tileLayer('https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxNativeZoom:19,maxZoom:22,crossOrigin:true,attribution:'Source: <a href="https://www.arcgis.com/home/item.html?id=10df2279f9684e4a9f6a7f08febac2a9">Esri</a>, Vantor, Earthstar Geographics, GIS User Community'}).addTo(map);
tiles.on('load',()=>$('mapStatus').textContent='지도 배경: Esri 위성영상');
tiles.on('tileerror',()=>$('mapStatus').textContent='지도 배경 연결 대기 · 좌표/격자는 사용 가능');
L.control.scale({imperial:false}).addTo(map);
const routeLayer=L.layerGroup().addTo(map),gridLayer=L.layerGroup().addTo(map),areaLayer=L.layerGroup().addTo(map);
const path=L.polyline([],{color:'#078b80',weight:3}).addTo(map);
const headingGuide=L.polyline([],{color:'#d28520',weight:3,dashArray:'5 5'}).addTo(map);
let boat=null,lastRoute='',lastGrid='',lastArea='',lastPaint='';
const active=()=>state&&['running','dwelling'].includes(state.state);
function notice(msg,error=false){$('notice').textContent=msg;$('notice').classList.toggle('error',error);}
function settings(){return Object.fromEntries(keys.map(k=>[k,Number($(k).value)]));}
let rx=0,tx=0,rxBefore=0,txBefore=0,lastMetric=performance.now(),rtt=null,lastPong=0;
const bytes=s=>new TextEncoder().encode(s).byteLength;
async function api(action,data={}){
 const body=JSON.stringify({client,...data});tx+=bytes(body);
 const r=await fetch('/api/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body,signal:AbortSignal.timeout(action==='stop'?3000:10000)});
 const raw=await r.text();rx+=bytes(raw);const result=JSON.parse(raw);
 if(!r.ok)throw Error(result.error||r.statusText);if(!state||result.control_revision===undefined||result.control_revision>=(state.control_revision??0))render(result);return result;
}
async function run(fn){if(busy)return;busy=true;try{await fn();}catch(e){notice(e.message,true);}finally{busy=false;}}
async function configure(extra={}){return api('configure',{mode:state?.mode||'real',route:$('pattern').value==='coverage'?[]:route,settings:settings(),pattern:$('pattern').value,area,...extra});}
function queueSetting(key){
 const input=$(key);if(!input.checkValidity()||input.value===''){delete livePending[key];dirtySettings.add(key);$('liveStatus').textContent='입력 범위 안의 값을 입력하면 적용합니다.';return;}
 dirtySettings.add(key);livePending[key]=Number(input.value);clearTimeout(liveTimer);
 $('liveStatus').textContent='변경 전송 대기';liveTimer=setTimeout(flushLive,180);
}
async function flushLive(){
 clearTimeout(liveTimer);if(liveSending)return;
 if(!client||ws?.readyState!==WebSocket.OPEN){livePending={};waypointPending=[];dirtySettings.clear();lastRoute='';if(state)render(state);$('liveStatus').textContent='연결이 없어 변경되지 않았습니다.';return;}
 liveSending=true;
 try{while(Object.keys(livePending).length||waypointPending.length){
  const changes=livePending;livePending={};const waypoint=waypointPending.shift(),generation=driveGeneration;
  try{
   await api('live-update',{settings:changes,...(waypoint?{waypoint}:{}),plan_revision:state.plan_revision,
    control_revision:state.control_revision,mission_id:state.mission_id});
   if(generation!==driveGeneration)break;
   for(const k of Object.keys(changes))if(!(k in livePending)){dirtySettings.delete(k);if(document.activeElement!==$(k))$(k).value=state.settings[k];}
   $('liveStatus').textContent='선박 설정 적용됨 · '+new Date().toLocaleTimeString('ko-KR');
  }catch(e){
   if(generation!==driveGeneration)break;
   for(const k of Object.keys(changes))if(!(k in livePending)){dirtySettings.delete(k);$(k).value=state.settings[k];}
   if(generation===driveGeneration){notice('변경 적용 실패: '+e.message,true);$('liveStatus').textContent='변경 미적용 · 다시 입력하세요.';}
   livePending={};waypointPending=[];dirtySettings.clear();lastRoute='';render(state);break;
  }
 }}finally{liveSending=false;}
}
function moveWaypoint(index,position){
 waypointPending=waypointPending.filter(p=>p.index!==index);
 waypointPending.push({index,lat:position.lat,lon:position.lng});flushLive();
}
function choose(value){tool=value;corner=null;for(const [id,t] of [['virtual','virtual'],['addRoute','route'],['selectArea','area']])$(id).classList.toggle('selected',value===t);
 $('toolHelp').textContent=value==='virtual'?'지도를 클릭하면 해당 좌표를 발행합니다.':value==='area'?'직사각형의 대각선 두 꼭짓점을 차례로 클릭하세요.':'지도에 클릭한 순서대로 경유지가 추가됩니다.';
}
$('armed').onchange=()=>{if(state)render(state);};
$('virtual').onclick=()=>run(async()=>{if(state?.mode!=='simulated'){if(active())throw Error('위치 입력을 바꾸려면 먼저 정지하세요.');await configure({mode:'simulated'});}choose('virtual');});
$('resetPosition').onclick=()=>run(async()=>{await api('reset-position');choose('route');notice('지도 입력 위치를 지우고 GPS 수신으로 돌아갑니다.');});
$('newMap').onclick=()=>run(async()=>{await api('new-map');$('pattern').value='waypoints';$('armed').checked=false;choose('route');map.setView(home,16);notice('새 조사 지도를 열었습니다. 이전 기록은 저장 목록에 남아 있습니다.');history();});
$('resetView').onclick=()=>map.setView(home,16);
$('addRoute').onclick=()=>run(async()=>{if(active())throw Error('먼저 정지하세요.');if(state?.pattern!=='waypoints'){route=[];area=null;$('pattern').value='waypoints';await configure();}choose('route');});
$('selectArea').onclick=()=>{if(active())return notice('먼저 정지하세요.',true);choose('area');};
map.on('click',e=>run(async()=>{
 const p={lat:e.latlng.lat,lon:e.latlng.lng};
 if(tool==='virtual'){await api('virtual',p);notice('위치를 발행했습니다.');return;}
 if(active())throw Error('조사 중에는 경로를 바꿀 수 없습니다.');
 if(tool==='area'){
  if(!corner){corner=p;notice('반대쪽 꼭짓점을 클릭하세요.');return;}
  const prev={area,route,pattern:$('pattern').value};
  area={south:Math.min(corner.lat,p.lat),north:Math.max(corner.lat,p.lat),west:Math.min(corner.lon,p.lon),east:Math.max(corner.lon,p.lon)};corner=null;$('pattern').value='coverage';
  try{await configure();notice('격자 중심을 잇는 조사 경로를 만들었습니다.');}catch(err){area=prev.area;route=prev.route;$('pattern').value=prev.pattern;throw err;}
 }else{const old=route;route=[...route,p];try{await configure();}catch(err){route=old;throw err;}}
}));
$('pattern').onchange=()=>run(async()=>{if($('pattern').value==='coverage'&&!area){$('pattern').value=state?.pattern||'waypoints';choose('area');notice('조사 범위의 두 꼭짓점을 클릭하세요.');return;}if($('pattern').value==='waypoints')route=[];await configure();choose('route');});
for(const k of liveKeys){$(k).oninput=()=>queueSetting(k);$(k).onchange=()=>queueSetting(k);}
$('cell_m').onchange=()=>run(configure);
$('undo').onclick=()=>run(async()=>{route=route.slice(0,-1);await configure();});
$('clear').onclick=()=>run(async()=>{route=[];area=null;$('pattern').value='waypoints';await configure();});
function startDrive(action,message){return run(async()=>{if(liveSending||Object.keys(livePending).length||waypointPending.length)throw Error('설정 적용 완료 후 시작하세요.');const generation=driveGeneration;try{await configure();if(generation!==driveGeneration)return;await api(action,{armed:$('armed').checked&&state.hardware,control_revision:state.control_revision});if(generation===driveGeneration)notice(message);}catch(e){if(generation===driveGeneration)throw e;}});}
$('start').onclick=()=>startDrive('start-waypoints','웨이포인트 추종을 시작했습니다.');
$('startAutonomous').onclick=()=>startDrive('start-autonomous',`중앙 경유지 기준 ±${state.settings.zigzag_deg}° 자율운항을 시작했습니다.`);
$('stop').onclick=()=>{driveGeneration++;clearTimeout(liveTimer);livePending={};waypointPending=[];dirtySettings.clear();notice('주행 종료 · 중립 명령 전송 중');api('stop').then(s=>notice(s.last_stop?.neutral_published?'주행 종료 · 중립 명령 전송':'주행 종료 · 추진 출력 비활성')).catch(()=>notice('중립 명령 전달을 확인하지 못했습니다. 선박 연결을 확인하세요.',true));};
for(const action of ['start','stop'])$('bringup'+(action==='start'?'Start':'Stop')).onclick=()=>run(async()=>{await api('bringup-'+action);notice(action==='start'?'선박 브링업을 시작하고 있습니다.':'조사를 정지하고 브링업을 종료합니다.');});
$('center').onclick=()=>{if(state?.position?.valid)map.setView([state.position.lat,state.position.lon],18);else if(route.length)map.fitBounds(route.map(p=>[p.lat,p.lon]),{padding:[40,40],maxZoom:18});};
function download(url,name){const a=document.createElement('a');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();}
$('export').onclick=()=>{if(state?.mission_id)download('/api/export/'+encodeURIComponent(state.mission_id),'water-survey.zip');};
$('downloadOld').onclick=()=>{if($('missions').value)download('/api/export/'+encodeURIComponent($('missions').value),'water-survey.zip');};
$('gridImage').onclick=()=>download($('grid').toDataURL('image/png'),'do-grid.png');
$('chartImage').onclick=()=>download($('chart').toDataURL('image/png'),'do-timeseries.png');
$('saveRoute').onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify({schema_version:2,pattern:state.pattern,area,route:state.pattern==='coverage'?[]:route,settings:settings()},null,2)],{type:'application/json'}));download(url,'survey-route.json');setTimeout(()=>URL.revokeObjectURL(url),5000);};
$('loadRoute').onclick=()=>$('routeFile').click();
$('routeFile').onchange=()=>run(async()=>{const f=$('routeFile').files[0];if(!f)return;if(f.size>100000)throw Error('경로 파일이 너무 큽니다.');const d=JSON.parse(await f.text());if(!Array.isArray(d.route))throw Error('경로 JSON 형식이 아닙니다.');await api('configure',{mode:state.mode,route:d.route,settings:d.settings||{},pattern:d.pattern||'waypoints',area:d.area});for(const k of keys)$(k).value=state.settings[k];$('routeFile').value='';});
async function history(){try{const items=await(await fetch('/api/missions')).json();const selected=$('missions').value;$('missions').replaceChildren(new Option('이전 조사 선택',''));for(const m of items)$('missions').append(new Option(`${m.started_utc.slice(0,19)} UTC · ${m.samples}개`,m.mission_id));$('missions').value=selected;}catch{}}
function send(message){const text=typeof message==='string'?message:JSON.stringify(message);tx+=bytes(text);ws.send(text);}
function connect(){ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws');ws.onopen=()=>{$('connection').textContent='선박 연결됨';$('connection').classList.remove('amber');};ws.onmessage=e=>{rx+=bytes(e.data);const m=JSON.parse(e.data);if(m.type==='hello'){state=null;client=m.client;if(['선박에 연결하고 있습니다.','화면 연결이 끊겼습니다. 선박은 조사를 계속합니다.'].includes($('notice').textContent))notice('대시보드 연결됨 · 조사 계획과 센서 상태를 확인하세요.');history();}else if(m.type==='state')render(m.data);else if(m.type==='pong'){rtt=performance.now()-m.sent;lastPong=performance.now();}};ws.onclose=()=>{driveGeneration++;clearTimeout(liveTimer);livePending={};waypointPending=[];dirtySettings.clear();client=null;rtt=null;$('connection').textContent='선박 연결 끊김';$('connection').classList.add('amber');$('start').disabled=true;$('startAutonomous').disabled=true;if(state)render(state);notice('화면 연결이 끊겼습니다. 선박은 조사를 계속합니다.',true);setTimeout(connect,2000);};}
setInterval(()=>{const now=performance.now(),seconds=(now-lastMetric)/1000;const ping=rtt!==null&&now-lastPong<5000?`${rtt.toFixed(0)} ms`:'—';$('netStatus').textContent=`왕복 ${ping} · 수신 ${((rx-rxBefore)/seconds/1024).toFixed(1)} KB/s · 송신 ${((tx-txBefore)/seconds/1024).toFixed(1)} KB/s`;$('netStatus').title='브라우저↔선박 애플리케이션 왕복 지연과 실제 대시보드 payload 전송률입니다. 회선 최대 속도가 아닙니다.';rxBefore=rx;txBefore=tx;lastMetric=now;if(ws?.readyState===WebSocket.OPEN){send('heartbeat');send({type:'ping',sent:now});}},1000);
setInterval(history,15000);
function render(s){if(state&&((s.control_revision??0)<(state.control_revision??0)||((s.control_revision??0)===(state.control_revision??0)&&(s.plan_revision??0)<(state.plan_revision??0))))return;state=s;route=s.route;area=s.area;const running=active();for(const k of keys)if(!dirtySettings.has(k)&&document.activeElement!==$(k))$(k).value=s.settings[k];first=false;$('pattern').value=s.pattern||'waypoints';
 $('clock').textContent=new Date(s.timestamp).toLocaleTimeString('ko-KR');$('modeBadge').textContent=({running:'조사 중',dwelling:'지점 관측',complete:'조사 완료',stopped:'정지',idle:'조사 대기'}[s.state]||s.state);$('armLabel').hidden=!s.hardware;
 const b=s.bringup||{},bstate={active:'실행 중',activating:'시작 중',inactive:'정지',deactivating:'종료 중',failed:'오류',unknown:'확인 중',unmanaged:'외부 실행'};
 $('bringupStatus').textContent=`브링업 ${bstate[b.state]||b.state||'확인 중'} · ${b.detail||''}`;
 $('bringupStart').disabled=running||!b.available||['active','activating','deactivating'].includes(b.state)||s.lifecycle_busy;
 $('bringupStop').disabled=!b.available||!['active','activating'].includes(b.state)||s.lifecycle_busy;
 const fresh=s.do&&s.do_age_s<5;$('doValue').innerHTML=(fresh?s.do.value.toFixed(2):'—')+'<small>mg/L</small>';$('sensorStatus').textContent=fresh?`${s.do.sensor} · ${s.do_age_s.toFixed(1)}초 전`:s.do_status;
 $('sampleCount').innerHTML=s.count+'<small>개</small>';$('mappedCount').textContent=`위치 결합 ${s.mapped}개`;
 $('progress').textContent=running?`${Math.min(s.waypoint+1,route.length)} / ${route.length}`:({idle:'대기',complete:'완료',stopped:'정지'}[s.state]||s.state);
 $('phase').textContent=s.state==='dwelling'?`지점 관측 중 · ${s.settings.dwell_s}초`:s.state==='running'?`목표까지 ${s.command.distance?.toFixed(1)||'—'} m`:s.reason||'경로를 지정하세요';
 if(running&&s.navigation==='river_zigzag'&&s.guidance){const g=s.guidance;$('phase').textContent=g.phase==='direct'?`목표로 직진 · ${g.course_deg.toFixed(1)}°`:`${g.side>0?'우측':'좌측'} ${Math.abs(g.offset_deg)}° · ${g.phase.startsWith('turn_')?'방향 전환 중':'직진'} · 목표 방위 ${g.course_deg.toFixed(1)}°`;}
 const scan=s.lidar,scanFresh=scan&&scan.valid&&s.lidar_age_s<=1;
 $('lidarStatus').textContent=scanFresh?`라이다 정면 1° · ${scan.front_mean_m===null?'히트 없음':scan.front_mean_m.toFixed(2)+' m'} · ${scan.front_hits}개 중 ${scan.front_kept}개 평균 · ≤${s.settings.obstacle_m}m 방향 전환`:'자율운항: 유효한 라이다 정면 1° 측정 대기';
 $('speed').textContent=!Number.isFinite(s.speed_m_s)?'GPS 속도 —':`GPS 속도(추정) ${s.speed_m_s.toFixed(2)} m/s · ${(s.speed_m_s*3.6).toFixed(1)} km/h`;
 $('heading').innerHTML=(s.heading!==null&&s.imu_age_s<2?s.heading.toFixed(1):'—')+'<small>°</small>';$('actuator').textContent=s.armed?`조향 ${s.command.servo.toFixed(0)}° · 추진 ${s.command.throttle.toFixed(0)}%`:'추진 대기';
 $('readiness').textContent=running?(s.armed?'실제 추진 주행 및 기록 중':'추진 비활성 · 위치와 DO 기록 중'):($('armed').checked?s.drive_ready_reason:s.ready_reason)||'센서 준비 완료 · 경로를 확인하고 시작하세요.';
 if(s.position?.valid){$('coords').textContent=`${s.position.lat.toFixed(7)}, ${s.position.lon.toFixed(7)}`;if(!boat)boat=L.marker([s.position.lat,s.position.lon],{icon:L.divIcon({className:'',html:'<div class="boat-dot"></div>',iconSize:[20,20],iconAnchor:[10,10]})}).addTo(map);else boat.setLatLng([s.position.lat,s.position.lon]);}else{$('coords').textContent='GPS 수신 대기';if(boat){map.removeLayer(boat);boat=null;}}
 const rkey=JSON.stringify([route,s.waypoint,s.state,!!client]);if(!draggingMarker&&lastRoute!==rkey){lastRoute=rkey;routeLayer.clearLayers();if(route.length)L.polyline(route.map(p=>[p.lat,p.lon]),{color:'#536e7b',dashArray:'6 5',weight:2}).addTo(routeLayer);$('waypoints').replaceChildren();if(!route.length){const li=document.createElement('li');li.className='empty';li.textContent='지도에서 경로 또는 범위를 지정하세요.';$('waypoints').append(li);}route.forEach((p,i)=>{if(route.length<150||i===s.waypoint){const marker=L.marker([p.lat,p.lon],{draggable:s.pattern==='waypoints'&&!!client,autoPan:true,icon:L.divIcon({className:'',html:`<div class="wp-dot">${i+1}</div>`,iconSize:[24,24],iconAnchor:[12,12]})}).addTo(routeLayer);marker.on('dragstart',()=>{draggingMarker=true;});marker.on('dragend',()=>{draggingMarker=false;moveWaypoint(i,marker.getLatLng());});} if(route.length>150&&Math.abs(i-s.waypoint)>5)return;const li=document.createElement('li');li.className=i<s.waypoint?'done':i===s.waypoint&&running?'current':'';li.innerHTML=`<span class="num">${i+1}</span><span>${p.lat.toFixed(6)}<br>${p.lon.toFixed(6)}</span>`;$('waypoints').append(li);});}
 const akey=JSON.stringify([s.area,s.plan,s.settings.cell_m]);if(lastArea!==akey){lastArea=akey;areaLayer.clearLayers();if(s.area){const a=s.area;L.rectangle([[a.south,a.west],[a.north,a.east]],{color:'#385d6e',weight:2,fill:false}).addTo(areaLayer);if(s.plan)for(const key of s.plan.targets){const [x,y]=key.split(',').map(Number);const size=s.settings.cell_m;L.rectangle([geo(x*size,y*size,s.plan.origin),geo(Math.min((x+1)*size,s.plan.width),Math.min((y+1)*size,s.plan.height),s.plan.origin)],{color:'#98aeb8',weight:.7,fill:false,interactive:false}).addTo(areaLayer);}}}
 path.setLatLngs(s.track);const gkey=JSON.stringify([s.origin,s.cells,s.color_scale]);if(gkey!==lastGrid){lastGrid=gkey;gridLayer.clearLayers();if(s.origin)for(const c of s.cells){const size=s.settings.cell_m,a=geo(c.ix*size,c.iy*size,s.origin),b=geo(s.plan?Math.min((c.ix+1)*size,s.plan.width):(c.ix+1)*size,s.plan?Math.min((c.iy+1)*size,s.plan.height):(c.iy+1)*size,s.origin);L.rectangle([a,b],{color:'#fff',weight:1,fillColor:color(c.mean),fillOpacity:.75}).bindTooltip(`${c.mean.toFixed(3)} mg/L · ${c.count}회`).addTo(gridLayer);}}
 for(const id of ['pattern','undo','clear','loadRoute','selectArea','addRoute','newMap','resetPosition','cell_m'])$(id).disabled=running;
 for(const k of liveKeys)$(k).disabled=!client;
 $('virtual').disabled=running&&s.mode!=='simulated';$('undo').disabled=running||s.pattern==='coverage';
 $('start').disabled=running||!client||!route.length||!!($('armed').checked?s.drive_ready_reason:s.ready_reason)||s.lifecycle_busy;
 $('startAutonomous').disabled=running||!client||!route.length||!!($('armed').checked?s.autonomy_drive_ready_reason:s.autonomy_ready_reason)||s.lifecycle_busy;
 $('startAutonomous').title=s.autonomy_ready_reason||`목표 기준 ±${s.settings.zigzag_deg}° · 0°는 목표 직진`;
 $('export').disabled=!s.mission_id;
 if(running&&s.navigation==='river_zigzag'&&s.guidance&&s.position?.valid){const a=s.guidance.course_deg*Math.PI/180;headingGuide.setLatLngs([[s.position.lat,s.position.lon],geo(10*Math.sin(a),10*Math.cos(a),s.position)]);}else headingGuide.setLatLngs([]);
 $('mission').textContent=s.mission_id||'저장 중인 조사 없음';$('hardwareCount').textContent=`추진 명령 발행 ${s.hardware_publish_count}회`;
 $('gridLabel').textContent=`${s.settings.cell_m} m 격자 · ${s.cells.length}칸 관측 · 북쪽 ↑`;
 const cv=s.coverage;$('coverageStatus').textContent=cv?`범위 ${s.plan.cols} × ${s.plan.rows}칸 · 관측 ${cv.observed}/${cv.total} (${(cv.observed/cv.total*100).toFixed(1)}%)`:'';
 $('provenance').textContent='격자 평균의 누적 최소·최대로 색을 자동 조정합니다.';
 $('scaleMin').textContent=s.color_scale?`${s.color_scale[0].toFixed(3)}`:'—';$('scaleMax').textContent=s.color_scale?`${s.color_scale[1].toFixed(3)} mg/L`:'— mg/L';
 const paintKey=JSON.stringify([s.mission_id,s.route,s.settings,s.origin,s.count,s.color_scale,s.samples.at(-1)?.received_utc]);if(paintKey!==lastPaint){lastPaint=paintKey;drawGrid(s);drawChart(s);document.dispatchEvent(new Event('survey-render'));}
}
function color(v){const bounds=state?.color_scale||[v,v],lo=bounds[0],hi=bounds[1];const t=hi>lo?Math.max(0,Math.min(1,(v-lo)/(hi-lo))):.5,stops=[[190,58,60],[235,179,64],[49,147,140],[42,83,161]],i=Math.min(2,Math.floor(t*3)),f=t*3-i;return '#'+stops[i].map((n,j)=>Math.round(n*(1-f)+stops[i+1][j]*f).toString(16).padStart(2,'0')).join('');}
function geo(x,y,o){return[o.lat+y/6378137*180/Math.PI,o.lon+x/(6378137*Math.cos(o.lat*Math.PI/180))*180/Math.PI];}
function meters(p,o){return[(p.lon-o.lon)*Math.PI/180*6378137*Math.cos(o.lat*Math.PI/180),(p.lat-o.lat)*Math.PI/180*6378137];}
function canvas(id,s){const c=$(id),ctx=c.getContext('2d');ctx.fillStyle='#f8fafb';ctx.fillRect(0,0,c.width,c.height);ctx.font='16px sans-serif';ctx.fillStyle='#71858f';ctx.fillText('위치별 용존산소 조사 · mg/L',30,28);return ctx;}
function drawGrid(s){const ctx=canvas('grid',s),o=s.origin||s.position||{lat:35.1777,lon:129.1198},size=s.settings.cell_m;let pts=s.route.map(p=>meters(p,o));for(const c of s.cells)pts.push([c.ix*size,c.iy*size],[(c.ix+1)*size,(c.iy+1)*size]);if(s.plan)pts.push([0,0],[s.plan.width,s.plan.height]);if(!pts.length)pts=[[0,0],[40,30]];const xs=pts.map(p=>p[0]),ys=pts.map(p=>p[1]);const minx=Math.min(...xs)-size,miny=Math.min(...ys)-size,w=Math.max(40,Math.max(...xs)-minx+size),h=Math.max(30,Math.max(...ys)-miny+size),scale=Math.min(780/w,310/h),pos=(x,y)=>[65+(x-minx)*scale,365-(y-miny)*scale];
 ctx.strokeStyle='#e2e9ec';ctx.lineWidth=1;const step=Math.max(size,Math.ceil(Math.max(w,h)/30/size)*size);for(let x=Math.floor(minx/step)*step;x<minx+w;x+=step){const p=pos(x,miny);ctx.beginPath();ctx.moveTo(p[0],55);ctx.lineTo(p[0],365);ctx.stroke();}for(let y=Math.floor(miny/step)*step;y<miny+h;y+=step){const p=pos(minx,y);ctx.beginPath();ctx.moveTo(65,p[1]);ctx.lineTo(845,p[1]);ctx.stroke();}
 if(s.plan){ctx.strokeStyle='#c4d3d9';for(const k of s.plan.targets){const [x,y]=k.split(',').map(Number),top=Math.min((y+1)*size,s.plan.height),right=Math.min((x+1)*size,s.plan.width),p=pos(x*size,top);ctx.strokeRect(p[0],p[1],(right-x*size)*scale,(top-y*size)*scale);}}for(const c of s.cells){const top=s.plan?Math.min((c.iy+1)*size,s.plan.height):(c.iy+1)*size,right=s.plan?Math.min((c.ix+1)*size,s.plan.width):(c.ix+1)*size,p=pos(c.ix*size,top);ctx.fillStyle=color(c.mean);ctx.fillRect(p[0],p[1],(right-c.ix*size)*scale,(top-c.iy*size)*scale);ctx.strokeStyle='#fff';ctx.strokeRect(p[0],p[1],(right-c.ix*size)*scale,(top-c.iy*size)*scale);if(size*scale>45){ctx.fillStyle='#fff';ctx.font='15px sans-serif';ctx.fillText(c.mean.toFixed(2),p[0]+5,p[1]+22);}}
 ctx.strokeStyle='#425e6c';ctx.lineWidth=2;ctx.beginPath();s.route.forEach((p,i)=>{const z=pos(...meters(p,o));i?ctx.lineTo(...z):ctx.moveTo(...z);});ctx.stroke();s.route.forEach((p,i)=>{const z=pos(...meters(p,o));ctx.fillStyle='#2d4e5b';ctx.beginPath();ctx.arc(...z,11,0,Math.PI*2);ctx.fill();ctx.fillStyle='#fff';ctx.font='12px sans-serif';ctx.fillText(String(i+1),z[0]-3,z[1]+4);});
 ctx.fillStyle='#71858f';ctx.font='13px sans-serif';ctx.fillText(`동서 ${w.toFixed(0)} m · 남북 ${h.toFixed(0)} m · 빈 칸=미관측`,65,402);if(!s.cells.length){ctx.fillStyle='#71858f';ctx.font='18px sans-serif';ctx.fillText('조사를 시작하면 관측 위치에 색이 채워집니다.',235,210);}}
function drawChart(s){const ctx=canvas('chart',s),rows=s.samples;if(!rows.length){ctx.fillText('아직 기록된 DO 데이터가 없습니다.',270,215);return;}const values=rows.map(r=>r.do_mg_l),lo=Math.min(...values)-.15,hi=Math.max(...values)+.15,first=Date.parse(rows[0].received_utc),last=Date.parse(rows.at(-1).received_utc),span=Math.max(1000,last-first);ctx.font='14px sans-serif';for(let i=0;i<=4;i++){const y=65+i*72;ctx.strokeStyle='#dce5e9';ctx.beginPath();ctx.moveTo(75,y);ctx.lineTo(855,y);ctx.stroke();ctx.fillStyle='#6d818c';ctx.fillText((hi-(hi-lo)*i/4).toFixed(2),18,y+5);}ctx.beginPath();ctx.strokeStyle='#07897d';ctx.lineWidth=3;rows.forEach((r,i)=>{const x=75+(Date.parse(r.received_utc)-first)/span*780,y=353-(r.do_mg_l-lo)/(hi-lo)*288;i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.stroke();ctx.fillStyle='#6d818c';ctx.fillText(new Date(first).toLocaleTimeString('ko-KR'),75,395);ctx.fillText(new Date(last).toLocaleTimeString('ko-KR'),745,395);$('range').textContent=`범위 ${Math.min(...values).toFixed(2)}~${Math.max(...values).toFixed(2)} mg/L · 화면 시간은 이 기기의 현지 시간` ;}
connect();
