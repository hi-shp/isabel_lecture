'use strict';
(() => {
 const el=id=>document.getElementById(id),status=t=>el('captureStatus').textContent=t;
 let session=null,snapshotBusy=false;
 const blobOf=canvas=>new Promise((resolve,reject)=>{canvas.toBlob(b=>b?resolve(b):reject(Error('PNG 생성 실패')),'image/png');});
 function save(blob,name){const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),60000);}
 const stamp=()=>new Date().toISOString().replace(/[:.]/g,'-');
 const panels=()=>[...document.querySelectorAll('[data-panel]')];

 el('capturePage').onclick=async()=>{
  if(snapshotBusy)return;snapshotBusy=true;el('capturePage').disabled=true;status('전체 페이지와 패널 PNG 생성 중');
  try{
   await document.fonts.ready;
   const geometries=panels().map(p=>{const r=p.getBoundingClientRect();return {name:p.dataset.panel,x:r.left+scrollX,y:r.top+scrollY,w:r.width,h:r.height};});
   const scale=devicePixelRatio||1;
   const full=await html2canvas(document.documentElement,{scale,useCORS:true,logging:false,scrollX:0,scrollY:0,
       width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight,
       windowWidth:innerWidth,windowHeight:innerHeight,backgroundColor:'#eef2f3'});
   const zip=new JSZip();zip.file('full-page.png',await blobOf(full));
   for(const p of geometries){const c=document.createElement('canvas');c.width=Math.round(p.w*scale);c.height=Math.round(p.h*scale);c.getContext('2d').drawImage(full,Math.round(p.x*scale),Math.round(p.y*scale),c.width,c.height,0,0,c.width,c.height);zip.file(`panels/${p.name}.png`,await blobOf(c));}
   zip.file('manifest.json',JSON.stringify({utc:new Date().toISOString(),method:'full-page DOM raster at device pixel ratio',
       scale,width:full.width,height:full.height,panels:geometries,mission_id:state?.mission_id,
       color_scale:state?.color_scale,notes:'Full scrolling-page PNG uses DOM rendering; not a native browser screenshot. Cross-origin map tiles depend on CORS.'},null,2));
   save(await zip.generateAsync({type:'blob',compression:'STORE'}),`survey-photos-${stamp()}.zip`);status('전체 페이지와 패널 PNG 저장 완료');
  }catch(e){status('캡처 실패: '+e.message);}finally{snapshotBusy=false;el('capturePage').disabled=false;}
 };

 function recorder(s,stream,name,kind){
  const mime=['video/webm;codecs=vp9','video/webm;codecs=vp8','video/webm'].find(MediaRecorder.isTypeSupported);
  if(!mime)throw Error('이 브라우저는 WebM 녹화를 지원하지 않습니다. Chrome 또는 Edge를 사용하세요.');
  const item={name,kind,stream,chunks:[],frames:0};
  item.rec=new MediaRecorder(stream,{mimeType:mime,videoBitsPerSecond:kind==='native-tab'?24000000:12000000});
  item.done=new Promise(resolve=>item.rec.onstop=resolve);
  item.rec.ondataavailable=e=>{if(e.data.size){item.chunks.push(e.data);s.bytes+=e.data.size;if(s.bytes>512*1024*1024&&!s.stopping)stop('메모리 보관 한도에 도달해 저장합니다.');}};
  item.rec.onerror=e=>{s.errors.push(e.error?.message||'MediaRecorder error');stop('녹화 오류로 저장합니다.');};
  item.rec.start(1000);s.recorders.push(item);return item;
 }

 el('recordStart').onclick=async()=>{
  if(session)return;
  if(!isSecureContext||!navigator.mediaDevices?.getDisplayMedia){status('녹화는 노트북의 http://localhost:8787 또는 HTTPS에서 열어 주세요. 실행 방법은 사용 설명에 있습니다.');return;}
  if(!HTMLVideoElement.prototype.requestVideoFrameCallback){status('원본 프레임 콜백을 지원하는 최신 Chrome/Edge가 필요합니다.');return;}
  el('recordStart').disabled=true;
  let native=null,created=null;
  try{
   const handle='water-survey-'+Math.random().toString(36).slice(2);
   navigator.mediaDevices.setCaptureHandleConfig?.({handle,exposeOrigin:true,permittedOrigins:[location.origin]});
   native=await navigator.mediaDevices.getDisplayMedia({video:{displaySurface:'browser',width:{ideal:innerWidth*devicePixelRatio},height:{ideal:innerHeight*devicePixelRatio},frameRate:{ideal:60}},audio:false,preferCurrentTab:true,selfBrowserSurface:'include',surfaceSwitching:'exclude',monitorTypeSurfaces:'exclude'});
   const track=native.getVideoTracks()[0],cfg=track.getSettings(),captureHandle=track.getCaptureHandle?.();
   if(cfg.displaySurface!=='browser')throw Error('현재 대시보드 탭을 선택해 주세요.');
   if(captureHandle?.handle&&captureHandle.handle!==handle)throw Error('다른 탭이 선택됐습니다. 현재 대시보드 탭을 선택해 주세요.');
   const s={stream:native,video:document.createElement('video'),recorders:[],frames:[],errors:[],bytes:0,started:new Date().toISOString(),settings:cfg,stopping:false,mission:state?.mission_id};
   session=s;created=s;s.video.muted=true;s.video.srcObject=native;await s.video.play();
   s.nativeRecorder=recorder(s,native,'full-tab.webm','native-tab');
   // These canvases are the actual canvases displayed by the dashboard, including while below the fold.
   s.canvases=['grid','chart'].map(id=>{const canvas=el(id),stream=canvas.captureStream(0);const item=recorder(s,stream,`${id}.webm`,'dashboard-canvas');return {id,track:stream.getVideoTracks()[0],item};});
   s.paint=()=>{if(s.stopping)return;for(const c of s.canvases){c.track.requestFrame();c.item.frames++;}s.frames.push({kind:'canvas-paint',utc:new Date().toISOString(),monotonic_ms:performance.now()});};
   document.addEventListener('survey-render',s.paint);s.paint();
   const canLocate=!!captureHandle?.handle;
   s.crops=canLocate?panels().filter(p=>!['grid','chart'].includes(p.dataset.panel)).map(p=>({element:p,canvas:null,item:null,skipped:0})):[];
   if(!canLocate)s.errors.push('Capture Handle not available; DOM panel crops omitted to avoid capturing incorrect regions.');
   const frame=(now,meta)=>{
    if(s.stopping)return;
    const sx=s.video.videoWidth/innerWidth,sy=s.video.videoHeight/innerHeight;
    s.nativeRecorder.frames++;
    s.frames.push({kind:'native',utc:new Date().toISOString(),monotonic_ms:now,media_time:meta.mediaTime,presented_frames:meta.presentedFrames,width:s.video.videoWidth,height:s.video.videoHeight,scroll_x:scrollX,scroll_y:scrollY});
    for(const c of s.crops){const r=c.element.getBoundingClientRect();
     // Only encode fully visible source pixels. Never fabricate hidden parts or duplicate old frames.
     if(r.left<0||r.top<0||r.right>innerWidth||r.bottom>innerHeight||r.width<1||r.height<1){c.skipped++;continue;}
     const w=Math.round(r.width*sx),h=Math.round(r.height*sy);
     if(!c.canvas){c.canvas=document.createElement('canvas');c.canvas.width=w;c.canvas.height=h;c.item=recorder(s,c.canvas.captureStream(0),`${c.element.dataset.panel}.webm`,'native-panel-crop');}
     if(c.canvas.width!==w||c.canvas.height!==h){c.skipped++;continue;}
     c.canvas.getContext('2d').drawImage(s.video,r.left*sx,r.top*sy,w,h,0,0,w,h);
     c.item.stream.getVideoTracks()[0].requestFrame();c.item.frames++;
    }
    s.video.requestVideoFrameCallback(frame);
   };
   s.video.requestVideoFrameCallback(frame);
   track.addEventListener('ended',()=>stop('화면 공유가 종료됐습니다.'));
   s.timer=setInterval(()=>status(`녹화 중 ${Math.floor((Date.now()-Date.parse(s.started))/1000)}초 · ${(s.bytes/1048576).toFixed(1)} MB · ${cfg.width}×${cfg.height}`),1000);
   el('recordStop').disabled=false;status('녹화 중 · 현재 탭을 스크롤할 수 있습니다.');
  }catch(e){if(created){created.errors.push(e.message);await stop('녹화 준비 실패');}else native?.getTracks().forEach(t=>t.stop());status('녹화 시작 실패: '+e.message);el('recordStart').disabled=false;}
 };

 async function stop(message='녹화를 종료하고 저장합니다.'){
  const s=session;if(!s||s.stopping)return;s.stopping=true;clearInterval(s.timer);document.removeEventListener('survey-render',s.paint);el('recordStop').disabled=true;status(message);
  try{
   for(const r of s.recorders)if(r.rec.state!=='inactive')r.rec.stop();
   await Promise.all(s.recorders.map(r=>r.done));
   s.stream.getTracks().forEach(t=>t.stop());for(const r of s.recorders)r.stream.getTracks().forEach(t=>t.stop());
   const zip=new JSZip();for(const r of s.recorders)if(r.kind==='native-tab'||r.frames)zip.file(r.name,new Blob(r.chunks,{type:r.rec.mimeType}));
   zip.file('frames.jsonl',s.frames.map(f=>JSON.stringify(f)).join('\n'));
   zip.file('manifest.json',JSON.stringify({started:s.started,finished:new Date().toISOString(),mission_id:s.mission,source_settings:s.settings,
       recordings:s.recorders.map(r=>({file:r.name,kind:r.kind,frames:r.frames,mime:r.rec.mimeType})),
       frame_count_meaning:'Observed source callbacks / requested canvas frames, not a decoded encoded-frame count.',
       omitted_frames:s.crops.map(c=>({panel:c.element.dataset.panel,not_fully_visible_or_resized:c.skipped})),errors:s.errors,
       notes:'Native tab video retains delivered source timing and resolution. WebM encoding is lossy. No fixed-FPS duplication or upscaling is performed. Grid/chart record actual dashboard canvas paints. Other panel crops contain fully visible native frames only; hidden panels cannot be recovered from tab pixels. Full-tab video records the visible viewport and scrolling, not the entire scroll height at once.'},null,2));
   zip.file('README.txt','full-tab.webm: 브라우저 원본 탭 해상도/전달 프레임 기반 영상. 스크롤한 화면도 기록됩니다.\ngrid.webm / chart.webm: 실제 대시보드 캔버스가 그려진 프레임.\nmap/status/plan.webm: 화면에 완전히 보인 구간만 원본 탭에서 자릅니다. 화면 밖 패널은 생성되지 않을 수 있습니다.\nframes.jsonl/manifest.json에 실제 시각과 생략 내역을 기록합니다. WebM은 압축 영상이며 무손실 원본 비트스트림이 아닙니다.\n');
   save(await zip.generateAsync({type:'blob',compression:'STORE'}),`survey-video-${stamp()}.zip`);status('원본 탭 영상과 패널 영상 저장 완료');
  }catch(e){status('영상 묶음 저장 실패: '+e.message);}finally{session=null;s.video.srcObject=null;el('recordStart').disabled=false;}
 }
 el('recordStop').onclick=()=>stop();
 window.addEventListener('beforeunload',e=>{if(session){e.preventDefault();e.returnValue='';}});
})();
