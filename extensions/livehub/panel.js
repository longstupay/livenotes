let lastData = { caps: [], dead: [], send: false, now: Date.now() };
let rooms = [];
let openTabs = new Map();   // url -> tabId

function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function ts(s){s=Math.max(0,Math.round(s||0));return Math.floor(s/60)+":"+String(s%60).padStart(2,"0");}
function ago(ms){const s=Math.round(ms/1000);return s<60?s+" 秒前":Math.floor(s/60)+" 分前";}
function roomName(t){
  t=String(t||"");
  t=t.replace(/^监听中心\s*·\s*/,"").replace(/^监控中心\s*·\s*/,"");
  t=t.replace(/\s*-\s*抖音直播$/,"").replace(/\s*-\s*抖音$/,"");
  return t||"未命名";
}

async function loadRooms(){
  const r = await chrome.storage.local.get("rooms");
  rooms = r.rooms || [];
  const tabs = await chrome.tabs.query({});
  openTabs = new Map();
  for (const t of tabs) if (t.url) openTabs.set(t.url, t.id);
  renderRooms();
}

function renderRooms(){
  const box = document.getElementById("rooms");
  if (!rooms.length){
    box.innerHTML = '<div class="rh"><b>固定直播间</b><span class="sp"></span></div>'
      + '<div class="st">还没有。点一次扩展图标把直播间加进来，之后就能一键全部打开。</div>';
    return;
  }
  const liveUrls = new Set((lastData.caps || []).map(c => c.url));
  let h = '<div class="rh"><b>固定直播间</b><span class="sp"></span>'
        + '<button id="openAll">全部打开（'+rooms.length+'）</button></div>';
  rooms.forEach((r, i) => {
    const isLive = liveUrls.has(r.url);
    const isOpen = openTabs.has(r.url);
    const state = isLive ? '<span class="st on">● 监听中</span>'
      : isOpen ? '<span class="st wait">已打开，待授权（点一次图标）</span>'
      : '<span class="st">未打开</span>';
    h += '<div class="ritem">'
      +  '<span class="rn" title="'+esc(r.title)+'">'+esc(roomName(r.title))+'</span>'
      +  state
      +  '<button data-open="'+i+'">打开</button>'
      +  '<button data-del="'+i+'" class="danger">删除</button>'
      +  '</div>';
  });
  box.innerHTML = h;

  const all = document.getElementById("openAll");
  if (all) all.onclick = openAll;
  box.querySelectorAll("[data-open]").forEach(b=>{
    b.onclick = async () => {
      const r = rooms[+b.dataset.open];
      const id = openTabs.get(r.url);
      if (id != null) {
        await chrome.tabs.update(id, {active:true});
        await chrome.windows.update((await chrome.tabs.get(id)).windowId, {focused:true});
      } else {
        await chrome.tabs.create({url:r.url, active:true});
      }
      setTimeout(loadRooms, 600);
    };
  });
  box.querySelectorAll("[data-del]").forEach(b=>{
    b.onclick = async () => {
      rooms.splice(+b.dataset.del, 1);
      await chrome.storage.local.set({rooms});
      renderRooms();
    };
  });
}

async function openAll(){
  let opened = 0;
  for (const r of rooms){
    if (openTabs.has(r.url)) continue;
    try { await chrome.tabs.create({url:r.url, active:false}); opened++; } catch(e){}
  }
  setTimeout(async () => { await loadRooms(); render(); }, 900);
  const hint = document.getElementById("sum");
  if (hint && opened) hint.textContent = "已打开 "+opened+" 个，逐个点扩展图标授权采集";
}

async function poll(){
  try{
    const r = await chrome.runtime.sendMessage({type:"stats"});
    if (r && r.caps) lastData = r;
  }catch(e){}
  render();
}

function render(){
  const caps = lastData.caps || [];
  const dead = lastData.dead || [];
  const now = Date.now();
  const el = document.getElementById("list");

  if (!caps.length && !dead.length){
    el.innerHTML = '<div class="empty">还没有在监听的标签页<br>打开一个直播间标签页，点一下工具栏上的扩展图标</div>';
  } else {
    let h = '<table><thead><tr><th>标签页</th><th>状态</th><th>电平</th><th>块数</th><th>时长</th><th>最近一块</th><th></th></tr></thead><tbody>';
    for (const c of caps){
      const idleMs = now - (c.lastChunkAt || now);
      const stale = idleMs > 3000;
      const lv = Math.min(100, Math.round((c.rms||0) * 400));
      const tag = c.sid
        ? '<span class="tag">已入库</span>'
        : '<span class="tag warn">未入库</span>';
      h += '<tr><td class="title" title="'+esc(c.title)+'">'+esc(roomName(c.title))+'</td>'
        +  '<td>'+esc(c.state)+' · '+c.sampleRate+'Hz'+tag
        +  (stale?' <span class="bad">断流中</span>':'')
        +  (c.reconnects? ' <span class="tag warn">重连 '+c.reconnects+'</span>':'')
        +  (c.dropped? ' <span class="tag warn">丢 '+c.dropped+' 块</span>':'')
        +  (c.sendErr?' <span class="bad">'+esc(c.sendErr)+'</span>':'')+'</td>'
        +  '<td><div class="lv"><i style="width:'+lv+'%"></i></div></td>'
        +  '<td class="mono">'+c.chunks+'</td>'
        +  '<td class="mono">'+ts(c.seconds)+'</td>'
        +  '<td class="mono">'+(idleMs/1000).toFixed(1)+'s 前</td>'
        +  '<td><button data-stop="'+c.tabId+'" class="danger">停止</button></td></tr>';
    }
    for (const d of dead){
      const tail = d.closed
        ? '<span class="tag">标签页已关闭</span>'
        : '<button data-goto="'+d.tabId+'">去重连</button><button data-unmute="'+d.tabId+'">取消静音</button>';
      h += '<tr class="row-dead"><td class="title" title="'+esc(d.title)+'">'+esc(roomName(d.title))+'</td>'
        +  '<td><span class="bad">已停：'+esc(d.reason)+'</span><span class="tag">已静音</span></td>'
        +  '<td><div class="lv"><i style="width:0"></i></div></td>'
        +  '<td class="mono">'+d.chunks+'</td>'
        +  '<td class="mono">'+ts(d.seconds)+'</td>'
        +  '<td class="mono">'+ago(now-(d.endedAt||now))+'</td>'
        +  '<td>'+tail+'</td></tr>';
    }
    h += '</tbody></table>';
    el.innerHTML = h;
    el.querySelectorAll("[data-stop]").forEach(b=>{
      b.onclick = () => {
        const closeNow = !!lastData.closeOnStop;
        const ask = closeNow ? "停止这一路，并关闭它的标签页？" : "停止这一路？录音会正常收尾保存。";
        if (!confirm(ask)) return;
        chrome.runtime.sendMessage({type:"stop", tabId:+b.dataset.stop}).then(poll);
      };
    });
    el.querySelectorAll("[data-unmute]").forEach(b=>{
      b.onclick = async () => {
        await chrome.runtime.sendMessage({type:"unmute", tabId:+b.dataset.unmute});
        b.textContent = "已取消静音";
        b.disabled = true;
      };
    });
    el.querySelectorAll("[data-goto]").forEach(b=>{
      b.onclick = async () => {
        const tabId = +b.dataset.goto;
        try {
          await chrome.tabs.update(tabId, {active:true});
          await chrome.windows.update((await chrome.tabs.get(tabId)).windowId, {focused:true});
        } catch(e) { alert("这个标签页已经关掉了"); }
      };
    });
  }

  const total = caps.length, deadN = dead.length;
  document.getElementById("sum").textContent = (total || deadN)
    ? (total+" 路在听"+(deadN?(" · "+deadN+" 路已断"):"")+" · 共 "+caps.reduce((a,c)=>a+c.chunks,0)+" 块")
    : "空闲";
  document.getElementById("clear").style.display = deadN ? "" : "none";

  // 连接状态要一直显示：用户最怕的是"以为在录，其实没送出去"
  const conn = document.getElementById("conn");
  const online = !!lastData.online;
  const sending = !!lastData.send;
  conn.className = "conn " + (online && sending ? "on" : "off");
  conn.textContent = !sending ? "○ 未推送（只监听不记录）"
    : online ? ("● 已连接 " + (lastData.base||"").replace(/^https?:\/\//,""))
    : "○ 未连接本地服务";
  const hint = document.getElementById("sendHint");
  if (hint) hint.textContent = sending
    ? (online ? "音频正在写入本地服务" : "服务没起？先 python run.py --lan")
    : "关掉它 = 只监听不记录，网页端看不到任何东西";
  renderRooms();
}

document.getElementById("retry").onclick = async () => {
  await chrome.runtime.sendMessage({type:"retry"});
  setTimeout(poll, 600);
};
document.getElementById("all").onclick = async () => {
  const closeNow = !!lastData.closeOnStop;
  if (!confirm(closeNow ? "停止全部，并关闭它们的标签页？" : "停止全部监听？录音会正常收尾保存。")) return;
  await chrome.runtime.sendMessage({type:"stopAll"});
  poll();
};
document.getElementById("clear").onclick = async () => {
  await chrome.runtime.sendMessage({type:"clearDead"});
  poll();
};
document.getElementById("send").onchange = async (e) => {
  await chrome.runtime.sendMessage({type:"config", send: e.target.checked});
  poll();
};
document.getElementById("closeOnStop").onchange = async (e) => {
  await chrome.runtime.sendMessage({type:"config", closeOnStop: e.target.checked});
  poll();
};

(async function init(){
  const r = await chrome.runtime.sendMessage({type:"stats"}).catch(()=>null);
  if (r) {
    lastData = r;
    document.getElementById("send").checked = !!r.send;
    document.getElementById("closeOnStop").checked = !!r.closeOnStop;
  }
  await loadRooms();
  render();
  setInterval(poll, 1000);
  setInterval(loadRooms, 5000);
})();
