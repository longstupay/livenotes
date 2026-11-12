/* ==================================================================
   桌面壳的应用内交互（overlay 的 JS 部分）

   WebView2 关掉了浏览器原生右键菜单，桌面版必须有应用内菜单；选中文字后
   的「全库搜索」也是桌面专属。这些是「宿主提供的界面」，不是页面能力，
   所以通过 window.LN 挂给基座页面用，基座只做能力探测（if (LN.menu)）。

   加载时机：基座适配器读完 host.json 后按顺序加载本文件。
   ================================================================== */
(function () {
  var host = window.LN && window.LN.host;
  if (!host) return;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  var ctxEl = null;
  var searchEl = null;

  function toast(msg) {
    var t = document.createElement('div');
    t.className = 'ln-toast';
    t.textContent = String(msg || '');
    document.body.appendChild(t);
    setTimeout(function () { t.remove(); }, 2400);
  }

  function closeMenu() {
    if (!ctxEl) return;
    ctxEl.remove();
    ctxEl = null;
    document.removeEventListener('mousedown', onDocDown, true);
  }

  function onDocDown(e) {
    if (ctxEl && !ctxEl.contains(e.target)) closeMenu();
  }

  function menu(items, x, y) {
    closeMenu();
    var m = document.createElement('div');
    m.className = 'ln-ctx';
    m.setAttribute('role', 'menu');
    m.style.visibility = 'hidden';
    m.innerHTML = items.map(function (it) {
      if (it.sep) return '<div class="ln-ctx-sep"></div>';
      return '<button class="ln-ctx-item' + (it.danger ? ' danger' : '') + '"'
        + (it.disabled ? ' disabled' : '') + '>' + esc(it.label)
        + (it.hint ? '<span class="ln-ctx-hint">' + esc(it.hint) + '</span>' : '')
        + '</button>';
    }).join('');
    document.body.appendChild(m);
    var w = m.offsetWidth, h = m.offsetHeight;
    m.style.left = Math.max(8, Math.min(x, innerWidth - w - 8)) + 'px';
    m.style.top = Math.max(8, Math.min(y, innerHeight - h - 8)) + 'px';
    m.style.visibility = '';
    ctxEl = m;
    var buttons = m.querySelectorAll('.ln-ctx-item');
    var flat = items.filter(function (it) { return !it.sep; });
    Array.prototype.forEach.call(buttons, function (b, i) {
      var it = flat[i];
      b.onclick = function (e) {
        e.stopPropagation();
        if (it.disabled) return;
        closeMenu();
        try { if (it.run) it.run(); } catch (err) { toast(err && err.message || err); }
      };
    });
    setTimeout(function () {
      document.addEventListener('mousedown', onDocDown, true);
    }, 0);
  }

  function copyText(text) {
    text = String(text == null ? '' : text);
    if (!text) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { toast('已复制'); }, fallback);
    } else fallback();
    function fallback() {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;left:-9999px;top:0';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); toast('已复制'); }
      catch (e) { toast('复制失败，请按 Ctrl+C'); }
      ta.remove();
    }
  }

  function readClipboard(done) {
    if (navigator.clipboard && navigator.clipboard.readText) {
      navigator.clipboard.readText().then(done, function () {
        toast('浏览器没给剪贴板权限，请按 Ctrl+V');
      });
    } else {
      toast('请按 Ctrl+V 粘贴');
    }
  }

  /* 选中文字 → 全库搜索；点结果跳到对应场次的时间码。 */
  function search(text) {
    closeSearch();
    var box = document.createElement('div');
    box.className = 'ln-search';
    box.innerHTML = '<div class="ln-s-head"><b>全库搜索</b><span>' + esc(text)
      + '</span><button data-x title="关闭">×</button></div>'
      + '<div class="ln-s-body"><div class="ln-s-empty">搜索中…</div></div>';
    document.body.appendChild(box);
    searchEl = box;
    box.querySelector('[data-x]').onclick = closeSearch;
    fetch('/api/search?q=' + encodeURIComponent(text) + '&limit=30')
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var rows = d.rows || [];
        var body = box.querySelector('.ln-s-body');
        if (!rows.length) {
          body.innerHTML = '<div class="ln-s-empty">没有匹配的发言</div>';
          return;
        }
        body.innerHTML = rows.map(function (r) {
          var t = Math.max(0, Math.floor(r.t_start || 0));
          var mm = Math.floor(t / 60), ss = t % 60;
          return '<button class="ln-s-row" data-s="' + esc(r.session_id) + '" data-t="' + t + '">'
            + '<span class="ln-s-meta">' + esc((r.started_at || '').slice(0, 10)) + ' · '
            + mm + ':' + (ss < 10 ? '0' : '') + ss + ' · ' + esc(r.speaker_label || '') + '</span>'
            + '<span class="ln-s-title">' + esc(r.title || '') + '</span>'
            + '<span class="ln-s-text">' + esc(r.text || '') + '</span></button>';
        }).join('');
        Array.prototype.forEach.call(body.querySelectorAll('.ln-s-row'), function (el) {
          el.onclick = function () {
            var sid = el.dataset.s, t = Number(el.dataset.t);
            closeSearch();
            window.LN.post({ type: 'ln-navigate',
                             href: '/?session=' + encodeURIComponent(sid) + '&t=' + t });
          };
        });
      })
      .catch(function () {
        box.querySelector('.ln-s-body').innerHTML =
          '<div class="ln-s-empty">搜索失败，服务可能刚重启</div>';
      });
  }

  function closeSearch() {
    if (!searchEl) return;
    searchEl.remove();
    searchEl = null;
  }

  document.addEventListener('contextmenu', function (e) {
    if (e.defaultPrevented) return;
    var t = e.target;
    var editable = !!(t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA'
      || t.isContentEditable));
    var sel = '';
    try { sel = String(getSelection() || '').trim(); } catch (err) {}
    var items = [];
    if (editable) {
      e.preventDefault();
      items = [
        { label: '剪切', hint: 'Ctrl+X', run: function () { document.execCommand('cut'); } },
        { label: '复制', hint: 'Ctrl+C', run: function () { document.execCommand('copy'); } },
        { label: '粘贴', hint: 'Ctrl+V', run: function () {
          var el = t;
          readClipboard(function (text) {
            try {
              var start = el.selectionStart, end = el.selectionEnd;
              el.value = el.value.slice(0, start) + text + el.value.slice(end);
              el.selectionStart = el.selectionEnd = start + text.length;
              el.dispatchEvent(new Event('input', { bubbles: true }));
            } catch (err) { toast('粘贴失败，请按 Ctrl+V'); }
          });
        } },
        { sep: true },
        { label: '全选', hint: 'Ctrl+A', run: function () { t.select && t.select(); } }
      ];
    } else if (sel) {
      e.preventDefault();
      items = [
        { label: '复制', hint: 'Ctrl+C', run: function () { copyText(sel); } },
        { label: '在全库搜索', run: function () { search(sel); } }
      ];
    } else {
      return;   // 空白处不弹，保持桌面应用克制
    }
    menu(items, e.clientX, e.clientY);
  });

  addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { closeMenu(); closeSearch(); }
  });

  /* ---------------- 文案：按宿主覆写 ---------------- */
  // 基座里语义不对的文案（例如「回到工作台点实时监听」）用 data-ln-copy 标记，
  // 具体文字由 host.json 的 profile.copy 提供；基座永远写网页版的说法。
  function applyCopy() {
    var copy = (host.profile && host.profile.copy) || {};
    var els = document.querySelectorAll('[data-ln-copy]');
    Array.prototype.forEach.call(els, function (el) {
      var text = copy[el.getAttribute('data-ln-copy')];
      if (typeof text === 'string' && text) el.textContent = text;
    });
  }

  window.LN.menu = menu;
  window.LN.closeMenu = closeMenu;
  window.LN.toast = toast;
  window.LN.copyText = copyText;
  window.LN.search = search;
  window.LN.esc = esc;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', applyCopy);
  } else {
    applyCopy();
  }
})();
