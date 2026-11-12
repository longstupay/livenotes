/* ==================================================================
   宿主适配器（host adapter）· 页面与「壳」之间唯一的协议层

   网页版：没有 ?embed=1，整个文件立刻返回，零副作用。
   桌面版：壳用 ?embed=1 打开页面，这里只做协议允许的四件事：
     1. 打上 embed 标记（html.ln-embed*），让页面让出宿主自己的顶栏；
     2. 把 ?theme= 落到 <html data-theme>，具体配色由 CSS 决定；
     3. 同源链接 / Ctrl+K / Ctrl+1..5 通过 postMessage 交回宿主；
     4. 拉 /api/host/manifest，按清单加载宿主皮肤（overlay 的 CSS/JS）。

   这里不出现任何桌面专属的样式、文案或交互——那些在 desktop/web/。
   契约见 docs/29-page-host-contract.md。
   ================================================================== */
(function () {
  var q;
  try { q = new URLSearchParams(location.search); } catch (e) { return; }
  if (q.get('embed') !== '1') return;

  var root = document.documentElement;
  root.classList.add('ln-embed');
  // 主题名是协议的一部分，取值由页面 CSS 决定；网页版（无参数）保持原样。
  var theme = q.get('theme') || '';
  if (theme) root.setAttribute('data-theme', theme);
  if (location.pathname === '/' || location.pathname === '/index.html') {
    root.classList.add('ln-embed-index');
  }
  if (q.has('settings')) root.classList.add('ln-embed-settings');

  // 嵌入模式的基础让位（藏顶栏、设置页铺满）是协议层，不属于某套皮肤。
  // 先内联挡住顶栏：embed.css 是异步加载的，桌面壳里不该闪一下网页版顶栏。
  var guard = document.createElement('style');
  guard.textContent = 'html.ln-embed [data-ln-region="topbar"]{visibility:hidden}';
  document.head.appendChild(guard);
  var skin = document.createElement('link');
  skin.rel = 'stylesheet';
  skin.href = '/static/css/embed.css';
  document.head.appendChild(skin);

  function post(msg) {
    try { parent.postMessage(msg, '*'); } catch (e) {}
  }

  var HOST = {
    embed: true,
    contract: 1,
    theme: theme,
    host: 'web',
    profile: null,
    capabilities: [],
    ready: null,
    has: function (c) { return HOST.capabilities.indexOf(c) >= 0; },
    whenReady: function () { return HOST.ready || Promise.resolve(null); }
  };

  // 宿主 API 的名字空间先建好：页面里的增强（右键菜单、全库搜索）由
  // overlay 的 JS 挂上来，基座只保证这个对象存在且带 host 能力查询。
  window.LN = window.LN || {};
  window.LN.host = HOST;
  window.LN.post = post;

  function loadCss(href) {
    var link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = href;
    document.head.appendChild(link);
  }

  // 顺序加载：overlay 的 JS 往往依赖前一个文件（例如先定义 LN.menu 再绑定）。
  function loadJs(src) {
    return new Promise(function (resolve) {
      var s = document.createElement('script');
      s.src = src;
      s.async = false;
      s.onload = s.onerror = function () { resolve(); };
      document.head.appendChild(s);
    });
  }

  function announce(profile) {
    post({
      type: 'ln-hello',
      contract: 1,
      host: HOST.host,
      page: location.pathname + location.search
    });
    try {
      document.dispatchEvent(new CustomEvent('ln:host-ready', { detail: profile || {} }));
    } catch (e) {}
  }

  var empty = { host: 'web', capabilities: [], copy: {} };

  HOST.ready = fetch('/api/host/manifest', { cache: 'no-store' })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (m) {
      if (!m || !m.overlay) { announce(empty); return null; }
      HOST.host = m.host || 'desktop';
      HOST.profile = m.profile || empty;
      HOST.capabilities = HOST.profile.capabilities || [];
      (m.css || []).forEach(loadCss);
      var chain = Promise.resolve();
      (m.js || []).forEach(function (src) {
        chain = chain.then(function () { return loadJs(src); });
      });
      return chain.then(function () { announce(HOST.profile); return HOST.profile; });
    })
    .catch(function () { announce(empty); return null; });

  /* ---------------- 协议：把「离开页面」的决定交回宿主 ---------------- */
  document.addEventListener('click', function (e) {
    var a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
    if (!a) return;
    var href = a.getAttribute('href') || '';
    if (!href || href.charAt(0) === '#') return;
    var u;
    try { u = new URL(a.href, location.href); } catch (err) { return; }
    if (u.origin !== location.origin) return;
    if (a.hasAttribute('download')) return;
    e.preventDefault();
    post({ type: 'ln-navigate', href: u.href });
  }, true);

  addEventListener('keydown', function (e) {
    var mod = e.ctrlKey || e.metaKey;
    if (!mod) return;
    var k = String(e.key || '').toLowerCase();
    if (k === 'k' || ['1', '2', '3', '4', '5'].indexOf(k) >= 0) {
      e.preventDefault();
      post({ type: 'ln-key', key: k });
    }
  });
})();
