// 由 app/web/index.html 按功能域切出，逻辑未改。
// 改动请只动本文件；跨模块依赖看下面的 import。

import { $, S, api, esc, post } from './core.js';
import { refresh } from './filters.js';
import { loadMeta } from './session.js';
async function classifyLoop(startOff,startTopics,force){
  let off=startOff||0, topics=startTopics||null, total=0, changed=0;
  const note=$('#clsNote');
  while(true){
    let r;
    try{
      r=await post('/api/classify/ai',{session_id:S.session,offset:off,limit:120,
        topics:topics,force:!!force});
      force=false;                 // 只有第一片需要 force，后续沿用同一套主题
    }catch(e){
      const msg=/Failed to fetch|NetworkError/i.test(e.message||'')
        ? '服务连不上（很可能刚热重载重启了）' : (e.message||'网络中断');
      return {error:msg,off:off,topics:topics,total:total,changed:changed};
    }
    if(!r.ok) return {error:r.error||'分类失败',off:off,topics:topics,total:total,changed:changed};
    topics=r.topics||topics; total=r.total||total; changed+=r.changed||0; off=r.next_offset;
    if(note)note.textContent='AI 标注中… '+off+'/'+total+' 句'
      +(r.remaining?('（还差 '+r.remaining+'）'):'')
      +(topics&&topics.length?(' · '+topics.length+' 个主题'):'');
    if(r.done) return {ok:true,off:off,topics:topics,total:total,changed:changed,
      reused:!!r.reused_topics};
  }
}
async function runClassifyAI(force){
  if(!S.session)return;
  const a=$('#btnClassifyAI');
  if(a.dataset.busy==='1')return;
  const ai=(S.meta&&S.meta.ai)||{};
  const labeled=ai.labeled||0,total=ai.total||0;
  const msg=force
    ? '全部重新标注？\n\n会把这一场每句话重标一遍，并把主题集换新——token 消耗最大，通常只在「主题分得不对」时才需要。'
    : (labeled>0&&labeled>=total
        ? '这一场 '+labeled+' 句都标过了，没有需要补的句子。\n\n要换主题重标，请点右上「全部重标」。'
        : '用大模型标注这一场每句话的类别？\n\n只处理还没标过的句子（通常是直播新增的），已经标过的不重做；原来的规则分类会保留，随时可还原。');
  if(!confirm(msg))return;
  const old=a.textContent;
  a.dataset.busy='1';a.textContent='分类中…';a.style.pointerEvents='none';
  const note=$('#clsNote');
  note.textContent='准备中…';
  try{
    const r=await classifyLoop(0,null,force);
    if(r.error){
      note.innerHTML='标注中断：'+esc(r.error)+'（已标 '+r.off+'/'+(r.total||'?')+' 句）'
        +' <a id="clsResume" style="cursor:pointer;color:var(--accent)">从断点继续</a>';
      const b=$('#clsResume');
      if(b)b.onclick=async function(){
        note.textContent='继续中…';
        const r2=await classifyLoop(r.off,r.topics);
        await afterClassify(r2, note);
      };
      return;
    }
    await afterClassify(r, note);
  }catch(e){
    note.textContent='分类失败：'+(e.message||'未知错误');
  }finally{
    a.dataset.busy='';a.textContent=old;a.style.pointerEvents='';
  }
}
async function afterClassify(r,note){
  const a=$('#btnClassifyAI');
  if(r.error){
    note.innerHTML='标注中断：'+esc(r.error)+'（已标 '+r.off+'/'+(r.total||'?')+' 句）'
      +' <a id="clsResume" style="cursor:pointer;color:var(--accent)">从断点继续</a>';
    const b=$('#clsResume');
    if(b)b.onclick=async function(){
      note.textContent='继续中…';
      const r2=await classifyLoop(r.off,r.topics);
      await afterClassify(r2,note);
    };
    return;
  }
  await loadMeta();await refresh();
  note.innerHTML='AI 标注完成：本次写入 '+r.changed+' 条 · 覆盖 '+r.off+'/'+(r.total||'?')+' 句'
    +(r.topics&&r.topics.length?(' · '+r.topics.length+' 个主题'):'')
    +' · <a id="clsRestore" style="cursor:pointer;color:var(--muted)">还原规则分类</a>';
  const rb=$('#clsRestore');
  if(rb)rb.onclick=async function(){
    if(!confirm('还原成规则分类？AI 打的话题也会一并回退。'))return;
    const d=await post('/api/classify/restore',{session_id:S.session});
    await loadMeta();await refresh();
    note.textContent='已还原规则分类（'+d.restored+' 条）';
  };
}

export { afterClassify, classifyLoop, runClassifyAI };
