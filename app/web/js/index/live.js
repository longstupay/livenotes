// 由 app/web/index.html 按功能域切出，逻辑未改。
// 改动请只动本文件；跨模块依赖看下面的 import。

import { $, api, esc, post, toast } from './core.js';
import { livePoll, liveStop } from './workflow.js';
const L={sid:'',active:false,stream:null,ctx:null,node:null,src:null,poll:null,chain:Promise.resolve(),
  err:'',paused:false,pauseAt:0,pausePending:false,marks:[],font:14,phase:'idle',
  hist:[],hasMore:true,total:0,loading:false,cinema:false};
const WORKLET=`class PCM16 extends AudioWorkletProcessor{
  constructor(){super();this.buf=new Int16Array(4096);this.n=0;this.sum=0;}
  process(inputs){
    const ch=inputs[0]&&inputs[0][0];
    if(!ch)return true;
    for(let i=0;i<ch.length;i++){
      let x=ch[i];x=Math.max(-1,Math.min(1,x));
      this.buf[this.n++]=x<0?x*32768:x*32767;
      this.sum+=x*x;
      if(this.n===this.buf.length){
        const out=this.buf.slice(0);
        const rms=Math.sqrt(this.sum/this.n);
        this.port.postMessage({pcm:out.buffer,rms:rms},[out.buffer]);
        this.n=0;this.sum=0;
      }
    }
    return true;
  }
}
registerProcessor('pcm16',PCM16);`;
function liveShow(v){$('#livePanel').classList.toggle('show',v)}
function liveWs(v){$('#liveWs').classList.toggle('show',v)}
function liveMini(v){$('#liveMini').classList.toggle('show',v)}
async function liveOpen(){
  if(L.active){liveMini(false);liveWs(true);return}
  liveShow(true);$('#lpErr').textContent='';
  $('#lpSetup').style.display='';
  if(!window.isSecureContext){
    // 浏览器只在 localhost/127.0.0.1 下暴露 getDisplayMedia；0.0.0.0 和局域网 IP 都不算。
    // 这里提前说清楚并给跳转，别等用户点了按钮才看到一句看不懂的报错。
    const local='http://127.0.0.1:'+(location.port||'8765')+'/';
    $('#lpReady').innerHTML='当前地址不是浏览器认可的「安全上下文」，无法采集标签页音频。'+
      '<br><a href="'+local+'" style="color:#2b5cff">→ 点这里用 '+local+' 打开</a>'+
      '（本机监听必须走 127.0.0.1，手机看回放才用局域网 IP）';
    $('#lpStart').disabled=true;
    return;
  }
  try{
    const info=await api('/api/live/info');
    $('#lpReady').innerHTML=info.ready
      ? `模型就绪 · ${esc(info.engine)}`
      : `未就绪：缺少 ${info.missing.map(m=>esc(m.split(/[\\/]/).pop())).join('、')}<br>先运行 <code>python tools/fetch_models.py</code>`;
    $('#lpStart').disabled=!info.ready;
  }catch(e){$('#lpReady').textContent='服务端检查失败：'+e.message}
}
async function liveStart(){
  if(L.phase==='stopping'){ toast('上一场还在保存，等它跑完再开新的'); return }
  const btn=$('#lpStart');btn.disabled=true;$('#lpErr').textContent='';
  let started=null;
  try{
    if(!navigator.mediaDevices||!navigator.mediaDevices.getDisplayMedia)
      throw new Error('当前浏览器不支持标签页采集，请用 Chrome / Edge 打开 http://127.0.0.1:8765/');
    const info=await api('/api/live/info');
    if(!info.ready)throw new Error('模型未就绪，请先运行 python tools/fetch_models.py');
    started=await post('/api/live/start',{title:'抖音直播 '+new Date().toLocaleString('zh-CN',{hour12:false})});
    let stream;
    try{
      stream=await navigator.mediaDevices.getDisplayMedia({
        video:true,
        audio:{echoCancellation:false,noiseSuppression:false,autoGainControl:false,channelCount:1},
        selfBrowserSurface:'exclude',surfaceSwitching:'include',preferCurrentTab:false
      });
    }catch(e){
      await post('/api/live/cancel',{session_id:started.session_id}).catch(()=>{});
      throw new Error('没有选择标签页，或已取消共享');
    }
    const at=stream.getAudioTracks();
    if(!at.length){
      stream.getTracks().forEach(t=>t.stop());
      await post('/api/live/cancel',{session_id:started.session_id}).catch(()=>{});
      throw new Error('没有拿到音频：请在弹窗底部勾选「分享标签页音频」后重试');
    }
    if(at[0].getSettings && at[0].getSettings().displaySurface && at[0].getSettings().displaySurface!=='browser')
      toast('建议选择「标签页」而不是整个屏幕');
    const ctx=new AudioContext({sampleRate:16000});
    if(Math.abs(ctx.sampleRate-16000)>1){
      await ctx.close();stream.getTracks().forEach(t=>t.stop());
      await post('/api/live/cancel',{session_id:started.session_id}).catch(()=>{});
      throw new Error('浏览器未按 16kHz 采样（'+ctx.sampleRate+'Hz），请用 Chrome/Edge');
    }
    await ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([WORKLET],{type:'application/javascript'})));
    L.ctx=ctx;L.stream=stream;L.sid=started.session_id;L.active=true;L.err='';
    L.hist=[];L.hasMore=true;L.total=0;L.loading=false;
    const v=$('#lpVideo');
    if(stream.getVideoTracks().length){
      v.srcObject=new MediaStream(stream.getVideoTracks());
      v.style.display='block';
      v.play().catch(()=>{});
    }else v.style.display='none';
    $('#wsVideo').classList.toggle('has-video',stream.getVideoTracks().length>0);
    L.src=ctx.createMediaStreamSource(stream);
    L.node=new AudioWorkletNode(ctx,'pcm16',{numberOfInputs:1,numberOfOutputs:1,outputChannelCount:[1]});
    const gain=ctx.createGain();gain.gain.value=0;
    L.src.connect(L.node);L.node.connect(gain);gain.connect(ctx.destination);
    L.chain=Promise.resolve();
    L.node.port.onmessage=e=>{
      const {pcm,rms}=e.data;
      if(L.paused)return;               // 暂停时直接丢块，不排队也不累计
      liveLevel(rms);
      L.chain=L.chain.then(()=>liveSend(pcm)).catch(err=>{if(!L.err)L.err=err.message});
    };
    at[0].onended=()=>{if(L.active)liveStop()};
    $('#lpList').innerHTML='<div class="ws-empty">等待说话…<br>主播开口后约 1 秒出字</div>';
    $('#lpSpeakers').innerHTML='';$('#wsMarks').innerHTML='';$('#wsErr').textContent='';
    $('#lpStats').textContent='—';$('#lpTimer').textContent='00:00:00';$('#wsRatio').innerHTML='';
    L.paused=false;L.pauseAt=0;L.pausePending=false;L.marks=[];L.font=14;
    document.documentElement.style.setProperty('--lpfs','14px');
    paintPauseUI();
    liveShow(false);liveMini(false);liveWs(true);
    toast('已开始监听标签页音频');
    livePoll();
  }catch(e){
    $('#lpErr').textContent=e.message;
    if(started&&!L.active)await post('/api/live/cancel',{session_id:started.session_id}).catch(()=>{});
    btn.disabled=false;
  }
}
async function liveSend(buf){
  if(!L.sid)return;
  const r=await fetch('/api/live/chunk',{method:'POST',
    headers:{'Content-Type':'application/octet-stream','X-Live-Id':L.sid},body:buf});
  if(!r.ok){
    const d=await r.json().catch(()=>({}));
    if(r.status===404)L.sid='';
    throw new Error(d.error||('HTTP '+r.status));
  }
}
function liveLevel(rms){
  const p=Math.min(1,Math.max(0,rms*8));
  const el=$('#lpLevel');el.style.width=(p*100).toFixed(0)+'%';
  el.style.background=rms>0.02?'var(--ok)':'var(--accent)';
}
/* 暂停状态统一在这里画：工作台按钮、小胶囊、视频上的遮罩都跟着 L.paused 走 */
function paintPauseUI(){
  const p=!!L.paused;
  $('#wsPause').textContent=p?'▶ 继续':'⏸ 暂停';
  $('#wsCinePause').textContent=p?'▶ 继续':'⏸ 暂停';
  $('#wsPause').classList.toggle('on',p);
  $('#wsPause').title=p?'继续监听（空格）':'暂停监听（空格）';
  $('#miniPause').textContent=p?'▶':'⏸';
  $('#miniPause').classList.toggle('on',p);
  $('#miniPause').title=p?'继续监听':'暂停监听';
  $('#miniText').textContent=p?'已暂停':'监听中';
  $('#liveMini').classList.toggle('paused',p);
  $('#wsVideo').classList.toggle('paused',p);
}
/* 字幕：本地累积 + 上滑加载更早的句子。
   以前每次轮询都用 status 里最后 60 句全量重绘，既数不出总数、也翻不到历史。 */

export { L, WORKLET, liveLevel, liveMini, liveOpen, liveSend, liveShow, liveStart, liveWs, paintPauseUI };
