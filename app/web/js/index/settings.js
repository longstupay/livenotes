// 由 app/web/index.html 按功能域切出，逻辑未改。
// 改动请只动本文件；跨模块依赖看下面的 import。

import { $, $$, api, esc, post, toast } from './core.js';
import { askText } from './dialog.js';
import { mark } from './search.js';
var LLM_PRESETS=[
  {id:'deepseek',name:'DeepSeek',base:'https://api.deepseek.com/v1',model:'deepseek-chat'},
  {id:'openai',name:'OpenAI',base:'https://api.openai.com/v1',model:'gpt-4o-mini'},
  {id:'moonshot',name:'Moonshot Kimi',base:'https://api.moonshot.cn/v1',model:'moonshot-v1-8k'},
  {id:'zhipu',name:'智谱 GLM',base:'https://open.bigmodel.cn/api/paas/v4',model:'glm-4-flash'},
  {id:'qwen',name:'通义千问',base:'https://dashscope.aliyuncs.com/compatible-mode/v1',model:'qwen-plus'},
  {id:'groq',name:'Groq',base:'https://api.groq.com/openai/v1',model:'llama-3.3-70b-versatile'},
  {id:'ollama',name:'Ollama 本地',base:'http://127.0.0.1:11434/v1',model:'qwen2.5'},
  {id:'custom',name:'自定义 / 中转',base:'',model:''}];
var LLMCFG={provider:''};
var SET={active:'',profiles:[],editing:'',isNew:false,lastState:null};
var MODELS=[];

// 「桌面版专属」选项由宿主清单声明能力（profile.capabilities），页面里不再
// 出现壳注入的全局标记；网页版没有 native-windows 就不显示这一行。
function updateDesktopRow(s){
  var openRow=$('#prefOpenModeRow'), openSel=$('#prefOpenMode');
  if(!openRow||!openSel) return;
  var able=!!(window.LN&&window.LN.host&&window.LN.host.has('native-windows'));
  openRow.style.display=able?'':'none';
  if(!able) return;
  var st=s||SET.lastState||{};
  openSel.value=(st.prefs&&st.prefs.open_mode)||'internal';
}
document.addEventListener('ln:host-ready',function(){updateDesktopRow(SET.lastState)});

function setStatus(text,cls){
  var el=$('#setStatus');
  el.textContent=text;
  el.className='set-status'+(cls?(' '+cls):'');
}
function renderProviders(){
  $('#provList').innerHTML=LLM_PRESETS.map(function(p){
    return '<button data-p="'+p.id+'" class="'+(LLMCFG.provider===p.id?'on':'')+'">'+p.name+'</button>';
  }).join('');
  $$('#provList button').forEach(function(b){
    b.onclick=function(){
      var p=LLM_PRESETS.filter(function(x){return x.id===b.dataset.p})[0];
      LLMCFG.provider=p.id;
      if(p.base)$('#setBase').value=p.base;
      if(p.model)$('#setModel').value=p.model;
      if(!$('#setName').value.trim())$('#setName').value=p.name;
      renderProviders();
    };
  });
}
function fillForm(p){
  SET.curName=p?(p.name||''):'新配置';
  $('#setBase').value=p?(p.base||''):'';
  $('#setModel').value=p?(p.model||''):'';
  var t=(p&&p.temperature!=null)?p.temperature:0.3;
  $('#setTemp').value=t;
  $('#setTempVal').textContent=t;
  $('#setKey').value='';
  $('#setKeyHint').textContent=(p&&p.has_key)
    ? ('已保存 Key '+p.key_hint+'，留空则不修改；仅存本机 data/settings.json。')
    : '仅保存在本机 data/settings.json，不经过任何中间服务器。';
  $('#curName').textContent=SET.curName;
  LLMCFG.provider=p?(p.provider||''):'';
  renderProviders();
}
function renderProfiles(){
  var list=SET.profiles||[];
  if(!list.length){
    $('#setProfiles').innerHTML='<div class="pempty">还没有配置，点右上「＋ 新建」</div>';
    $('#curName').textContent='—';
    return;
  }
  $('#setProfiles').innerHTML=list.map(function(p){
    var on=p.id===SET.editing;
    var host=(p.base||'').replace(/^https?:\/\//,'');
    return '<div class="prow'+(on?' on':'')+'" data-id="'+esc(p.id)+'">'
      +'<div style="flex:1;min-width:0">'
      +'<div class="pn">'+esc(p.name)+(p.configured?'':' <span style="color:#d48806;font-weight:400">（未填全）</span>')+'</div>'
      +'<div class="pm">'+esc(p.model||'没填模型')+(host?' · '+esc(host):'')+'</div>'
      +'</div>'
      +'<div class="pact">'
      +'<button data-a="rename" title="改名">✎</button>'
      +'<button data-a="dup" title="复制这份">⧉</button>'
      +'<button data-a="del" class="del" title="删除这份">✕</button>'
      +'</div></div>';
  }).join('');
  $$('#setProfiles .prow').forEach(function(row){
    row.onclick=function(e){
      var b=e.target.closest('button');
      if(b){ e.stopPropagation(); profileAction(b.dataset.a,row.dataset.id); return }
      if(row.dataset.id===SET.editing)return;
      post('/api/settings/activate',{id:row.dataset.id}).then(applyState)
        .catch(function(err){toast('切换失败：'+err.message)});
    };
  });
}
async function profileAction(a,id){
  var p=(SET.profiles||[]).filter(function(x){return x.id===id})[0]||{};
  if(a==='rename'){
    var name=await askText({title:'给这份配置改名',
      desc:'只改显示名，地址、模型、Key 都不变。',
      value:p.name||'',placeholder:'例如：DeepSeek 官方 / 中转 A'});
    if(name==null)return;
    name=name.trim(); if(!name)return;
    if(name===p.name)return;
    try{
      await post('/api/settings',{id:id,name:name});
      applyState(await api('/api/settings'));
      toast('已改名为「'+name+'」');
    }catch(e){ toast('改名失败：'+e.message) }
    return;
  }
  if(a==='dup'){
    try{
      applyState(await post('/api/settings/duplicate',{id:id}));
      toast('已复制，改一下就是新账号');
    }catch(e){ toast('复制失败：'+e.message) }
    return;
  }
  if(a==='del'){
    if(!confirm('删除配置「'+(p.name||'')+'」？\n\n只删这份配置，已录的场次和总结不受影响。'))return;
    try{
      applyState(await post('/api/settings/delete',{id:id}));
      toast('已删除');
    }catch(e){ toast('删除失败：'+e.message) }
  }
}
function applyState(s){
  SET={active:s.active||'',profiles:s.profiles||[],editing:s.active||'',isNew:false,curName:'',lastState:s};
  if(s.prefs&&s.prefs.topics){ const el=$('#prefTopics'); if(el) el.value=s.prefs.topics }
  const df=s.direct_fallback;
  const sel=$('#prefDirectFallback'), hint=$('#prefDirectHint');
  if(sel){
    if(!df){
      // 老服务进程还没有这个字段：别让人以为改了会生效。
      sel.value='ask';
      sel.disabled=true;
      if(hint) hint.textContent='当前服务还是旧版本：重启服务与采集代理后，这里才能配置。';
    }else{
      sel.value=df.mode||'ask';
      sel.disabled=df.source==='env';
      if(hint){
        hint.textContent = df.source==='env'
          ? '当前由环境变量 LIVENOTES_DIRECT_FALLBACK='+(df.mode||'')+' 指定；去掉环境变量后，这里的选择才生效。'
          : df.mode==='none'
            ? '只把房间标成失败并给出原因，不提供浏览器切换。'
            : df.mode==='ask'
              ? '直取失败时会在收听页停下来，等你点「切换到浏览器采集」；不会自己打开浏览器。'
              : '直取失败后自动切到浏览器采集，不再询问。';
      }
    }
  }
  updateDesktopRow(s);
  var capSel=$('#prefCaptureMode');
  if(capSel) capSel.value=(s.prefs&&s.prefs.capture_mode)||'browser';
  var closeSel=$('#prefCloseAction');
  if(closeSel) closeSel.value=(s.prefs&&s.prefs.close_action)||'tray';
  renderProfiles();
  var cur=(SET.profiles.filter(function(p){return p.id===SET.editing})[0])||null;
  fillForm(cur);
  if(!cur){
    setStatus('还没有可用的配置：点「＋ 新建」填一份，或先选上面任意一行。','');
    return;
  }
  setStatus(s.configured
    ? ('当前使用：'+cur.name+' · '+(cur.model||'')+' @ '+(cur.base||''))
    : '当前档案还没填全：总结会走本地抽取式，自动标题只能给摘录。',
    s.configured?'ok':'');
}
async function openSettings(){
  $('#setMask').classList.add('show');
  $('#setDrawer').classList.add('show');
  $('#setModels').innerHTML='';MODELS=[];
  try{ applyState(await api('/api/settings')) }
  catch(e){ setStatus('读取设置失败：'+e.message,'err') }
}
function closeSettings(){
  $('#setMask').classList.remove('show');
  $('#setDrawer').classList.remove('show');
}
function formBody(){
  var b={id:SET.editing||'',name:SET.curName||'新配置',
         provider:LLMCFG.provider||'',base:$('#setBase').value.trim(),
         model:$('#setModel').value.trim(),temperature:Number($('#setTemp').value)};
  if(SET.isNew)b.new=1;
  var k=$('#setKey').value.trim();
  if(k)b.key=k;
  return b;
}
async function saveSettings(){
  try{
    var s=await post('/api/settings',formBody());
    applyState(s);
    toast('已保存');
  }catch(e){ setStatus('保存失败：'+e.message,'err') }
}
function newProfile(){
  SET.editing='';
  SET.isNew=true;
  SET.curName='新配置';
  fillForm(null);
  renderProfiles();
  $('#setBase').focus();
  setStatus('新建档案：填地址和 Key，点「测试连接」或「保存」');
}
async function testLLM(){
  var b=formBody();
  setStatus('正在测试连接…');
  var saved=null;
  try{ saved=await post('/api/settings',b) }catch(e){}
  try{
    var r=await post('/api/llm/test',b);
    if(!r.ok){
      if(saved)applyState(saved);
      setStatus('连接失败：'+(r.error||'未知错误')+'（配置已保存，可随时重试）','err');
      return;
    }
    if(saved)applyState(saved);
    setStatus('连接正常：'+r.model+' · '+r.ms+'ms'+(r.reply?(' · 回复「'+r.reply+'」'):'')+'，已保存','ok');
    toast('已连接并保存');
  }catch(e){ setStatus('测试失败：'+e.message,'err') }
}
function renderModelChips(list, results){
  var byName={};
  (results||[]).forEach(function(r){byName[r.model]=r});
  $('#setModels').innerHTML=list.map(function(m){
    var r=byName[m];
    var cls=r?(r.ok?'ok':'bad'):'';
    var tip=r?(r.ok?('可用 · '+r.ms+'ms'):(r.error||'不可用')):'';
    var mark=r?(r.ok?'✓ ':'✕ '):'';
    return '<span class="'+cls+'" data-m="'+esc(m)+'" title="'+esc(tip)+'">'+mark+esc(m)+'</span>';
  }).join('');
  $$('#setModels span').forEach(function(el){
    el.onclick=function(){$('#setModel').value=el.dataset.m};
  });
}
async function probeModels(){
  if(!MODELS.length){ setStatus('先点「拉取列表」，再逐个试','err'); return }
  var base=$('#setBase').value.trim();
  if(!base){ setStatus('先填接口地址','err'); return }
  var body={base:base,models:MODELS};
  var kk=$('#setKey').value.trim();
  if(kk)body.key=kk;
  setStatus('正在逐个试 '+MODELS.length+' 个模型，可能要十几秒…');
  try{
    var r=await post('/api/llm/probe',body);
    if(!r.ok){ setStatus('探测失败：'+(r.error||''),'err'); return }
    var okAll=(r.results||[]).filter(function(x){return x.ok});
    renderModelChips(MODELS, r.results);
    if(okAll.length){
      setStatus('可用 '+okAll.length+' 个：'+okAll.map(function(x){return x.model}).join('、')
        +'。点一个填进上面，再点「测试连接」保存。','ok');
    }else{
      setStatus('这些模型全部失败——问题在服务端（中转站上游或 Key 余额），不是你填错。把下面划掉的错误发我。','err');
    }
  }catch(e){ setStatus('探测失败：'+e.message,'err') }
}
async function fetchModels(){
  var base=$('#setBase').value.trim();
  if(!base){ setStatus('先在「接口地址」填好再拉取，或直接手填模型名','err'); return }
  var body={base:base};
  var kk=$('#setKey').value.trim();
  if(kk)body.key=kk;
  $('#setModels').innerHTML='<span>拉取中…</span>';
  try{
    var r=await post('/api/llm/models',body);
    if(!r.ok){ $('#setModels').innerHTML='';setStatus('拉取失败：'+(r.error||''),'err');return }
    var list=(r.models||[]).slice(0,40);
    MODELS=list;
    renderModelChips(list,null);
    setStatus('拉到 '+list.length+' 个模型。点一个填进上面，或点「逐个试」看哪些真能用');
  }catch(e){ $('#setModels').innerHTML='';setStatus('拉取失败：'+e.message,'err') }
}
/* ---------------- 全局搜索（跨场次） ---------------- */

export { LLMCFG, LLM_PRESETS, MODELS, SET, applyState, closeSettings, fetchModels, fillForm, formBody, newProfile, openSettings, probeModels, profileAction, renderModelChips, renderProfiles, renderProviders, saveSettings, setStatus, testLLM };
