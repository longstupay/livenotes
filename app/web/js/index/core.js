// 由 app/web/index.html 按功能域切出，逻辑未改。
// 改动请只动本文件；跨模块依赖看下面的 import。


const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// 分类色和说话人色只从 CSS 变量取：基座在 index.css 里定义冷色，宿主皮肤
// （desktop/web）覆盖同名变量就换成暖纸色。JS 里不再出现「如果主题是某某」
// 这种分支——换肤是 CSS 的事。
const SPK_COLORS = Array.from({length:10},(_,i)=>`var(--spk-${i})`);
const CAT_META = {
  substantive: ['实质讨论', 'var(--cat-substantive)'],
  question_consult: ['上麦咨询', 'var(--cat-question_consult)'],
  host_control: ['主持控场', 'var(--cat-host_control)'],
  chitchat: ['闲聊', 'var(--cat-chitchat)'],
  ad: ['广告引流', 'var(--cat-ad)'],
};
const spkColor=id=>SPK_COLORS[(parseInt(String(id).replace(/\D/g,''),10)||0)%SPK_COLORS.length];
const fmt=s=>{s=Math.max(0,Math.floor(s||0));return [s/3600,(s%3600)/60,s%60].map(x=>String(Math.floor(x)).padStart(2,'0')).join(':')};
const short=s=>fmt(s).replace(/^00:/,'');

const S={sumCache:{},session:'',duration:7200,meta:null,cats:new Set(),spks:new Set(),q:'',
  tFrom:0,tTo:7200,exLow:true,exOvl:false,onlyHigh:false,mode:'content',
  rows:[],hl:new Set(),manualHl:new Set(),busy:false,playhead:0,sessions:[]};

function toast(m){const t=$('#toast');t.textContent=m;t.classList.add('show');clearTimeout(t._t);t._t=setTimeout(()=>t.classList.remove('show'),2600)}
async function api(path,opt){const r=await fetch(path,opt);const ct=r.headers.get('content-type')||'';
  if(!ct.includes('json')){const e=new Error('HTTP '+r.status);e.status=r.status;throw e}
  const d=await r.json();if(d.error&&!r.ok)throw new Error(d.error);return d}
function post(path,body){return api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})}
// 工作台嵌在桌面壳 iframe 里：换场次、跳时间码都通知父窗口，避免整页重载。
function notifyParent(type,data){try{if(parent&&parent!==window)parent.postMessage(Object.assign({type:type},data||{}),'*')}catch(e){}}
// 页面状态事件：宿主（桌面壳或任何嵌入方）监听 document 上的 ln:* 即可，
// 不用猜页面内部函数。约定见 docs/29-page-host-contract.md。
function emitHost(name,detail){try{document.dispatchEvent(new CustomEvent('ln:'+name,{detail:detail||{}}))}catch(e){}}
function copyText(text){text=String(text==null?'':text);if(!text)return Promise.resolve(false);
  if(navigator.clipboard&&navigator.clipboard.writeText)
    return navigator.clipboard.writeText(text).then(()=>true,()=>fallback());
  return Promise.resolve(fallback());
  function fallback(){try{const ta=document.createElement('textarea');ta.value=text;
    ta.style.cssText='position:fixed;left:-9999px';document.body.appendChild(ta);ta.select();
    document.execCommand('copy');ta.remove();return true}catch(e){return false}}}

export { $, $$, CAT_META, S, SPK_COLORS, api, copyText, emitHost, esc, fmt, notifyParent, post, short, spkColor, toast };
