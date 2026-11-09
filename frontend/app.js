/* NS2 总览页（以图为主，参照旧版 v3 分镜编辑器）：GET /api/overview 15 秒轮询；剧本文本从导入稿 JSON 取一次。
   顶栏五阶段 → “要你处理”横幅 + 一键全部用这版 → 按集 / 按状态筛 → 每集：标题 + 进度 + 成片卡 + 胶片条 + 每镜一行（首帧 / 尾帧 / 视频三格 + 剧本 + 原因 + 动作）。 */
(function () {
  'use strict';
  var NS = window.NS2, esc = NS.esc, $ = NS.$;
  var POLL_MS = 15000;

  var runInput = $('#run-input');
  var epFilterEl = $('#ep-filter');
  var statusFilterEl = $('#status-filter');
  var episodesEl = $('#episodes');
  var cached = null;          // 最近一次 /api/overview
  var script = {};            // 镜号 → 导入稿镜头（剧本）
  var lastOk = 0;
  var openWhy = {};           // 展开过的原因（重渲后保持）

  function currentRun() { return NS.params.get('run') || (runInput && runInput.value.trim()) || 'series10_v2'; }

  // ---------------------------------------------------------------- 一键用这版（有片：待你看 + 尽力版、没定稿、没驳回、非未触发成人镜）
  function usable(shot) {
    var vs = (shot.video && shot.video.state) || '';
    return (vs === 'pending_review' || vs === 'best_effort') && shot.review !== 'passed' && shot.review !== 'rejected' &&
      !(shot.adult && !shot.triggered);   // 审查 3 N7：已驳回 / 未触发的成人镜不进一键
  }
  function epUsable(ep) { return (ep.shots || []).filter(usable).map(function (s) { return s.id; }); }
  function visibleUsable() {
    var out = [];
    ((cached && cached.episodes) || []).forEach(function (ep) {
      if (epFilterEl.value && String(ep.ep) !== epFilterEl.value) return;
      out = out.concat(epUsable(ep));
    });
    return out;
  }
  function useThese(shots, label) {
    if (!shots.length) { NS.toast('现在没有可以“用这版”的镜（要有片子、还没定稿）'); return; }
    NS.confirm(label + '（' + shots.length + ' 镜）',
      '<p>这 ' + shots.length + ' 镜都定为“用这版”，之后这一集就能合成正片。</p>' +
      '<p class="muted small">只定有片子的镜；已驳回的、还没触发的成人镜不会被批量改。哪一镜后悔了，点进去按“撤销定稿”就行。</p>' +
      '<p class="muted small">' + esc(shots.slice(0, 12).join('、')) + (shots.length > 12 ? ' 等' : '') + '</p>', '全部用这版')
      .then(function (ok) {
        if (!ok) return null;
        return NS.write('一键用这版', '/api/reviews/bulk', { method: 'POST', json: { run_id: currentRun(), shots: shots, state: 'passed', by: 'owner' } })
          .then(function (r) {
            if (!r) return;
            var sk = Object.keys(r.skipped || {}), rj = Object.keys(r.rejected || {});
            NS.toast('已定稿 ' + (r.done || []).length + ' 镜' + (sk.length ? '；跳过 ' + sk.length + ' 镜（' +
              sk.slice(0, 4).map(function (x) { return x + '：' + r.skipped[x]; }).join('；') + (sk.length > 4 ? '…' : '') + '）' : '') +
              (rj.length ? '；已驳回的 ' + rj.length + ' 镜没动' : ''), 'ok', 6000);
            loadOverview();
          });
      }).catch(function (e) { NS.toast(e.message, 'bad'); });
  }
  $('#bulk-all').addEventListener('click', function () {
    useThese(visibleUsable(), epFilterEl.value ? '第 ' + epFilterEl.value + ' 集全部用这版' : '全部集用这版');
  });

  // ---------------------------------------------------------------- 加载
  function setLive(ok, text) {
    $('#live').className = 'live' + (ok ? '' : ' down');
    $('#updated-at').textContent = text;
  }
  function loadOverview() {
    return NS.get('/api/overview?run=' + encodeURIComponent(currentRun()))
      .then(function (data) {
        cached = data; lastOk = Date.now();
        $('#error').hidden = true;
        setLive(true, '实时 · ' + new Date().toLocaleTimeString().slice(0, 5) + ' 更新');
        loadScript(data);
        render();
        fixActive(data);
      })
      .catch(function (err) {
        setLive(false, '连不上');
        var e = $('#error');
        e.textContent = '总览没加载出来：' + err.message + (cached ? '\n（下面是 ' + Math.round((Date.now() - lastOk) / 60000) + ' 分钟前的数据）' : '') +
          '\n常见原因：笔记本到服务器的隧道断了（等它自己重连，或叫 PS-MASTER 看一下）。';
        e.hidden = false;
      });
  }
  // 尽力版的镜：图换成 takes.json 里真正的当前版（见 common.js NS.applyActive）
  function fixActive(data) {
    var ps = [];
    (data.episodes || []).forEach(function (ep) {
      if (epFilterEl.value && String(ep.ep) !== epFilterEl.value) return;
      (ep.shots || []).forEach(function (s) {
        if ((s.first && s.first.state === 'best_effort') || (s.last && s.last.state === 'best_effort'))
          ps.push(NS.takes(s).then(function (td) { return NS.applyActive(s, td); }));
      });
    });
    if (ps.length) Promise.all(ps).then(function (ch) { if (data === cached && ch.some(Boolean)) render(); });
  }
  var scriptAsked = false;
  function loadScript(data) {
    if (scriptAsked) return;
    var any = null;
    (data.episodes || []).some(function (ep) { return (ep.shots || []).some(function (s) { any = s; return !!(s.first && s.first.url); }); });
    if (!any) return;
    scriptAsked = true;
    NS.script(NS.hostOf(any)).then(function (s) { script = s || {}; render(); });
  }

  // ---------------------------------------------------------------- 顶栏五阶段
  function renderStages(d) {
    var t = d.totals || {};
    var st = [['剧本', t.script], ['首帧', t.first], ['尾帧', t.last], ['视频定稿', t.video], ['成片', t.cut]];
    var cur = -1;
    st.forEach(function (x, i) { if (cur < 0 && x[1] && x[1][0] < x[1][1]) cur = i; });
    $('#stages').innerHTML = st.map(function (x, i) {
      var a = (x[1] || [0, 0])[0], b = (x[1] || [0, 0])[1], done = b > 0 && a >= b;
      var pct = b ? Math.round(a / b * 100) : 0;
      return '<div class="stage ' + (done ? 'done' : (i === cur ? 'cur' : '')) + '" title="' + esc(x[0]) + ' ' + a + ' / ' + b + '">' +
        '<span class="no">' + (done ? '✓' : (i + 1)) + '</span><div class="stage-wrap"><div class="stage-top"><span>' + esc(x[0]) +
        '</span><span class="val">' + a + '/' + b + '</span></div><div class="bar"><i style="width:' + pct + '%"></i></div></div></div>';
    }).join('');
  }

  // ---------------------------------------------------------------- 横幅：现在要你做什么
  function counts(eps) {
    var c = { you: 0, review: 0, adult: 0, busy: 0, bad: 0, best: 0, final: 0, total: 0 };
    eps.forEach(function (ep) {
      (ep.shots || []).forEach(function (s) {
        var k = NS.shotStatus(s).key;
        c.total++;
        if (k === 'review') { c.you++; c.review++; }
        if (k === 'adult') { c.you++; c.adult++; }
        if (k === 'busy') c.busy++;
        if (k === 'bad' || k === 'rejected') c.bad++;
        if (k === 'final') c.final++;
        if (isBest(s)) c.best++;
      });
    });
    return c;
  }
  function isBest(s) { return ['first', 'last', 'video'].some(function (k) { return s[k] && s[k].state === 'best_effort'; }); }
  function renderBanner(d) {
    var eps = (d.episodes || []).filter(function (ep) { return !epFilterEl.value || String(ep.ep) === epFilterEl.value; });
    var c = counts(eps), v = d.videos || {};
    var n = visibleUsable().length;
    var title;
    if (c.review) title = '有 ' + c.review + ' 镜片子等你看' + (c.adult ? '，另有 ' + c.adult + ' 个成人镜等你触发' : '');
    else if (c.adult) title = c.adult + ' 个成人镜等你触发（触发前系统不会出这些镜）';
    else if (c.busy) title = '系统在出，现在不用你管';
    else if (c.bad) title = '有 ' + c.bad + ' 镜卡住 / 失败，系统在等处理';
    else title = c.total && c.final === c.total ? '全部定稿 ✓' : '暂时没有要你处理的';
    $('#banner-title').textContent = title;
    var left = (v.rendering || 0) + (v.queued || 0);
    $('#banner-facts').innerHTML =
      '<span>视频定稿 <b>' + (v.final || 0) + '</b> / ' + (v.total || 0) + '</span>' +
      '<span>出片中 <b>' + (v.rendering || 0) + '</b> · 排队 <b>' + (v.queued || 0) + '</b></span>' +
      (left && v.eta_all ? '<span>预计 <b>' + esc(NS.time(v.eta_all)) + '</b> 出完（单镜约 ' + Math.round((v.median_s || 0) / 60) + ' 分钟）</span>' : '') +
      '<span>尽力版 <b>' + c.best + '</b> 镜（自动用了，可以抽看）</span>' +
      (c.bad ? '<span style="color:var(--bad)">卡住 / 失败 <b>' + c.bad + '</b></span>' : '') +
      (v.missing_file ? '<span style="color:var(--warn)">片子文件丢了 <b>' + v.missing_file + '</b></span>' : '');
    var btn = $('#bulk-all');
    btn.disabled = n === 0;
    btn.textContent = '✓ ' + (epFilterEl.value ? '第 ' + epFilterEl.value + ' 集' : '') + '全部用这版（' + n + ' 镜）';
    $('#bulk-hint').textContent = n ? '只定有片子、还没定稿的镜；已驳回、未触发的成人镜不动，事后单镜可“撤销定稿”'
      : '现在没有待你定的片子；片子出来会自动出现在这里';
  }

  // ---------------------------------------------------------------- 筛选芯片
  var STATUS = [['', '全部'], ['you', '要你处理'], ['busy', '在办'], ['bad', '卡住 / 失败'], ['best_effort', '尽力版'], ['final', '已定稿']];
  function matches(shot, f) {
    if (!f) return true;
    var k = NS.shotStatus(shot).key;
    if (f === 'you') return k === 'review' || k === 'adult';
    if (f === 'busy') return k === 'busy' || k === 'wait';
    if (f === 'bad') return k === 'bad' || k === 'rejected';
    if (f === 'best_effort') return isBest(shot);
    if (f === 'final') return k === 'final';
    return true;
  }
  function renderChips(d) {
    var eps = d.episodes || [];
    $('#ep-chips').innerHTML = '<span class="label">集</span><button type="button" class="chip' + (!epFilterEl.value ? ' on' : '') + '" data-ep="">全部</button>' +
      eps.map(function (ep) {
        var c = counts([ep]);
        return '<button type="button" class="chip' + (String(ep.ep) === epFilterEl.value ? ' on' : '') + (c.you ? ' you' : '') + '" data-ep="' + esc(ep.ep) + '"' + (c.you ? ' title="有 ' + c.you + ' 镜要你处理"' : '') + '>' +
          '第' + esc(ep.ep) + '集' + (ep.title ? ' ' + esc(ep.title) : '') + ' <span class="n">' + ep.video_done + '/' + ep.total + '</span></button>';
      }).join('');
    var pool = eps.filter(function (ep) { return !epFilterEl.value || String(ep.ep) === epFilterEl.value; });
    var all = [];
    pool.forEach(function (ep) { all = all.concat(ep.shots || []); });
    $('#status-chips').innerHTML = '<span class="label">状态</span>' + STATUS.map(function (s) {
      var n = all.filter(function (x) { return matches(x, s[0]); }).length;
      return '<button type="button" class="chip' + (statusFilterEl.value === s[0] ? ' on' : '') + '" data-st="' + s[0] + '">' + esc(s[1]) +
        ' <span class="n">' + n + '</span></button>';
    }).join('');
    var keep = epFilterEl.value;
    epFilterEl.innerHTML = '<option value="">全部集</option>' + eps.map(function (ep) { return '<option value="' + esc(ep.ep) + '">第 ' + esc(ep.ep) + ' 集</option>'; }).join('');
    epFilterEl.value = keep;
  }
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('button[data-ep]');
    if (b && b.closest('#ep-chips')) { epFilterEl.value = b.getAttribute('data-ep'); syncUrl(); render(); if (cached) fixActive(cached); return; }
    var s = e.target.closest && e.target.closest('button[data-st]');
    if (s) { statusFilterEl.value = s.getAttribute('data-st'); syncUrl(); render(); }
  });
  function syncUrl() {
    var p = new URLSearchParams(window.location.search);
    ['ep', 'st'].forEach(function (k) { p.delete(k); });
    if (epFilterEl.value) p.set('ep', epFilterEl.value);
    if (statusFilterEl.value) p.set('st', statusFilterEl.value);
    window.history.replaceState(null, '', window.location.pathname + (p.toString() ? '?' + p.toString() : ''));
  }

  // ---------------------------------------------------------------- 一镜三格
  function ring(p) {
    var r = 22, c = 2 * Math.PI * r, v = Math.max(0.03, Math.min(1, p || 0));
    return '<div class="ring"><svg width="56" height="56"><circle cx="28" cy="28" r="' + r + '" fill="none" stroke="var(--line-2)" stroke-width="4"/>' +
      '<circle cx="28" cy="28" r="' + r + '" fill="none" stroke="var(--busy)" stroke-width="4" stroke-linecap="round" stroke-dasharray="' +
      (c * v).toFixed(1) + ' ' + c.toFixed(1) + '"/></svg><b>' + Math.round((p || 0) * 100) + '%</b></div>';
  }
  function frameTile(shot, k, cap) {
    var st = shot[k] || {}, lab = NS.frameLabel(st.state);
    var n = st.n ? ' #' + esc(st.n) : '';
    if (st.url && st.state !== 'fallback') {
      return '<button type="button" class="tile ' + (lab[1] === 'warn' ? 'warn' : '') + '" data-zoom="' + esc(shot.id) + '|' + k + '" title="点开看大图">' +
        '<img loading="lazy" src="' + esc(st.url) + '" alt="' + esc(shot.id + ' ' + cap) + '"><span class="cap">' + cap + n + '</span>' +
        '<span class="st">' + NS.pill(lab[0], lab[1]) + '</span></button>';
    }
    var ph = st.state === 'fallback' ? '这镜不用尾帧<br><span class="small">视频只给首帧</span>' :
      st.state === 'running' ? '在出图…' : st.state === 'pending' ? '排队' : st.state === 'blocked' ? '卡住' : st.state === 'failed' ? '失败' : '未出';
    return '<div class="tile empty ' + (lab[1] === 'bad' ? 'bad' : '') + '"><span class="cap">' + cap + '</span><span class="ph">' + ph + '</span></div>';
  }
  function videoTile(shot) {
    var v = shot.video || {}, lab = NS.videoLabel(v);
    var poster = (shot.last && shot.last.url && shot.last.state !== 'fallback' ? shot.last.url : null) || (shot.first && shot.first.url) || '';
    if (v.url) {
      return '<button type="button" class="tile" data-zoom="' + esc(shot.id) + '|video" title="点开播放">' +
        (poster ? '<img loading="lazy" src="' + esc(poster) + '" alt="">' : '') + '<span class="cap">视频' + (v.n ? ' #' + esc(v.n) : '') + '</span>' +
        '<span class="playbtn">▶</span><span class="st">' + NS.pill(lab[0], lab[1]) + '</span></button>';
    }
    if (v.state === 'rendering') {
      return '<div class="tile"><span class="cap">视频</span>' + ring(v.progress) +
        '<span class="st">' + NS.pill(v.eta ? '约 ' + NS.time(v.eta) + ' 好' : '在出', 'busy') + '</span></div>';
    }
    var ph = { queued: '排队第 ' + (v.queue_pos || '?') + '<br><span class="small">' + (v.eta ? '约 ' + NS.time(v.eta) + ' 开出' : '') + '</span>',
      need_frames: '等首尾帧', blocked: '卡住', failed: '失败', todo: '未出' }[v.state] || esc(lab[0]);
    return '<div class="tile empty ' + (lab[1] === 'bad' ? 'bad' : '') + '"><span class="cap">视频</span><span class="ph">' + ph + '</span></div>';
  }
  function shotRow(shot) {
    var s = NS.shotStatus(shot), sc = script[shot.id];
    var why = NS.reason(shot);
    var reviewUrl = 'review.html?shot=' + encodeURIComponent(shot.id) + '&run=' + encodeURIComponent(currentRun());
    var facts = [];
    if (sc && sc.duration) facts.push(esc(sc.duration) + ' 秒');
    if (shot.candidates) facts.push('视频候选 ' + esc(shot.candidates) + ' 版');
    if (shot.review === 'passed') facts.push('已定稿');
    if (shot.stale && shot.stale.length) facts.push('<span style="color:var(--warn)">过期：' + esc(shot.stale.join('、')) + '</span>');
    if (shot.stale_global && shot.stale_global.length) facts.push('全局过期（仅提示）：' + esc(shot.stale_global.join('、')));
    if (shot.missing && shot.missing.length && s.key !== 'final') facts.push('还缺：' + esc(shot.missing.map(function (m) { return { first: '首帧', last: '尾帧', video: '视频' }[m] || m; }).join('、')));
    if (shot.video && shot.video.tries) facts.push('视频第 ' + esc(shot.video.tries) + ' 次');
    var line = NS.dialogue(sc);
    var acts = '';
    if (usable(shot)) acts += '<button type="button" class="primary" data-use="' + esc(shot.id) + '">✓ 用这版</button>';
    if (s.key === 'adult') acts += '<a class="btn" href="' + reviewUrl + '#trigger">去触发 →</a>';
    acts += '<a class="btn" href="' + reviewUrl + '">' + (s.key === 'review' ? '看片 · 修改' : '打开这镜') + '</a>';
    return '<article class="shot' + (s.you ? ' you' : '') + '" id="shot-' + esc(shot.id) + '">' +
      '<div class="sid-col"><div class="sid"><a href="' + reviewUrl + '">S' + esc(shot.index) + '</a></div>' + NS.pill(s.text, s.tone) + '</div>' +
      '<div class="tiles">' + frameTile(shot, 'first', '首帧') + frameTile(shot, 'last', '尾帧') + videoTile(shot) + '</div>' +
      '<div class="body">' +
        (sc ? '<div class="script" title="' + esc(sc.img_prompt || '') + '">' + esc(NS.scriptLine(sc)) + '</div>' : '') +
        (line ? '<div class="line">💬 ' + esc(line) + '</div>' : '') +
        (why ? '<div class="why ' + (s.tone === 'bad' ? 'bad' : '') + (openWhy[shot.id] ? ' open' : '') + '" data-why="' + esc(shot.id) + '" title="点一下展开 / 收起">' +
          '<span class="t">' + (s.key === 'review' || isBest(shot) ? '没全过的地方：' : '原因：') + esc(why) + '</span></div>' : '') +
        (facts.length ? '<div class="facts">' + facts.join('<span>·</span>') + '</div>' : '') +
      '</div>' +
      '<div class="acts">' + acts + '</div></article>';
  }

  // ---------------------------------------------------------------- 一集
  function filmCard(ep) {
    var f = ep.film;
    if (!f) {
      var fj = ep.film_job;
      return '<div class="film"><span class="muted">成片：' + (fj ? (fj.state === 'blocked' ? '还拼不了 · ' + esc((fj.error || '').slice(0, 60)) : '排队中') : '还没拼') + '</span></div>';
    }
    return '<div class="film">' + (f.url && !f.file_missing ? '<button type="button" class="play" data-film="' + esc(ep.ep) + '" title="播放成片">▶</button>' : '') +
      '<div><div><b>' + (f.kind === 'final' ? '正片' : '预览片') + ' v' + esc(f.version) + '</b>' +
      (f.dur_s ? ' · ' + Math.round(f.dur_s) + ' 秒' : '') + (f.missing_n ? ' · <span class="warn">缺 ' + esc(f.missing_n) + ' 镜用占位</span>' : '') + '</div>' +
      (f.warning ? '<div class="warn small">⚠ ' + esc(f.warning) + '</div>' : '') +
      (f.file_missing ? '<div class="warn small">⚠ 成片文件在本机找不到</div>' : '') +
      ((f.missing && f.missing.length) ? '<div class="muted small">占位：' + esc(f.missing.join('、')) + '</div>' : '') +
      '</div></div>';
  }
  function timingLine(ep) {
    var t = ep.timing;
    if (!t || !t.wall_min) return '';
    var over = t.budget_min && t.wall_min > t.budget_min;
    return '<span>耗时 <b class="' + (over ? 'over' : '') + '">' + Math.round(t.wall_min) + ' 分钟</b>' + (t.budget_min ? ' / 目标 ' + t.budget_min + ' 分钟' : '') +
      (over && t.top_cause ? '（主要慢在 ' + esc(t.top_cause) + '）' : '') + '</span>';
  }
  function renderEpisodes(d) {
    var html = '';
    (d.episodes || []).forEach(function (ep) {
      if (epFilterEl.value && String(ep.ep) !== epFilterEl.value) return;
      var shots = (ep.shots || []).filter(function (s) { return matches(s, statusFilterEl.value); });
      var all = ep.shots || [], n = all.length || 1;
      var fin = all.filter(function (s) { return NS.shotStatus(s).key === 'final'; }).length;
      var rev = all.filter(function (s) { var k = NS.shotStatus(s).key; return k === 'review' || k === 'adult'; }).length;
      var busy = all.filter(function (s) { return NS.shotStatus(s).key === 'busy'; }).length;
      var bad = all.filter(function (s) { var k = NS.shotStatus(s).key; return k === 'bad' || k === 'rejected'; }).length;
      var ph = NS.PHASE[ep.phase] || [ep.phase || '–', 'idle'];
      var uu = epUsable(ep);
      html += '<section class="episode" id="ep-' + esc(ep.ep) + '">' +
        '<div class="ep-head"><div>' +
          '<div class="ep-title"><h2>第 ' + esc(ep.ep) + ' 集' + (ep.title ? ' · ' + esc(ep.title) : '') + '</h2>' + NS.pill(ph[0], ph[1]) +
          '<span class="num">' + ep.total + ' 镜 · 首帧 ' + ep.first_done + ' · 尾帧 ' + ep.last_done + (ep.fallback_n ? '（' + ep.fallback_n + ' 镜不用尾帧）' : '') + ' · 视频定稿 ' + ep.video_done + '</span></div>' +
          '<div class="ep-meta">' + timingLine(ep) +
            (ep.start ? '<span>开工 <b>' + esc(NS.time(ep.start)) + '</b></span>' : '') +
            (ep.last_at ? '<span>最近动静 <b>' + esc(NS.time(ep.last_at)) + '</b></span>' : '') +
            '<span>成片：<b>' + (ep.assembled ? '已拼好（最新）' : '未拼 / 要重拼') + '</b></span></div>' +
          '<div class="ep-progress" title="绿 已定稿 · 蓝 要你看 · 青 在办 · 红 卡住"><i class="f" style="width:' + (fin / n * 100) + '%"></i><i class="r" style="width:' + (rev / n * 100) +
            '%"></i><i class="b" style="width:' + (busy / n * 100) + '%"></i><i class="x" style="width:' + (bad / n * 100) + '%"></i></div>' +
        '</div><div class="ep-actions">' + filmCard(ep) +
          (uu.length ? '<button type="button" class="primary" data-use-ep="' + esc(ep.ep) + '">✓ 本集全部用这版（' + uu.length + ' 镜）</button>' : '') +
        '</div></div>' +
        '<div class="strip">' + all.map(function (s) {
          var st = NS.shotStatus(s), img = (s.first && s.first.url) || '';
          return '<button type="button" class="th" data-jump="' + esc(s.id) + '" title="' + esc(s.id + ' · ' + st.text) + '">' +
            (img ? '<img loading="lazy" src="' + esc(img) + '" alt="">' : '') + '<span class="id">S' + esc(s.index) + '</span><span class="dot ' + st.tone + '"></span></button>';
        }).join('') + '</div>' +
        (shots.length ? shots.map(shotRow).join('') : '<p class="empty-note">这一集没有符合“' + esc((STATUS.filter(function (x) { return x[0] === statusFilterEl.value; })[0] || ['', ''])[1]) + '”的镜</p>') +
        '</section>';
    });
    episodesEl.innerHTML = html || '<p class="empty-note">还没有集数据</p>';
  }
  function renderDock(d) {
    var v = d.videos || {};
    $('#videos-summary').innerHTML = '视频共 <b>' + (v.total || 0) + '</b> · 已定稿 <b>' + (v.final || 0) + '</b> · 待你看 <b>' + (v.pending_review || 0) +
      '</b> · 尽力版 <b>' + (v.best_effort || 0) + '</b>（不算定稿） · 出片中 <b>' + (v.rendering || 0) + '</b> · 排队 <b>' + (v.queued || 0) +
      '</b> · 等首尾帧 <b>' + (v.need_frames || 0) + '</b> · 未出 <b>' + (v.todo || 0) + '</b> · 卡住 <b>' + (v.blocked || 0) + '</b> · 失败 <b>' + (v.failed || 0) + '</b>' +
      (v.missing_file ? ' · 文件丢了 <b>' + v.missing_file + '</b>' : '') +
      (v.median_s ? ' · 单镜中位 <b>' + Math.round(v.median_s) + ' 秒</b>' : '');
  }
  function render() {
    if (!cached) return;
    renderStages(cached);
    renderChips(cached);
    renderBanner(cached);
    renderEpisodes(cached);
    renderDock(cached);
  }

  // ---------------------------------------------------------------- 交互
  function findShot(id) {
    var out = null;
    ((cached && cached.episodes) || []).forEach(function (ep) { (ep.shots || []).forEach(function (s) { if (s.id === id) out = s; }); });
    return out;
  }
  function zoomItems(shot) {
    var items = [], sc = script[shot.id];
    [['first', '首帧'], ['last', '尾帧']].forEach(function (x) {
      var st = shot[x[0]] || {};
      if (st.url && st.state !== 'fallback') items.push({ key: x[0], url: st.url, title: shot.id + ' · ' + x[1] + (st.n ? ' #' + st.n : ''),
        caption: esc(NS.frameLabel(st.state)[0]) + (st.error ? ' · ' + esc(String(st.error).slice(0, 220)) : '') });
    });
    if (shot.video && shot.video.url) items.push({ key: 'video', url: shot.video.url, video: true, title: shot.id + ' · 视频', caption: sc ? esc(NS.dialogue(sc)) : '' });
    return items;
  }
  episodesEl.addEventListener('click', function (e) {
    var t = e.target;
    var z = t.closest('[data-zoom]');
    if (z) {
      var p = z.getAttribute('data-zoom').split('|'), shot = findShot(p[0]);
      if (!shot) return;
      var items = zoomItems(shot);
      NS.lightbox(items, Math.max(0, items.map(function (x) { return x.key; }).indexOf(p[1])));
      return;
    }
    var u = t.closest('button[data-use]');
    if (u) { useThese([u.getAttribute('data-use')], u.getAttribute('data-use') + ' 用这版'); return; }
    var ue = t.closest('button[data-use-ep]');
    if (ue) {
      var epn = ue.getAttribute('data-use-ep'), ids = [];
      ((cached && cached.episodes) || []).forEach(function (ep) { if (String(ep.ep) === epn) ids = epUsable(ep); });
      useThese(ids, '第 ' + epn + ' 集全部用这版');
      return;
    }
    var w = t.closest('[data-why]');
    if (w) { var id = w.getAttribute('data-why'); openWhy[id] = !openWhy[id]; w.classList.toggle('open'); return; }
    var j = t.closest('[data-jump]');
    if (j) {
      var el = document.getElementById('shot-' + j.getAttribute('data-jump'));
      if (!el) { statusFilterEl.value = ''; syncUrl(); render(); el = document.getElementById('shot-' + j.getAttribute('data-jump')); }
      if (el) { el.scrollIntoView({ behavior: 'smooth', block: 'center' }); el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash'); }
      return;
    }
    var f = t.closest('[data-film]');
    if (f) {
      var ep = ((cached && cached.episodes) || []).filter(function (x) { return String(x.ep) === f.getAttribute('data-film'); })[0];
      if (ep && ep.film && ep.film.url) NS.lightbox([{ url: ep.film.url, video: true, title: '第 ' + ep.ep + ' 集成片 v' + ep.film.version }], 0);
    }
  });

  $('#refresh-btn').addEventListener('click', loadOverview);
  $('#run-apply').addEventListener('click', function () {
    var p = new URLSearchParams(window.location.search);
    p.set('run', runInput.value.trim() || 'series10_v2');
    window.history.replaceState(null, '', window.location.pathname + '?' + p.toString());
    NS.params = p; scriptAsked = false; script = {};
    loadOverview();
  });
  runInput.addEventListener('keydown', function (e) { if (e.key === 'Enter') $('#run-apply').click(); });
  document.addEventListener('keydown', function (e) {
    if (e.target.closest && e.target.closest('input, textarea, select, .layer, .lightbox')) return;
    if (document.querySelector('.layer, .lightbox')) return;
    if (e.key === 'r' || e.key === 'R') { loadOverview(); }
    else if (/^[1-9]$/.test(e.key) && cached) {
      var ep = (cached.episodes || [])[parseInt(e.key, 10) - 1];
      if (ep) { epFilterEl.value = String(ep.ep); syncUrl(); render(); window.scrollTo({ top: 0, behavior: 'smooth' }); }
    } else if (e.key === '0') { epFilterEl.value = ''; syncUrl(); render(); }
  });
  statusFilterEl.addEventListener('change', render);
  epFilterEl.addEventListener('change', render);

  var init = NS.params;
  if (init.get('run')) runInput.value = init.get('run');
  epFilterEl.innerHTML = '<option value="' + esc(init.get('ep') || '') + '"></option>';
  epFilterEl.value = init.get('ep') || '';
  statusFilterEl.value = init.get('st') || '';
  NS.bindKeyChip();
  NS.initTheme();
  loadOverview();
  setInterval(function () { if (!document.hidden && !document.querySelector('.layer, .lightbox')) loadOverview(); }, POLL_MS);
})();
