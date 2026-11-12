// 由 app/web/index.html 按功能域切出，逻辑未改。
// 改动请只动本文件；跨模块依赖看下面的 import。

import { $, $$, S, api, esc, fmt } from './core.js';
import { setPlayhead } from './player.js';
function hideGres(){ $('#gres').classList.remove('show') }
function renderGres(d){
  var box=$('#gres');
  var rows=d.rows||[];
  if(!rows.length){
    box.innerHTML='<div class="gempty">没有匹配的发言</div>';
    box.classList.add('show');
    return;
  }
  box.innerHTML='<div class="gh">找到 '+d.count+' 条 · 跨 '+new Set(rows.map(function(r){return r.session_id})).size+' 场</div>'
    + rows.slice(0,30).map(function(r){
        return '<div class="gi" data-s="'+esc(r.session_id)+'" data-t="'+r.t_start+'">'
          +'<div class="gt"><span>'+esc((r.started_at||'').slice(0,10))+'</span>'
          +'<span>'+fmt(r.t_start)+'</span><span>'+esc(r.speaker_label)+'</span>'
          +'<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(r.title)+'</span></div>'
          +'<div class="ex">'+mark(r.text,d.q)+'</div></div>';
      }).join('');
  box.classList.add('show');
  $$('#gres .gi').forEach(function(el){
    el.onclick=function(){
      var sid=el.dataset.s, t=Number(el.dataset.t);
      hideGres();
      if(sid===S.session){ setPlayhead(t,'搜索定位'); return }
      location.href='/?session='+encodeURIComponent(sid)+'&t='+t;
    };
  });
}
function mark(text,q){
  var s=esc(text||'');
  if(!q)return s;
  var e=esc(q);
  var i=s.toLowerCase().indexOf(e.toLowerCase());
  if(i<0)return s;
  return s.slice(0,i)+'<b>'+s.slice(i,i+e.length)+'</b>'+s.slice(i+e.length);
}
async function doSearch(){
  var q=$('#gq').value.trim();
  if(!q){ hideGres(); return }
  try{
    var d=await api('/api/search?q='+encodeURIComponent(q)+'&limit=60');
    renderGres(d);
  }catch(e){ hideGres() }
}

export { doSearch, hideGres, mark, renderGres };
