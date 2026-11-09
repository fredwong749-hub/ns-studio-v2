/* NS2 总览页：只调 GET /api/overview，轮询 15 秒。 */
(function () {
  'use strict';

  var POLL_MS = 15000;
  var DEFAULT_RUN = 'series10_v2';

  var runInput = document.getElementById('run-input');
  var runApplyBtn = document.getElementById('run-apply');
  var refreshBtn = document.getElementById('refresh-btn');
  var updatedAtEl = document.getElementById('updated-at');
  var errorEl = document.getElementById('error');
  var epFilterEl = document.getElementById('ep-filter');
  var statusFilterEl = document.getElementById('status-filter');
  var episodesEl = document.getElementById('episodes');
  var videosSummaryEl = document.getElementById('videos-summary');

  var cached = null;      // 最近一次 /api/overview 的完整返回

  // ---- 密钥：页面顶上粘一次，经 key.js 存本浏览器（和审片页同一份），90 天到期 ----
  function savedKey() { return window.NS2Key.get(); }
  function setKeyMsg(text, ok) {
    var el = document.getElementById('key-msg');
    el.textContent = text || '';
    el.className = ok === undefined ? 'muted' : (ok ? 'ok-msg' : 'err-msg');
  }
  function renderKeybar() {
    var has = !!savedKey();
    document.getElementById('key-saved').hidden = !has;
    document.getElementById('key-form').hidden = has;
  }
  function saveKey() {
    var k = (document.getElementById('key-input').value || '').trim();
    if (!k) { setKeyMsg('先把密钥粘进框里', false); return; }
    setKeyMsg('正在核对……');
    fetch('/api/auth/check', { method: 'POST', headers: { 'X-NS2-Key': k } })
      .then(function (resp) {
        if (resp.status === 401) { setKeyMsg('密钥不对：要整串复制，前后不要多空格', false); return; }
        if (!resp.ok) { setKeyMsg('核对失败（' + resp.status + '），稍后再试', false); return; }
        if (!window.NS2Key.set(k)) { setKeyMsg('浏览器不让存，只能这次用', false); }
        document.getElementById('key-input').value = '';
        renderKeybar();
        setKeyMsg('密钥正确，已记住 90 天 ✓', true);
      })
      .catch(function (err) { setKeyMsg('连不上服务器：' + err.message, false); });
  }
  document.getElementById('key-save').addEventListener('click', saveKey);
  document.getElementById('key-input').addEventListener('keydown', function (e) { if (e.key === 'Enter') saveKey(); });
  document.getElementById('key-change').addEventListener('click', function () {
    window.NS2Key.clear();
    renderKeybar(); setKeyMsg('');
  });

  // ---- 一键用这版：有片（待审片 / 尽力版）且还没定稿的镜 ----
  function usable(shot) {
    var vs = (shot.video && shot.video.state) || '';
    return (vs === 'pending_review' || vs === 'best_effort') && shot.review !== 'passed' && shot.review !== 'rejected' &&
      !(shot.adult && !shot.triggered);   // 审查 3 N7：已驳回 / 未触发的成人镜不进一键
  }
  function epUsable(ep) { return (ep.shots || []).filter(usable).map(function (s) { return s.id; }); }
  function visibleUsable() {
    var out = [];
    var epFilter = epFilterEl.value;
    ((cached && cached.episodes) || []).forEach(function (ep) {
      if (epFilter && String(ep.ep) !== epFilter) return;
      out = out.concat(epUsable(ep));
    });
    return out;
  }
  function setBulkMsg(text, ok) {
    var el = document.getElementById('bulk-msg');
    el.textContent = text || '';
    el.className = ok ? 'ok-msg' : (ok === false ? 'err-msg' : '');
  }
  function useThese(shots, label) {
    if (!shots.length) { setBulkMsg('没有可以“用这版”的镜', false); return; }
    var k = savedKey();
    if (!k) { setBulkMsg('先在上面粘一次密钥再点', false); document.getElementById('key-input').focus(); return; }
    if (!window.confirm(label + '：共 ' + shots.length + ' 镜都定为“用这版”？\n（只定有片子的镜；已驳回的、未触发的成人镜不会被批量改；以后想改，单镜点进去点“撤销定稿”或“驳回”）')) return;
    setBulkMsg('正在提交 ' + shots.length + ' 镜……');
    fetch('/api/reviews/bulk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-NS2-Key': k },
      body: JSON.stringify({ run_id: currentRun(), shots: shots, state: 'passed', by: 'owner' })
    }).then(function (resp) {
      if (resp.status === 401) {
        window.NS2Key.clear();
        renderKeybar(); setBulkMsg('密钥失效了，请在上面重新粘一次', false); return null;
      }
      if (!resp.ok) return resp.text().then(function (t) { throw new Error(resp.status + ' ' + t); });
      return resp.json();
    }).then(function (r) {
      if (!r) return;
      var sk = Object.keys(r.skipped || {});
      setBulkMsg('已定稿 ' + r.done.length + ' 镜' + (sk.length ? '，跳过 ' + sk.length + ' 镜（' +
        sk.slice(0, 5).map(function (x) { return x + '：' + r.skipped[x]; }).join('；') + (sk.length > 5 ? '…' : '') + '）' : '') + ' ✓', true);
      loadOverview();
    }).catch(function (err) { setBulkMsg('提交失败：' + err.message, false); });
  }
  function renderBulkHint() {
    var n = visibleUsable().length;
    var btn = document.getElementById('bulk-all');
    btn.disabled = n === 0;
    btn.textContent = '✅ 全部用这版（' + n + ' 镜）';
    document.getElementById('bulk-hint').textContent = epFilterEl.value
      ? '只算当前筛选的第 ' + epFilterEl.value + ' 集里有片子、还没定稿的镜'
      : '所有集里有片子（待审片 + 尽力版）、还没定稿的镜；只想定某一集，先在下面“按集筛选”选那一集，或点那一集标题旁的按钮';
  }
  document.getElementById('bulk-all').addEventListener('click', function () {
    useThese(visibleUsable(), epFilterEl.value ? '第 ' + epFilterEl.value + ' 集全部用这版' : '全部集用这版');
  });
  document.getElementById('episodes').addEventListener('click', function (e) {
    if (!e.target.closest) return;
    var b = e.target.closest('button[data-use]');
    if (b) { useThese([b.getAttribute('data-use')], b.getAttribute('data-use') + ' 用这版'); return; }
    var be = e.target.closest('button[data-use-ep]');
    if (be) {
      var epn = be.getAttribute('data-use-ep');
      var ids = [];
      ((cached && cached.episodes) || []).forEach(function (ep) { if (String(ep.ep) === epn) ids = epUsable(ep); });
      useThese(ids, '第 ' + epn + ' 集全部用这版');
    }
  });
  var timer = null;

  function currentRun() {
    var params = new URLSearchParams(window.location.search);
    return params.get('run') || (runInput && runInput.value.trim()) || DEFAULT_RUN;
  }

  function pairText(pair) {
    if (Array.isArray(pair) && pair.length >= 2) return pair[0] + ' / ' + pair[1];
    if (pair === null || pair === undefined) return '–';
    return String(pair);
  }

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function badge(state) {
    if (!state) return '<span class="badge">–</span>';
    return '<span class="badge ' + esc(state) + '">' + esc(state) + '</span>';
  }

  function showError(text) {
    errorEl.textContent = text;
    errorEl.hidden = false;
  }

  function clearError() {
    errorEl.textContent = '';
    errorEl.hidden = true;
  }

  function loadOverview() {
    var run = currentRun();
    return fetch('/api/overview?run=' + encodeURIComponent(run))
      .then(function (resp) {
        if (!resp.ok) {
          return resp.text().then(function (t) {
            throw new Error('总览接口返回 ' + resp.status + '：' + (t || resp.statusText));
          });
        }
        return resp.json();
      })
      .then(function (data) {
        cached = data;
        clearError();
        render();
        updatedAtEl.textContent = '更新于 ' + new Date().toLocaleTimeString();
      })
      .catch(function (err) {
        showError('加载总览失败：' + err.message);
      });
  }

  function renderTotals(data) {
    var t = data.totals || {};
    document.getElementById('total-script').textContent = pairText(t.script);
    document.getElementById('total-first').textContent = pairText(t.first);
    document.getElementById('total-last').textContent = pairText(t.last);
    document.getElementById('total-video').textContent = pairText(t.video);
    document.getElementById('total-cut').textContent = pairText(t.cut);
  }

  function renderVideosSummary(data) {
    var v = data.videos || {};
    function n(k) { return (v[k] === null || v[k] === undefined) ? 0 : v[k]; }
    // best_effort 单独计数，不算进 done（final / video_done）。
    videosSummaryEl.innerHTML =
      '<span>视频总数 <b>' + n('total') + '</b></span>' +
      '<span>已定稿 <b>' + n('final') + '</b></span>' +
      '<span>渲染中 <b>' + n('rendering') + '</b></span>' +
      '<span>排队中 <b>' + n('queued') + '</b></span>' +
      '<span>待审片 <b>' + n('pending_review') + '</b></span>' +
      (n('missing_file') ? '<span>文件丢了 <b>' + n('missing_file') + '</b></span>' : '') +
      '<span class="be">尽力版 <b>' + n('best_effort') + '</b>（不计入已定稿）</span>' +
      '<span>缺首尾帧 <b>' + n('need_frames') + '</b></span>' +
      '<span>待出片 <b>' + n('todo') + '</b></span>' +
      '<span>失败 <b>' + n('failed') + '</b></span>' +
      '<span>卡住等人 <b>' + n('blocked') + '</b>（缺料 / 成人镜待触发，原因见每镜）</span>' +
      '<span>预计全部完成 <b>' + esc(v.eta_all || '–') + '</b></span>' +
      '<span>单镜中位耗时 <b>' + n('median_s') + ' 秒</b></span>';
  }

  function shotMatchesStatus(shot, status) {
    if (!status) return true;
    var states = [];
    ['first', 'last', 'video'].forEach(function (k) {
      if (shot[k] && shot[k].state) states.push(shot[k].state);
    });
    // 口径以视频为准（ns2/overview.py）：已定稿 = video.final；待审片 = video.pending_review（帧 ready 不算“待审片”）
    var vs = (shot.video && shot.video.state) || '';
    if (status === 'passed') return vs === 'final';
    if (status === 'pending_review') return vs === 'pending_review';
    if (status === 'blocked') return vs === 'blocked' || states.indexOf('blocked') !== -1 || !!shot.owner_trigger;
    if (status === 'need_frames') return vs === 'need_frames';
    if (status === 'best_effort') return states.indexOf('best_effort') !== -1;
    if (status === 'failed') {
      return states.indexOf('failed') !== -1 || shot.review === 'rejected';
    }
    if (status === 'todo') return states.indexOf('todo') !== -1;
    if (status === 'stale') return (shot.stale && shot.stale.length > 0);
    if (status === 'running') {
      return states.indexOf('running') !== -1 || states.indexOf('rendering') !== -1;
    }
    return states.indexOf(status) !== -1;
  }

  function stageCell(stage) {
    if (!stage) return '<td>–</td>';
    var extra = '';
    if (stage.n !== null && stage.n !== undefined) extra += ' #' + esc(stage.n);
    if (stage.progress !== null && stage.progress !== undefined) {
      extra += ' ' + Math.round(stage.progress * 100) + '%';
    }
    if (stage.eta) extra += ' 预计 ' + esc(stage.eta);
    if (stage.queue_pos !== null && stage.queue_pos !== undefined) {
      extra += ' 排队第 ' + esc(stage.queue_pos) + ' 位';
    }
    return '<td>' + badge(stage.state) + esc(extra) + '</td>';
  }

  function shotRow(shot) {
    var stale = (shot.stale && shot.stale.length)
      ? '<div>过期：' + esc(shot.stale.join('、')) + '</div>' : '';
    // stale_global 只作独立字段显示，不进 stale、不上卡片过期判定。
    var staleGlobal = (shot.stale_global && shot.stale_global.length)
      ? '<div class="stale-global">全局过期（仅提示）：' + esc(shot.stale_global.join('、')) + '</div>' : '';
    var missing = (shot.missing && shot.missing.length)
      ? '<div>缺：' + esc(shot.missing.join('、')) + '</div>' : '';
    // 文件不在本机：后端按口径把该镜 video 记为 todo；前端显示提示，不计 final。
    var note = '';
    if (shot.note) {
      note = '<div class="note">' + esc(shot.note) + '</div>';
    } else if (shot.video && shot.video.state === 'todo' &&
        shot.missing && shot.missing.indexOf('video') !== -1) {
      note = '<div class="note">片子还没拉到本机</div>';
    }
    var review = shot.review ? esc(shot.review) : '未审';
    return '<tr>' +
      '<td class="shot-id"><a href="review.html?shot=' + encodeURIComponent(shot.id) +
        '&amp;run=' + encodeURIComponent(currentRun()) + '">' + esc(shot.id) + '</a></td>' +
      stageCell(shot.first) + stageCell(shot.last) + stageCell(shot.video) +
      '<td>' + review + (usable(shot) ? ' <button type="button" class="use-btn" data-use="' + esc(shot.id) + '">用这版</button>' : '') + '</td>' +
      '<td>' + stale + staleGlobal + missing + note + '</td>' +
      '<td>' + ((shot.candidates === null || shot.candidates === undefined)
        ? '–' : esc(shot.candidates)) + '</td>' +
      '</tr>';
  }

  function renderEpisodes(data) {
    data = data || { episodes: [] };
    var epFilter = epFilterEl.value;
    var statusFilter = statusFilterEl.value;
    var episodes = data.episodes || [];
    var html = '';
    episodes.forEach(function (ep) {
      if (epFilter && String(ep.ep) !== epFilter) return;
      var shots = (ep.shots || []).filter(function (s) {
        return shotMatchesStatus(s, statusFilter);
      });
      var filmHtml = '';
      if (ep.film) {
        filmHtml = '<div class="film-info">成片 <b>' + esc(ep.film.name || '') + '</b>' +
          ' v' + esc(ep.film.version) +
          (ep.film.kind ? '（' + esc(ep.film.kind) + '）' : '') +
          (ep.film.missing_n ? ' 缺 ' + esc(ep.film.missing_n) + ' 镜' : '') +
          (ep.film.warning ? ' <span class="err-msg">⚠ ' + esc(ep.film.warning) + '</span>' : '') +
          (ep.film.file_missing ? ' <span class="err-msg">⚠ 成片文件在本机找不到</span>' : '') +
          ((ep.film.missing && ep.film.missing.length)
            ? '：' + esc(ep.film.missing.join('、')) : '') +
          (ep.film.url ? ' <a href="' + esc(ep.film.url) + '">打开</a>' : '') +
          '</div>';
      } else {
        filmHtml = '<div class="film-info">成片：尚未拼片</div>';
      }
      var rows = shots.map(shotRow).join('');
      html += '<section class="episode">' +
        '<div class="episode-head"><h2>第 ' + esc(ep.ep) + ' 集' +
          (ep.title ? ' · ' + esc(ep.title) : '') + '</h2>' +
          '<span class="phase">阶段：' + esc(ep.phase || '–') + '</span>' +
          '<span class="counts">首帧 ' + esc(ep.first_done) + ' / 尾帧 ' + esc(ep.last_done) +
          ' / 视频已定 ' + esc(ep.video_done) + ' / 共 ' + esc(ep.total) + ' 镜</span>' +
          '<span class="counts">成片：' + (ep.assembled ? '已拼好' : '未拼') + '</span>' +
          (epUsable(ep).length
            ? '<button type="button" data-use-ep="' + esc(ep.ep) + '">✅ 本集全部用这版（' + epUsable(ep).length + ' 镜）</button>' : '') +
          '</div>' +
        filmHtml +
        (rows
          ? '<table class="shots"><thead><tr><th>镜</th><th>首帧</th><th>尾帧</th>' +
            '<th>视频</th><th>审片</th><th>过期 / 缺失</th><th>候选</th></tr></thead>' +
            '<tbody>' + rows + '</tbody></table>'
          : '<p class="muted" style="padding:12px 16px">本集没有符合筛选条件的镜。</p>') +
        '</section>';
    });
    episodesEl.innerHTML = html || '<p class="muted">暂无集数据。</p>';
  }

  function renderEpFilter(data) {
    var keep = epFilterEl.value;
    var options = '<option value="">全部集</option>';
    (data.episodes || []).forEach(function (ep) {
      options += '<option value="' + esc(ep.ep) + '">第 ' + esc(ep.ep) + ' 集' +
        (ep.title ? ' · ' + esc(ep.title) : '') + '</option>';
    });
    epFilterEl.innerHTML = options;
    epFilterEl.value = keep;
  }

  function render() {
    if (!cached) return;
    renderTotals(cached);
    renderVideosSummary(cached);
    renderEpFilter(cached);
    renderEpisodes(cached);
    renderBulkHint();
  }

  function startPolling() {
    if (timer) clearInterval(timer);
    timer = setInterval(loadOverview, POLL_MS);
  }

  refreshBtn.addEventListener('click', loadOverview);
  runApplyBtn.addEventListener('click', function () {
    var params = new URLSearchParams(window.location.search);
    params.set('run', runInput.value.trim() || DEFAULT_RUN);
    window.history.replaceState(null, '', window.location.pathname + '?' + params.toString());
    loadOverview();
  });
  statusFilterEl.addEventListener('change', function () { renderEpisodes(cached); });
  // 筛选变化时用缓存重渲染，不重新请求。
  epFilterEl.addEventListener('change', function () { if (cached) { renderEpisodes(cached); renderBulkHint(); } });

  var initParams = new URLSearchParams(window.location.search);
  if (initParams.get('run')) runInput.value = initParams.get('run');
  renderKeybar();
  loadOverview();
  startPolling();
})();
