/* NS2 前端公共件（总览 + 审片共用）：状态大白话、接口、密钥弹层、提示条、确认框、大图 / 视频看图层、主题。
   设计参照旧版 v3 分镜编辑器（apps/web、apps/prototype-v3）：深色工作台、以图为主、每镜一行首帧 / 尾帧 / 视频三格、底部胶片条。 */
(function () {
  'use strict';
  var NS = window.NS2 = {};

  NS.esc = function (s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  };
  NS.$ = function (sel, root) { return (root || document).querySelector(sel); };
  NS.params = new URLSearchParams(window.location.search);
  NS.run = function () { return NS.params.get('run') || 'series10_v2'; };

  // ---------------------------------------------------------------- 状态 → 大白话 + 色调
  // tone：ok 绿 / warn 琥珀 / bad 红 / busy 蓝 / idle 灰 / you 主色（要你处理）
  var FRAME = {
    ready: ['已过', 'ok'], passed: ['已过', 'ok'], best_effort: ['尽力版', 'warn'], fallback: ['不用尾帧', 'idle'],
    na: ['不需要', 'idle'], running: ['在出', 'busy'], pending: ['排队', 'idle'], blocked: ['卡住', 'bad'], failed: ['失败', 'bad']
  };
  var VIDEO = {
    final: ['已定稿', 'ok'], pending_review: ['待你看', 'you'], best_effort: ['尽力版', 'warn'], rendering: ['在出', 'busy'],
    queued: ['排队', 'idle'], need_frames: ['等首尾帧', 'idle'], blocked: ['卡住', 'bad'], failed: ['失败', 'bad'], todo: ['未出', 'idle']
  };
  NS.frameLabel = function (st) { return FRAME[st] || [st || '–', 'idle']; };
  NS.videoLabel = function (v) {
    v = v || {};
    var l = VIDEO[v.state] || [v.state || '–', 'idle'];
    if (v.state === 'rendering' && v.progress !== null && v.progress !== undefined) return ['在出 ' + Math.round(v.progress * 100) + '%', 'busy'];
    if (v.state === 'queued' && v.queue_pos) return ['排队第 ' + v.queue_pos, 'idle'];
    return l;
  };
  NS.PHASE = { frames: ['出图中', 'busy'], videos: ['出片中', 'busy'], assembly: ['待合成', 'you'], done: ['已完成', 'ok'] };

  // 一镜的总状态（一颗胶囊说清楚“现在是什么情况、要不要你管”）
  NS.usable = function (shot) {
    var vs = (shot.video && shot.video.state) || '';
    return (vs === 'pending_review' || vs === 'best_effort') && shot.review !== 'passed' && shot.review !== 'rejected' &&
      !(shot.adult && !shot.triggered);
  };
  NS.shotStatus = function (shot) {
    var v = shot.video || {}, f = shot.first || {}, l = shot.last || {};
    if (shot.adult && !shot.triggered) return { key: 'adult', text: '成人镜 · 等你触发', tone: 'you', you: true };
    if (shot.review === 'rejected') return { key: 'rejected', text: '已驳回 · 等重出', tone: 'bad' };
    if (v.state === 'final') return { key: 'final', text: '已定稿', tone: 'ok' };
    if (NS.usable(shot)) return { key: 'review', text: v.state === 'best_effort' ? '尽力版 · 待你看' : '待你看', tone: 'you', you: true };
    if (v.state === 'rendering') return { key: 'busy', text: NS.videoLabel(v)[0].replace('在出', '出片中'), tone: 'busy' };
    if (v.state === 'queued') return { key: 'busy', text: '视频' + NS.videoLabel(v)[0], tone: 'busy' };
    if (f.state === 'running' || l.state === 'running') return { key: 'busy', text: '出图中', tone: 'busy' };
    if (v.state === 'failed' || f.state === 'failed' || l.state === 'failed') return { key: 'bad', text: '失败', tone: 'bad' };
    if (v.state === 'blocked' || f.state === 'blocked' || l.state === 'blocked') return { key: 'bad', text: '卡住', tone: 'bad' };
    if (v.state === 'need_frames') return { key: 'wait', text: '等首尾帧', tone: 'idle' };
    return { key: 'busy', text: '排队中', tone: 'idle' };
  };
  // 卡住 / 失败的原因：取最该看的那一条（视频 > 尾帧 > 首帧），去掉内部前缀
  NS.reason = function (shot) {
    var v = shot.video || {}, f = shot.first || {}, l = shot.last || {};
    var r = shot.note || (['blocked', 'failed'].indexOf(v.state) >= 0 && v.error) ||
      (['blocked', 'failed', 'best_effort'].indexOf(l.state) >= 0 && l.error) ||
      (['blocked', 'failed', 'best_effort'].indexOf(f.state) >= 0 && f.error) || '';
    return String(r || '');
  };
  NS.time = function (iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d)) return String(iso);
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    return p(d.getHours()) + ':' + p(d.getMinutes());
  };
  NS.pill = function (text, tone, extra) {
    return '<span class="pill ' + (tone || 'idle') + (extra ? ' ' + extra : '') + '">' + NS.esc(text) + '</span>';
  };

  // ---------------------------------------------------------------- 接口
  NS.get = function (url) {
    return fetch(url).then(function (r) {
      if (!r.ok) return r.text().then(function (t) { throw new Error(r.status + ' ' + (t || r.statusText)); });
      return r.json();
    });
  };
  // 写操作：带密钥；没有 / 失效 → 弹出密钥层（粘一次，记 90 天），再重试一次
  NS.write = function (label, url, opts, retried) {
    var key = window.NS2Key.get();
    if (!key) return NS.askKey('“' + label + '”要密钥').then(function (k) { return k ? NS.write(label, url, opts, true) : null; });
    opts = Object.assign({}, opts || {});
    opts.headers = Object.assign({}, opts.headers || {}, { 'X-NS2-Key': key });
    if (opts.json !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.json);
      delete opts.json;
    }
    return fetch(url, opts).then(function (r) {
      if (r.status === 401 && !retried) {
        window.NS2Key.clear(); NS.renderKeyChip();
        return NS.askKey('密钥失效或换过了，请重新粘一次').then(function (k) { return k ? NS.write(label, url, opts, true) : null; });
      }
      if (!r.ok) return r.text().then(function (t) {
        var msg = t;
        try { msg = JSON.parse(t).detail || t; } catch (e) { /* 原文 */ }
        throw new Error(label + '没成功（' + r.status + '）：' + (typeof msg === 'string' ? msg : JSON.stringify(msg)));
      });
      return r.text().then(function (t) { try { return JSON.parse(t); } catch (e) { return t; } });
    });
  };

  // ---------------------------------------------------------------- 弹层：密钥 / 确认 / 输入
  function layer(html, cls) {
    var wrap = document.createElement('div');
    wrap.className = 'layer ' + (cls || '');
    wrap.innerHTML = '<div class="sheet" role="dialog" aria-modal="true">' + html + '</div>';
    document.body.appendChild(wrap);
    return wrap;
  }
  function closeOn(wrap, done) {
    function close(v) { document.removeEventListener('keydown', onKey); wrap.remove(); done(v); }
    function onKey(e) { if (e.key === 'Escape') close(null); }
    document.addEventListener('keydown', onKey);
    wrap.addEventListener('click', function (e) { if (e.target === wrap) close(null); });
    return close;
  }
  NS.askKey = function (why) {
    return new Promise(function (resolve) {
      var w = layer('<h3>🔑 粘一次密钥</h3><p class="sub">' + NS.esc(why || '写操作要密钥') +
        '。那一长串（64 位字母数字）粘进来点“保存”，这台电脑记住 90 天；看图不需要密钥。</p>' +
        '<input class="key-in" type="password" autocomplete="off" placeholder="粘贴密钥">' +
        '<p class="msg"></p><div class="row end"><button class="ghost" data-x>取消</button><button class="primary" data-ok>保存</button></div>');
      var close = closeOn(w, resolve);
      var inp = NS.$('.key-in', w), msg = NS.$('.msg', w);
      inp.focus();
      function save() {
        var k = (inp.value || '').trim();
        if (!k) { msg.textContent = '先把密钥粘进框里'; return; }
        msg.textContent = '正在核对……';
        fetch('/api/auth/check', { method: 'POST', headers: { 'X-NS2-Key': k } }).then(function (r) {
          if (r.status === 401) { msg.textContent = '密钥不对：要整串复制，前后不要多空格'; return; }
          if (!r.ok) { msg.textContent = '核对失败（' + r.status + '），稍后再试'; return; }
          window.NS2Key.set(k); NS.renderKeyChip(); NS.toast('密钥正确，已记住 90 天', 'ok'); close(k);
        }).catch(function (e) { msg.textContent = '连不上服务器：' + e.message; });
      }
      NS.$('[data-ok]', w).onclick = save;
      NS.$('[data-x]', w).onclick = function () { close(null); };
      inp.addEventListener('keydown', function (e) { if (e.key === 'Enter') save(); });
    });
  };
  // 确认框：title + 说清楚会发生什么；danger = 红色确认键
  NS.confirm = function (title, body, okText, danger) {
    return new Promise(function (resolve) {
      var w = layer('<h3>' + NS.esc(title) + '</h3><div class="sub">' + (body || '') + '</div>' +
        '<div class="row end"><button class="ghost" data-x>取消</button><button class="' + (danger ? 'danger' : 'primary') + '" data-ok>' +
        NS.esc(okText || '确定') + '</button></div>');
      var close = closeOn(w, function (v) { resolve(!!v); });
      NS.$('[data-ok]', w).onclick = function () { close(true); };
      NS.$('[data-x]', w).onclick = function () { close(null); };
      NS.$('[data-ok]', w).focus();
    });
  };
  NS.ask = function (title, body, placeholder, okText, required) {
    return new Promise(function (resolve) {
      var w = layer('<h3>' + NS.esc(title) + '</h3><div class="sub">' + (body || '') + '</div>' +
        '<textarea rows="3" placeholder="' + NS.esc(placeholder || '') + '"></textarea><p class="msg"></p>' +
        '<div class="row end"><button class="ghost" data-x>取消</button><button class="primary" data-ok>' + NS.esc(okText || '确定') + '</button></div>');
      var close = closeOn(w, resolve);
      var ta = NS.$('textarea', w);
      ta.focus();
      NS.$('[data-ok]', w).onclick = function () {
        var v = (ta.value || '').trim();
        if (required && !v) { NS.$('.msg', w).textContent = '这一项要写'; return; }
        close(v);
      };
      NS.$('[data-x]', w).onclick = function () { close(null); };
    });
  };

  // ---------------------------------------------------------------- 提示条
  NS.toast = function (text, tone, ms) {
    var box = NS.$('#toasts');
    if (!box) { box = document.createElement('div'); box.id = 'toasts'; document.body.appendChild(box); }
    var t = document.createElement('div');
    t.className = 'toast ' + (tone || '');
    t.textContent = text;
    box.appendChild(t);
    setTimeout(function () { t.classList.add('out'); setTimeout(function () { t.remove(); }, 300); }, ms || (tone === 'bad' ? 9000 : 3500));
  };

  // ---------------------------------------------------------------- 看图层（大图 / 视频，←→ 翻，Esc 关）
  NS.lightbox = function (items, start) {
    if (!items.length) return;
    var i = Math.max(0, Math.min(start || 0, items.length - 1));
    var w = document.createElement('div');
    w.className = 'lightbox';
    document.body.appendChild(w);
    function show() {
      var it = items[i];
      w.innerHTML = '<div class="lb-top"><span>' + NS.esc(it.title || '') + '</span><span class="muted">' + (i + 1) + ' / ' + items.length +
        ' · ←→ 翻 · Esc 关</span><button class="ghost" data-x>关闭</button></div>' +
        '<div class="lb-body">' + (it.video ? '<video src="' + NS.esc(it.url) + '" controls autoplay loop playsinline></video>'
          : '<img src="' + NS.esc(it.url) + '" alt="">') + '</div>' +
        (it.caption ? '<div class="lb-cap">' + it.caption + '</div>' : '') +
        (items.length > 1 ? '<button class="lb-nav prev" data-p aria-label="上一张">‹</button><button class="lb-nav next" data-n aria-label="下一张">›</button>' : '');
    }
    function close() { document.removeEventListener('keydown', onKey); w.remove(); }
    function onKey(e) {
      if (e.key === 'Escape') close();
      else if (e.key === 'ArrowLeft') { i = (i - 1 + items.length) % items.length; show(); }
      else if (e.key === 'ArrowRight') { i = (i + 1) % items.length; show(); }
    }
    w.addEventListener('click', function (e) {
      if (e.target.closest('[data-x]') || e.target === w) close();
      else if (e.target.closest('[data-p]')) { i = (i - 1 + items.length) % items.length; show(); }
      else if (e.target.closest('[data-n]')) { i = (i + 1) % items.length; show(); }
    });
    document.addEventListener('keydown', onKey);
    show();
  };

  // ---------------------------------------------------------------- 顶栏：密钥小牌 + 主题
  NS.renderKeyChip = function () {
    var el = NS.$('#key-chip');
    if (!el) return;
    var has = !!window.NS2Key.get();
    el.className = 'chip ' + (has ? 'ok' : 'you');
    el.innerHTML = has ? '🔑 已登录' : '🔑 粘密钥';
    el.title = has ? '这台电脑已记住密钥（90 天）。点一下可以清除' : '定稿 / 修改 / 触发要密钥，点这里粘一次';
  };
  NS.bindKeyChip = function () {
    var el = NS.$('#key-chip');
    if (!el) return;
    el.addEventListener('click', function () {
      if (window.NS2Key.get()) {
        NS.confirm('清除本机密钥？', '清除后看图照常，下次定稿 / 修改时会再问一次密钥。', '清除').then(function (ok) {
          if (ok) { window.NS2Key.clear(); NS.renderKeyChip(); NS.toast('本机密钥已清除'); }
        });
      } else NS.askKey('定稿、修改、触发都要密钥');
    });
    NS.renderKeyChip();
  };
  NS.initTheme = function () {
    var saved = null;
    try { saved = localStorage.getItem('ns2_theme'); } catch (e) { /* 无痕模式 */ }
    if (saved) document.documentElement.setAttribute('data-theme', saved);
    var b = NS.$('#theme-btn');
    if (b) b.addEventListener('click', function () {
      var cur = document.documentElement.getAttribute('data-theme') ||
        (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
      var nx = cur === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', nx);
      try { localStorage.setItem('ns2_theme', nx); } catch (e) { /* 忽略 */ }
    });
  };

  // ---------------------------------------------------------------- 当前版（takes.json active）
  // 总览的帧 url 来自“最近一次出图任务”的产出；尽力版自动用时当前版可能是更早的一版（ep05-S1：最近 #3 是坏图，当前是 #2）。
  // 尽力版的镜按 takes.json 的 active 改成真正的当前版（文件口取，按 镜+状态+版本 缓存 60 秒）。
  var takesCache = {};
  NS.takesUrl = function (shot) {
    var m = /^ep(\d+)-S(\d+)$/.exec(shot.id || '');
    return m ? '/files/' + encodeURIComponent(NS.hostOf(shot)) + '/renders/' + encodeURIComponent(NS.run()) + '/ep' + m[1] + '/S' + m[2] + '/takes.json' : null;
  };
  NS.takes = function (shot) {
    var u = NS.takesUrl(shot);
    if (!u) return Promise.resolve(null);
    var k = u + '|' + [(shot.first || {}).state, (shot.first || {}).n, (shot.last || {}).state, (shot.last || {}).n].join(',');
    var hit = takesCache[k];
    if (!hit || Date.now() - hit.at > 60000) {
      hit = takesCache[k] = { at: Date.now(), p: NS.get(u).catch(function () { return null; }) };
    }
    return hit.p;
  };
  // 用 takes 的当前版覆盖 shot.first / shot.last 的 url + n（返回是否改了）
  NS.applyActive = function (shot, td) {
    if (!td || !td.takes) return false;
    var host = NS.hostOf(shot), act = td.active || {}, be = td.best_effort || {}, changed = false;
    [['first', 'image'], ['last', 'last']].forEach(function (x) {
      var st = shot[x[0]], n = act[x[1]];
      if (!st || !n || st.state === 'fallback') return;
      var t = (td.takes || []).filter(function (y) { return y.stage === x[1] && y.n === n && y.file; })[0];
      if (!t) return;
      var url = '/files/' + host + '/' + t.file;
      if (st.url !== url) { st.latest = { url: st.url, n: st.n }; st.url = url; st.n = n; changed = true; }
      if (be[x[1]] && be[x[1]].n === n) st.best_mark = true;
      var pf = t.postflight || t.contract || {};
      if (pf.reasons && pf.reasons.length && st.state === 'best_effort') st.error = '契约没过：' + (pf.blocking || []).join('/') + '；' + pf.reasons.join('；');
    });
    return changed;
  };

  // ---------------------------------------------------------------- 剧本（导入稿，按集取一次缓存）
  var scriptCache = {};
  NS.script = function (host) {
    var run = NS.run(), k = run + '@' + host;
    if (!scriptCache[k]) {
      scriptCache[k] = NS.get('/files/' + encodeURIComponent(host) + '/import/runs/' + encodeURIComponent(run) + '/script_v2.json')
        .then(function (d) {
          var out = {};
          (d.episodes || []).forEach(function (e) {
            (e.shots || []).forEach(function (s) {
              out['ep' + ('0' + e.number).slice(-2) + '-S' + s.index] = s;
            });
            out['ep' + ('0' + e.number).slice(-2)] = { title: e.title, synopsis: e.synopsis, cast: e.cast };
          });
          return out;
        }).catch(function () { return {}; });
    }
    return scriptCache[k];
  };
  NS.hostOf = function (shot) {
    var u = (shot && ((shot.first && shot.first.url) || (shot.last && shot.last.url) || (shot.video && shot.video.url))) || '';
    var m = /^\/files\/([^/]+)\//.exec(u);
    return m ? m[1] : 'gpu33';
  };
  // 剧本镜头 → 一句话（景别 / 机位那句截短）
  NS.scriptLine = function (s) {
    if (!s) return '';
    var t = String(s.img_prompt || s.motion_prompt || '');
    t = t.split('；')[0];
    return t.length > 90 ? t.slice(0, 90) + '…' : t;
  };
  NS.dialogue = function (s) {
    if (!s) return '';
    var d = s.dialogue;
    if (Array.isArray(d)) return d.map(function (x) { return typeof x === 'string' ? x : ((x.who || x.speaker || '') + (x.who || x.speaker ? '：' : '') + (x.line || x.text || '')); }).join('  ');
    return d ? String(d) : (s.dialogue_raw || '');
  };
})();
