'use strict';
const $=id=>document.getElementById(id);
let state=null,client=null,ws=null,tool='route',route=[],busy=false,first=true;
const keys=['cell_m','dwell_s','radius_m','throttle','sim_speed'];
const map=L.map('map',{doubleClickZoom:false}).setView([35.1777,129.1198],16);
const tiles=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}).addTo(map);
tiles.on('tileerror',()=>$('mapStatus').textContent='배경 지도 연결 대기 · 좌표/격자는 계속 사용 가능');
L.control.scale({imperial:false}).addTo(map);
const routeLayer=L.layerGroup().addTo(map),gridLayer=L.layerGroup().addTo(map);
const path=L.polyline([],{color:'#078b80',weight:3}).addTo(map);
let boat=null,lastRoute='',lastGrid='';
const active=()=>state&&['running','dwelling'].includes(state.state);
function notice(msg,error=false){$('notice').textContent=msg;$('notice').classList.toggle('error',error);}
function settings(){return Object.fromEntries(keys.map(k=>[k,Number($(k).value)]));}
async function api(action,data={}){const r=await fetch('/api/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({client,...data})});const result=await r.json();if(!r.ok)throw Error(result.error||r.statusText);render(result);return result;}
async function run(fn){if(busy)return;busy=true;try{await fn();}catch(e){notice(e.message,true);}finally{busy=false;}}
async function configure(){return api('configure',{mode:$('mode').value,route,settings:settings()});}
function choose(value){tool=value;$('virtual').classList.toggle('selected',value==='virtual');$('addRoute').classList.toggle('selected',value==='route');$('toolHelp').textContent=value==='virtual'?'지도를 클릭하면 그 지점이 가상 GPS 위치가 됩니다.':'지도에 클릭한 순서대로 경유지가 추가됩니다.';}
$('virtual').onclick=()=>run(async()=>{if(active())throw Error('먼저 정지하세요.');$('mode').value='simulated';await configure();choose('virtual');});
$('addRoute').onclick=()=>choose('route');
map.on('click',e=>run(async()=>{if(active())throw Error('주행 중에는 경로를 바꿀 수 없습니다.');if(tool==='virtual'){await api('virtual',{lat:e.latlng.lat,lon:e.latlng.lng});notice('가상 위치를 지정했습니다. 경유지를 추가한 뒤 시작하세요.');}else{const old=route;route=[...route,{lat:e.latlng.lat,lon:e.latlng.lng}];try{await configure();}catch(err){route=old;throw err;}}}));
$('mode').onchange=()=>run(async()=>{await configure();$('armed').checked=false;choose('route');});
for(const k of keys)$(k).onchange=()=>run(configure);
$('undo').onclick=()=>run(async()=>{route=route.slice(0,-1);await configure();});
$('clear').onclick=()=>run(async()=>{route=[];await configure();});
$('start').onclick=()=>run(async()=>{await configure();await api('start',{armed:$('armed').checked});notice(state.mode==='simulated'?'실내 시험을 시작했습니다. 실제 DO와 가상 위치를 기록합니다.':'웨이포인트 조사를 시작했습니다.');});
$('stop').onclick=()=>{api('stop').then(()=>notice('조사를 정지했습니다.')).catch(e=>notice(e.message,true));};
$('center').onclick=()=>{if(state?.position?.valid)map.setView([state.position.lat,state.position.lon],18);else if(route.length)map.fitBounds(route.map(p=>[p.lat,p.lon]),{padding:[40,40],maxZoom:18});};
function download(url,name){const a=document.createElement('a');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();}
$('export').onclick=()=>{if(state?.mission_id)download('/api/export/'+encodeURIComponent(state.mission_id),'water-survey.zip');};
$('downloadOld').onclick=()=>{if($('missions').value)download('/api/export/'+encodeURIComponent($('missions').value),'water-survey.zip');};
$('gridImage').onclick=()=>download($('grid').toDataURL('image/png'),'do-grid.png');
$('chartImage').onclick=()=>download($('chart').toDataURL('image/png'),'do-timeseries.png');
$('saveRoute').onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify({schema_version:1,route,settings:settings()},null,2)],{type:'application/json'}));download(url,'survey-route.json');setTimeout(()=>URL.revokeObjectURL(url),5000);};
$('loadRoute').onclick=()=>$('routeFile').click();
$('routeFile').onchange=()=>run(async()=>{const f=$('routeFile').files[0];if(!f)return;if(f.size>50000)throw Error('경로 파일이 너무 큽니다.');const data=JSON.parse(await f.text());if(!Array.isArray(data.route))throw Error('경로 JSON 형식이 아닙니다.');const previous=route;route=data.route;try{await api('configure',{mode:$('mode').value,route,settings:data.settings||{}});}catch(e){route=previous;throw e;}for(const k of keys)$(k).value=state.settings[k];$('routeFile').value='';});
async function history(){try{const items=await(await fetch('/api/missions')).json();const selected=$('missions').value;$('missions').replaceChildren(new Option('이전 조사 선택',''));for(const m of items)$('missions').append(new Option(`${m.experimental?'실내':'수상'} · ${m.started_utc.slice(0,19)} UTC · ${m.samples}개`,m.mission_id));$('missions').value=selected;}catch{}}
function connect(){ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws');ws.onopen=()=>{$('connection').textContent='선박 연결됨';$('connection').classList.remove('amber');};ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.type==='hello'){client=m.client;history();}else if(m.type==='state')render(m.data);};ws.onclose=()=>{client=null;$('connection').textContent='선박 연결 끊김';$('connection').classList.add('amber');$('start').disabled=true;notice('화면 연결이 끊겼습니다. 선박은 저장된 경로의 조사를 계속합니다.',true);setTimeout(connect,2000);};}
setInterval(()=>{if(ws?.readyState===WebSocket.OPEN)ws.send('heartbeat');},1000);
setInterval(history,15000);
function render(s){state=s;route=s.route;const running=active();if(first){$('mode').value=s.mode;for(const k of keys)$(k).value=s.settings[k];first=false;}if(document.activeElement!==$('mode'))$('mode').value=s.mode;
 $('clock').textContent=new Date(s.timestamp).toLocaleTimeString('ko-KR');$('modeBadge').textContent=s.mode==='simulated'?'실내 시험 · 가상 GPS':'수상 조사 · 실제 GPS';$('modeBadge').classList.toggle('amber',s.mode==='simulated');$('simHint').hidden=s.mode!=='simulated';$('armLabel').hidden=s.mode!=='real';
 const fresh=s.do&&s.do_age_s<5;$('doValue').innerHTML=(fresh?s.do.value.toFixed(2):'—')+'<small>mg/L</small>';$('sensorStatus').textContent=fresh?`${s.do.sensor} · ${s.do_age_s.toFixed(1)}초 전`:s.do_status;$('sensorStatus').title=s.do_status;
 $('sampleCount').innerHTML=s.count+'<small>개</small>';$('mappedCount').textContent=`위치 결합 ${s.mapped}개`;
 $('progress').textContent=running?`${Math.min(s.waypoint+1,route.length)} / ${route.length}`:({idle:'대기',complete:'완료',stopped:'정지'}[s.state]||s.state);
 $('phase').textContent=s.state==='dwelling'?`지점 관측 중 · ${s.settings.dwell_s}초`:s.state==='running'?`목표까지 ${s.command.distance?.toFixed(1)||'—'} m`:s.reason||'경로를 지정하세요';
 $('heading').innerHTML=(s.heading!==null&&s.imu_age_s<2?s.heading.toFixed(1):'—')+'<small>°</small>';$('actuator').textContent=s.armed?`조향 ${s.command.servo.toFixed(0)}° · 추진 ${s.command.throttle.toFixed(0)}%`:'추진 명령 비활성';
 $('readiness').textContent=running?'선박에서 독립 실행 중 · 화면을 닫아도 계속 진행합니다.':s.ready_reason||'센서 준비 완료 · 경로를 확인하고 시작하세요.';
 if(s.position?.valid){$('coords').textContent=`${s.position.lat.toFixed(7)}, ${s.position.lon.toFixed(7)}${s.mode==='simulated'?' · 가상 위치':''}`;if(!boat){boat=L.marker([s.position.lat,s.position.lon],{icon:L.divIcon({className:'',html:`<div class="boat-dot ${s.mode==='simulated'?'sim':''}"></div>`,iconSize:[20,20],iconAnchor:[10,10]})}).addTo(map);}else boat.setLatLng([s.position.lat,s.position.lon]);}else{$('coords').textContent='GPS 수신 대기';if(boat){map.removeLayer(boat);boat=null;}}
 const rkey=JSON.stringify([route,s.waypoint,s.state]);if(lastRoute!==rkey){lastRoute=rkey;routeLayer.clearLayers();if(route.length)L.polyline(route.map(p=>[p.lat,p.lon]),{color:'#536e7b',dashArray:'6 5',weight:2}).addTo(routeLayer);$('waypoints').replaceChildren();if(!route.length){const li=document.createElement('li');li.className='empty';li.textContent='지도에서 첫 경유지를 선택하세요.';$('waypoints').append(li);}route.forEach((p,i)=>{L.marker([p.lat,p.lon],{icon:L.divIcon({className:'',html:`<div class="wp-dot">${i+1}</div>`,iconSize:[24,24],iconAnchor:[12,12]})}).addTo(routeLayer);const li=document.createElement('li');li.className=i<s.waypoint?'done':i===s.waypoint&&running?'current':'';li.innerHTML=`<span class="num">${i+1}</span><span>${p.lat.toFixed(6)}<br>${p.lon.toFixed(6)}</span>`;$('waypoints').append(li);});}
 path.setLatLngs(s.track);const gkey=JSON.stringify([s.origin,s.cells]);if(gkey!==lastGrid){lastGrid=gkey;gridLayer.clearLayers();if(s.origin)for(const c of s.cells){const a=geo(c.ix*s.settings.cell_m,c.iy*s.settings.cell_m,s.origin),b=geo((c.ix+1)*s.settings.cell_m,(c.iy+1)*s.settings.cell_m,s.origin);L.rectangle([a,b],{color:'#fff',weight:1,fillColor:color(c.mean),fillOpacity:.65}).bindTooltip(`${c.mean.toFixed(2)} mg/L · ${c.count}회`).addTo(gridLayer);}}
 for(const id of ['mode','virtual','undo','clear','loadRoute',...keys])$(id).disabled=running;
 $('start').disabled=running||!client||!route.length||!!s.ready_reason;$('export').disabled=!s.mission_id;
 $('mission').textContent=s.mission_id||'저장 중인 조사 없음';$('hardwareCount').textContent=`추진 명령 발행 ${s.hardware_publish_count}회`;
 $('gridLabel').textContent=`${s.settings.cell_m} m 격자 · ${s.cells.length}칸 관측 · 북쪽 ↑`;
 $('provenance').textContent=s.mode==='simulated'?'실제 DO + 가상 GPS · 실내 시험 기록이며 하천 측정 결과가 아닙니다.':'실제 DO + GPS · 최근 2초 이내 위치만 결합합니다.';
 drawGrid(s);drawChart(s);
}
function color(v){const t=Math.max(0,Math.min(1,(v-2)/10)),stops=[[190,58,60],[235,179,64],[49,147,140],[42,83,161]],i=Math.min(2,Math.floor(t*3)),f=t*3-i;return '#'+stops[i].map((n,j)=>Math.round(n*(1-f)+stops[i+1][j]*f).toString(16).padStart(2,'0')).join('');}
function geo(x,y,o){return[o.lat+y/6378137*180/Math.PI,o.lon+x/(6378137*Math.cos(o.lat*Math.PI/180))*180/Math.PI];}
function meters(p,o){return[(p.lon-o.lon)*Math.PI/180*6378137*Math.cos(o.lat*Math.PI/180),(p.lat-o.lat)*Math.PI/180*6378137];}
function canvas(id,s){const c=$(id),ctx=c.getContext('2d');ctx.fillStyle='#f8fafb';ctx.fillRect(0,0,c.width,c.height);ctx.font='16px sans-serif';ctx.fillStyle='#71858f';ctx.fillText(s.mode==='simulated'?'실내 시험 · 가상 GPS + 실제 DO':'수상 조사 · 실제 GPS + DO',30,28);return ctx;}
function drawGrid(s){const ctx=canvas('grid',s),o=s.origin||s.position||{lat:35.1777,lon:129.1198},size=s.settings.cell_m;let pts=s.route.map(p=>meters(p,o));for(const c of s.cells)pts.push([c.ix*size,c.iy*size],[(c.ix+1)*size,(c.iy+1)*size]);if(!pts.length)pts=[[0,0],[40,30]];const xs=pts.map(p=>p[0]),ys=pts.map(p=>p[1]);const minx=Math.min(...xs)-size,miny=Math.min(...ys)-size,w=Math.max(40,Math.max(...xs)-minx+size),h=Math.max(30,Math.max(...ys)-miny+size),scale=Math.min(780/w,310/h),pos=(x,y)=>[65+(x-minx)*scale,365-(y-miny)*scale];
 ctx.strokeStyle='#e2e9ec';ctx.lineWidth=1;const step=Math.max(size,Math.ceil(Math.max(w,h)/30/size)*size);for(let x=Math.floor(minx/step)*step;x<minx+w;x+=step){const p=pos(x,miny);ctx.beginPath();ctx.moveTo(p[0],55);ctx.lineTo(p[0],365);ctx.stroke();}for(let y=Math.floor(miny/step)*step;y<miny+h;y+=step){const p=pos(minx,y);ctx.beginPath();ctx.moveTo(65,p[1]);ctx.lineTo(845,p[1]);ctx.stroke();}
 for(const c of s.cells){const p=pos(c.ix*size,(c.iy+1)*size);ctx.fillStyle=color(c.mean);ctx.fillRect(p[0],p[1],size*scale,size*scale);ctx.strokeStyle='#fff';ctx.strokeRect(p[0],p[1],size*scale,size*scale);if(size*scale>45){ctx.fillStyle='#fff';ctx.font='15px sans-serif';ctx.fillText(c.mean.toFixed(2),p[0]+5,p[1]+22);}}
 ctx.strokeStyle='#425e6c';ctx.lineWidth=2;ctx.beginPath();s.route.forEach((p,i)=>{const z=pos(...meters(p,o));i?ctx.lineTo(...z):ctx.moveTo(...z);});ctx.stroke();s.route.forEach((p,i)=>{const z=pos(...meters(p,o));ctx.fillStyle='#2d4e5b';ctx.beginPath();ctx.arc(...z,11,0,Math.PI*2);ctx.fill();ctx.fillStyle='#fff';ctx.font='12px sans-serif';ctx.fillText(String(i+1),z[0]-3,z[1]+4);});
 ctx.fillStyle='#71858f';ctx.font='13px sans-serif';ctx.fillText(`동서 ${w.toFixed(0)} m · 남북 ${h.toFixed(0)} m · 빈 칸=미관측`,65,402);if(!s.cells.length){ctx.fillStyle='#71858f';ctx.font='18px sans-serif';ctx.fillText('조사를 시작하면 관측 위치에 색이 채워집니다.',235,210);}}
function drawChart(s){const ctx=canvas('chart',s),rows=s.samples;if(!rows.length){ctx.fillText('아직 기록된 DO 데이터가 없습니다.',270,215);return;}const values=rows.map(r=>r.do_mg_l),lo=Math.min(...values)-.15,hi=Math.max(...values)+.15,first=Date.parse(rows[0].received_utc),last=Date.parse(rows.at(-1).received_utc),span=Math.max(1000,last-first);ctx.font='14px sans-serif';for(let i=0;i<=4;i++){const y=65+i*72;ctx.strokeStyle='#dce5e9';ctx.beginPath();ctx.moveTo(75,y);ctx.lineTo(855,y);ctx.stroke();ctx.fillStyle='#6d818c';ctx.fillText((hi-(hi-lo)*i/4).toFixed(2),18,y+5);}ctx.beginPath();ctx.strokeStyle='#07897d';ctx.lineWidth=3;rows.forEach((r,i)=>{const x=75+(Date.parse(r.received_utc)-first)/span*780,y=353-(r.do_mg_l-lo)/(hi-lo)*288;i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.stroke();ctx.fillStyle='#6d818c';ctx.fillText(new Date(first).toLocaleTimeString('ko-KR'),75,395);ctx.fillText(new Date(last).toLocaleTimeString('ko-KR'),745,395);$('range').textContent=`범위 ${Math.min(...values).toFixed(2)}~${Math.max(...values).toFixed(2)} mg/L · 화면 시간은 이 기기의 현지 시간` ;}
connect();
