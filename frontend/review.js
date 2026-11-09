/* NS2 审片页（参照旧版 v3“精修”视图）：一镜三大格（首帧 / 尾帧 / 视频）+ 底部决定条（用这版 / 修改 / 更多）+ 右栏（剧本 / 版本 / 任务记录 / 状态明细）。
   数据：/api/overview 找镜；/api/jobs?shot= 任务记录；takes.json（文件口）看历次版本；导入稿 JSON 看剧本。
   写操作都经 NS2.write 带 X-NS2-Key（key.js 存本浏览器，90 天到期；没有 / 失效会弹层让粘一次）。 */
(function () {
  'use strict';
  var NS = window.NS2, esc = NS.esc, $ = NS.$;

  var shotId = NS.params.get('shot') || '';
  var run = NS.params.get('run') || 'series10_v2';
  var shot = null, episode = null, order = [], jobRows = [], takes = null, sc = null;
  var busy = false;

  $('#back-link').href = 'index.html?run=' + encodeURIComponent(run) + (shotId ? '&ep=' + parseInt((/^ep(\d+)/.exec(shotId) || [0, ''])[1], 10) : '');

  // 契约项代号 → 大白话（不认识的原样显示）
  var CODE = { G1: '看镜头 / 排排站', Q1: '脸不像本人', Q2: '服装不对', Q3: '场景不对', Q4: '构图不对', Q5: '手 / 肢体', Q6: '画面里有字',
    Q7: '画质', Q8: '光线', Q9: '看图检测没过', Q10: '人数 / 站位不对', FL: '尾帧和首帧太像', QB: '背景', I2: '提示词自相矛盾' };
  function blockingOf(err) {
    var m = /契约没过：([A-Z0-9/]+)/.exec(err || '');
    return m ? m[1].split('/') : [];
  }
  function reasonsOf(err) {
    var s = String(err || '').replace(/^契约没过：[A-Z0-9/]+；?/, '');
    return s.split('；').map(function (x) { return x.trim(); }).filter(Boolean);
  }
  function tags(codes, tone) {
    return codes.length ? '<div class="blocking">' + codes.map(function (c) {
      return '<span class="tag ' + (tone || '') + '" title="' + esc(c) + '">' + esc(CODE[c] ? c + ' ' + CODE[c] : c) + '</span>';
    }).join('') + '</div>' : '';
  }

  // ---------------------------------------------------------------- 三大格
  function framePanel(k, cap) {
    var st = shot[k] || {}, lab = NS.frameLabel(st.state);
    var media;
    if (st.url && st.state !== 'fallback') media = '<button type="button" class="media" data-zoom="' + k + '"><img src="' + esc(st.url) + '" alt="' + esc(cap) + '"></button>';
    else media = '<div class="media"><div class="ph">' + (st.state === 'fallback' ? '这一镜不用尾帧<br><span class="small">视频只给首帧，运镜交给视频模型</span>' :
      st.state === 'running' ? '正在出图…' : st.state === 'pending' ? '排队中，前一步好了就出' : st.state === 'blocked' ? '卡住了，原因见下面' :
        st.state === 'failed' ? '失败，原因见下面' : '还没出') + '</div></div>';
    var codes = blockingOf(st.error), rs = reasonsOf(st.error);
    var why = '';
    if (st.error) why = '<p class="why">' + rs.map(esc).join('<br>') + '</p>';
    return '<div class="panel" id="panel-' + k + '"><div class="panel-head"><b>' + cap + '</b>' + NS.pill(lab[0], lab[1]) +
      (st.n ? '<span class="n">当前第 ' + esc(st.n) + ' 版</span>' : '') + '</div>' + media +
      (st.latest && st.latest.n ? '<p class="why small muted" style="border-top:none">最近一次出的是第 ' + esc(st.latest.n) + ' 版（没被选为当前），在“版本”里能看</p>' : '') +
      (codes.length ? tags(codes, st.state === 'best_effort' ? 'warn' : '') : '') + why + '</div>';
  }
  function videoPanel() {
    var v = shot.video || {}, lab = NS.videoLabel(v);
    var poster = (shot.last && shot.last.url && shot.last.state !== 'fallback' ? shot.last.url : '') || (shot.first && shot.first.url) || '';
    var media;
    if (v.url) media = '<div class="media"><video src="' + esc(v.url) + '" controls loop playsinline preload="metadata"' + (poster ? ' poster="' + esc(poster) + '"' : '') + '></video></div>';
    else if (v.state === 'rendering') {
      media = '<div class="media"><div class="ph"><b style="font-size:28px;color:var(--busy)">' + Math.round((v.progress || 0) * 100) + '%</b><br>正在出片' +
        (v.eta ? '<br><span class="small">预计 ' + esc(NS.time(v.eta)) + ' 好</span>' : '') + '</div></div>';
    } else {
      media = '<div class="media"><div class="ph">' + ({ queued: '排队第 ' + (v.queue_pos || '?') + (v.eta ? '<br><span class="small">预计 ' + esc(NS.time(v.eta)) + ' 开出</span>' : ''),
        need_frames: '等首尾帧好了再出', blocked: '卡住了，原因见下面', failed: '失败，原因见下面', todo: '还没出' }[v.state] || esc(lab[0])) + '</div></div>';
    }
    var why = (v.error || shot.note) ? '<p class="why">' + esc(v.error || shot.note) + '</p>' : '';
    return '<div class="panel" id="panel-video"><div class="panel-head"><b>视频</b>' + NS.pill(lab[0], lab[1]) +
      (v.n ? '<span class="n">第 ' + esc(v.n) + ' 版</span>' : '') + (v.tries ? '<span class="n">第 ' + esc(v.tries) + ' 次</span>' : '') + '</div>' + media + why + '</div>';
  }

  // ---------------------------------------------------------------- 右栏
  function renderScript() {
    var el = $('#tab-script');
    if (!sc) { el.innerHTML = '<p class="muted">剧本还没读到（导入稿在出图机上；读不到不影响审片）</p>'; return; }
    var dl = NS.dialogue(sc);
    el.innerHTML = '<dl class="kv">' +
      (sc.duration ? '<dt>时长</dt><dd>' + esc(sc.duration) + ' 秒</dd>' : '') +
      (sc.cast && sc.cast.length ? '<dt>人物</dt><dd>' + esc((sc.cast || []).map(function (c) { return typeof c === 'string' ? c : (c.name || c.id || ''); }).join('、')) + '</dd>' : '') +
      (sc.scene_id ? '<dt>场景</dt><dd>' + esc(sc.scene_detail || sc.scene_id) + '</dd>' : '') +
      (dl ? '<dt>台词</dt><dd>' + esc(dl) + '</dd>' : '') +
      '<dt>画面</dt><dd>' + esc(sc.img_prompt || '–') + '</dd>' +
      (sc.motion_prompt ? '<dt>动作</dt><dd>' + esc(sc.motion_prompt) + '</dd>' : '') +
      (sc.sound ? '<dt>声音</dt><dd>' + esc(sc.sound) + '</dd>' : '') +
      (sc.adult ? '<dt>成人</dt><dd>' + esc((sc.adult_reasons || []).join('、') || '是') + '</dd>' : '') +
      '</dl>';
  }
  function takesUrl() {
    var m = /^ep(\d+)-S(\d+)$/.exec(shotId);
    if (!m) return null;
    return '/files/' + encodeURIComponent(NS.hostOf(shot)) + '/renders/' + encodeURIComponent(run) + '/ep' + m[1] + '/S' + m[2] + '/takes.json';
  }
  function pfOf(t) { return (t && (t.postflight || t.contract)) || {}; }
  function renderVersions() {
    var el = $('#tab-versions');
    if (!takes) { el.innerHTML = '<p class="muted">版本记录还没读到</p>'; return; }
    var host = NS.hostOf(shot), act = takes.active || {}, be = takes.best_effort || {};
    var html = '';
    [['image', '首帧'], ['last', '尾帧']].forEach(function (x) {
      var list = (takes.takes || []).filter(function (t) { return t.stage === x[0] && t.file; }).sort(function (a, b) { return b.n - a.n; });
      if (!list.length) return;
      html += '<h4 style="margin:4px 0 8px">' + x[1] + ' · ' + list.length + ' 版' +
        (be[x[0]] && be[x[0]].n === act[x[0]] ? ' <span class="pill warn">当前是尽力版（自动用）</span>' : '') + '</h4><div class="versions" style="margin-bottom:14px">' +
        list.map(function (t) {
          var pf = pfOf(t), ok = pf.ok;
          return '<button type="button" class="ver' + (t.n === act[x[0]] ? ' active' : '') + '" data-ver="' + x[0] + '|' + t.n + '" title="第 ' + t.n + ' 版' +
            (pf.blocking && pf.blocking.length ? ' · 没过：' + esc(pf.blocking.join('/')) : '') + '">' +
            '<img loading="lazy" src="/files/' + esc(host) + '/' + esc(t.file) + '" alt="">' + (t.n === act[x[0]] ? '<span class="cur">当前</span>' : '') +
            '<span class="lab"><span>#' + t.n + '</span><span class="' + (ok ? 'y' : (ok === false ? 'x' : '')) + '">' +
            (ok ? '过' : ok === false ? '✗ ' + esc((pf.blocking || []).join('/')) : (t.bad ? '坏图' : '未打分')) + '</span></span></button>';
        }).join('') + '</div>';
    });
    el.innerHTML = html ? html + '<p class="muted small">点一张看大图和没过的原因。换“当前版”请用“修改”出新版；系统按规则自动挑当前版（过契约优先，全没过时用不过项最少的尽力版）。</p>'
      : '<p class="muted">还没有出过图</p>';
  }
  function renderCandidates() {
    var el = $('#candidates');
    if (!jobRows.length) { el.innerHTML = '<p class="muted">还没有任务记录（候选共 ' + esc(shot && shot.candidates || 0) + ' 版）</p>'; return; }
    var STEP = { first: '首帧', last: '尾帧', video: '视频', board: '分镜', film: '成片', script: '剧本', asset: '资产', assets: '资产' };
    el.innerHTML = jobRows.slice().reverse().map(function (job) {
      var r = job.result || {};
      var takeN = (r.take_n !== undefined) ? r.take_n : (r.take !== undefined ? r.take : null);
      var file = r.clip || r.file || r.path || r.output || '';   // 审查 3 N2：视频步真实字段是 clip / score
      var qc = (r.score !== undefined) ? r.score : r.qc;
      var qcText = (qc === null || qc === undefined) ? '' : (typeof qc === 'object' ? JSON.stringify(qc) : String(qc));
      var stl = { done: ['完成', 'ok'], best_effort: ['尽力版', 'warn'], failed: ['失败', 'bad'], blocked: ['卡住', 'bad'], running: ['在跑', 'busy'],
        queued: ['排队', 'idle'], na: ['不需要', 'idle'], cancelled: ['已取消', 'idle'] }[job.state] || [job.state, 'idle'];
      return '<div class="job"><div class="top"><b>' + esc(STEP[job.step] || job.step) + '</b>' + NS.pill(stl[0], stl[1]) +
        (takeN !== null ? '<span class="muted">第 ' + esc(takeN) + ' 版</span>' : '') + (job.tries ? '<span class="muted">试了 ' + esc(job.tries) + ' 次</span>' : '') +
        '<span class="spacer"></span><span class="muted small">#' + esc(job.id) + ' · ' + esc(NS.time(job.finished_at || job.started_at || job.created_at)) + '</span></div>' +
        (file ? '<div class="file">' + esc(String(file).split('/').slice(-1)[0]) + (qcText ? ' · 分数 ' + esc(qcText.slice(0, 120)) : '') + '</div>' : '') +
        (job.error ? '<div class="err">' + esc(String(job.error).slice(0, 400)) + '</div>' : '') + '</div>';
    }).join('');
  }
  function renderDetail() {
    var miss = (shot.missing && shot.missing.length) ? shot.missing.map(function (m) { return { first: '首帧', last: '尾帧', video: '视频' }[m] || m; }).join('、') : '无';
    $('#shot-detail').innerHTML = '<dl class="kv">' +
      '<dt>镜号</dt><dd>' + esc(shot.id) + '</dd>' +
      '<dt>所属集</dt><dd>' + (episode ? '第 ' + esc(episode.ep) + ' 集' + (episode.title ? ' · ' + esc(episode.title) : '') : '–') + '</dd>' +
      '<dt>审片</dt><dd>' + esc({ passed: '已定稿', rejected: '已驳回', unreviewed: '待审' }[shot.review] || '待审') + (shot.review_reason ? '（' + esc(shot.review_reason) + '）' : '') + '</dd>' +
      '<dt>过期</dt><dd>' + esc((shot.stale && shot.stale.length) ? shot.stale.join('、') : '无') + '</dd>' +
      '<dt>全局过期</dt><dd>' + esc((shot.stale_global && shot.stale_global.length) ? shot.stale_global.join('、') + '（仅提示）' : '无') + '</dd>' +
      '<dt>还缺</dt><dd>' + esc(miss) + '</dd>' +
      '<dt>视频候选</dt><dd>' + esc(shot.candidates === null || shot.candidates === undefined ? '–' : shot.candidates) + ' 版</dd>' +
      '<dt>成人镜</dt><dd>' + (shot.adult ? (shot.triggered ? '是，已触发' : '是，等你触发') : '否') + '</dd>' +
      (shot.note ? '<dt>备注</dt><dd>' + esc(shot.note) + '</dd>' : '') + '</dl>';
    $('#current').innerHTML = '<dl class="kv" style="margin-top:10px">' + [['first', '首帧'], ['last', '尾帧']].map(function (x) {
      var st = shot[x[0]] || {};
      return '<dt>' + x[1] + '</dt><dd>' + esc(NS.frameLabel(st.state)[0]) + (st.n ? ' · 第 ' + esc(st.n) + ' 版' : '') + (st.url ? ' · <a href="' + esc(st.url) + '" target="_blank">原图</a>' : '') + '</dd>';
    }).join('') + '<dt>视频</dt><dd>' + esc(NS.videoLabel(shot.video)[0]) + (shot.video && shot.video.url ? ' · <a href="' + esc(shot.video.url) + '" target="_blank">原片</a>' : '') + '</dd></dl>';
  }

  // ---------------------------------------------------------------- 整页
  function render() {
    if (!shot) return;
    var s = NS.shotStatus(shot);
    document.title = shotId + ' · NS2 审片';
    $('#shot-title').textContent = shotId;
    $('#shot-ep').textContent = episode ? '第 ' + episode.ep + ' 集' + (episode.title ? ' · ' + episode.title : '') : '';
    $('#shot-status').innerHTML = NS.pill(s.text, s.tone);
    var i = order.indexOf(shotId), inEp = episode ? (episode.shots || []).map(function (x) { return x.id; }) : [];
    $('#shot-pos').textContent = inEp.indexOf(shotId) >= 0 ? '本集 ' + (inEp.indexOf(shotId) + 1) + ' / ' + inEp.length : '';
    $('#prev-shot').disabled = i <= 0;
    $('#next-shot').disabled = i < 0 || i >= order.length - 1;
    $('#panels').innerHTML = framePanel('first', '首帧') + framePanel('last', '尾帧') + videoPanel();
    renderAdultButtons();
    var hasClip = !!(shot.video && shot.video.url);
    var passed = shot.review === 'passed';
    $('#btn-pass').disabled = passed;
    $('#btn-pass').textContent = passed ? '已定稿 ✓' : '✓ 用这版';
    $('#decide-state').innerHTML = passed ? '这一镜已定稿；想换，点“修改”出新版，或“更多 → 撤销定稿”' :
      shot.review === 'rejected' ? '已驳回' + (shot.review_reason ? '：' + esc(shot.review_reason) : '') + '；点“修改”重出' :
      hasClip ? '片子在左边，看完点“用这版”定稿，或“修改”' : '片子还没出来；可以先看首尾帧，不满意点“修改”';
    renderScript(); renderVersions(); renderCandidates(); renderDetail();
  }
  // 审查 3 堵点 3：触发 / 撤销按钮只在服务端判为成人镜的镜上出现
  function renderAdultButtons() {
    var isAdult = !!(shot && shot.adult);
    $('#adult-alert').hidden = !isAdult;
    $('#btn-adult').hidden = !isAdult || !!shot.triggered;
    $('#btn-untrigger').hidden = !isAdult || !shot.triggered;
  }
  function setLive(ok, t) { $('#live').className = 'live' + (ok ? '' : ' down'); $('#updated-at').textContent = t; }
  function loadShot() {
    return NS.get('/api/overview?run=' + encodeURIComponent(run))
      .then(function (data) {
        shot = null; episode = null; order = [];
        (data.episodes || []).forEach(function (ep) {
          (ep.shots || []).forEach(function (s) {
            order.push(s.id);
            if (s.id === shotId) { shot = s; episode = ep; }
          });
        });
        if (!shot) throw new Error('总览里找不到 ' + shotId + '（剧 ' + run + '）');
        $('#error').hidden = true;
        setLive(true, '实时 · ' + new Date().toLocaleTimeString().slice(0, 5));
        render();
        if (!sc) NS.script(NS.hostOf(shot)).then(function (m) { sc = m[shotId] || null; renderScript(); });
        var tu = takesUrl();
        var pTakes = tu ? NS.get(tu).then(function (d) {
          takes = d;
          NS.applyActive(shot, d);            // 三大格按 takes.json 当前版显示（尽力版自动用时可能不是最近一版）
          render();
        }).catch(function () { /* 没出过图 */ }) : null;
        var pJobs = NS.get('/api/jobs?shot=' + encodeURIComponent(shotId) + '&run=' + encodeURIComponent(run)).then(function (jobsData) {
          var rows = Array.isArray(jobsData) ? jobsData : (jobsData.jobs || []);
          jobRows = rows.filter(function (j) { return !j.shot || j.shot === shotId; });
          renderCandidates();
        }).catch(function () { /* 记录读不到不影响审片 */ });
        return Promise.all([pTakes, pJobs]);
      })
      .catch(function (err) {
        setLive(false, '连不上');
        $('#error').textContent = '没加载出来：' + err.message;
        $('#error').hidden = false;
      });
  }

  // ---------------------------------------------------------------- 写操作（带密钥 X-NS2-Key，经 NS2.write）
  function writeOp(label, url, options) {
    busy = true;
    return NS.write(label, url, options).then(function (r) {
      if (r === null) { NS.toast(label + '：没有密钥，已取消'); return null; }
      NS.toast(label + '：成功', 'ok');
      return loadShot();
    }).catch(function (err) { NS.toast(err.message, 'bad'); })
      .then(function (x) { busy = false; return x; });
  }
  function epNumber() {
    if (episode && episode.ep !== undefined && episode.ep !== null) return episode.ep;
    var m = /^ep(\d+)-/i.exec(shotId);
    return m ? parseInt(m[1], 10) : null;
  }
  function menuClose() { $('#more-items').hidden = true; }

  document.getElementById('btn-pass').addEventListener('click', function () {
    var hasClip = !!(shot && shot.video && shot.video.url);
    NS.confirm(shotId + ' 用这版？', hasClip ? '定稿后这一镜进正片。后悔了在“更多”里撤销。'
      : '<b>这一镜还没有片子。</b>现在定稿只记下“认可当前首尾帧”，片子出来后仍按这一版算。', '用这版').then(function (ok) {
      if (!ok) return;
      writeOp('定稿', '/api/shots/' + encodeURIComponent(shotId) + '/review', { method: 'PUT', json: { run_id: run, state: 'passed' } });
    });
  });

  // 修改（审查 3 N5：重出 / 按意见改 → POST /api/shots/{id}/jobs；写了意见 = 按意见改，只对首 / 尾帧）
  function setStage(stg) {
    $('#redo-stage').value = stg;
    Array.prototype.forEach.call(document.querySelectorAll('#redo-seg button'), function (b) { b.classList.toggle('on', b.getAttribute('data-stage') === stg); });
    $('#redo-edit').disabled = stg === 'video';
    $('#redo-edit').placeholder = stg === 'video' ? '视频不收修改意见：直接换种子重出（改画面请改首帧 / 尾帧）' :
      '想怎么改，写清楚（例：祁念的视线看向卓琳的手，不要看镜头；唐瑶站到左后，不和另外两人排成一排）。不写 = 换个种子重出。';
    Array.prototype.forEach.call(document.querySelectorAll('.panel'), function (p) { p.classList.toggle('sel', p.id === 'panel-' + stg); });
  }
  function openEdit(stg) {
    $('#edit-box').hidden = false;
    setStage(stg || (shot && shot.first && ['ready', 'passed'].indexOf(shot.first.state) < 0 ? 'first' : 'video'));
    $('#redo-edit').focus();
    $('#edit-box').scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  document.getElementById('btn-edit').addEventListener('click', function () { openEdit(); });
  document.getElementById('edit-cancel').addEventListener('click', function () {
    $('#edit-box').hidden = true;
    Array.prototype.forEach.call(document.querySelectorAll('.panel'), function (p) { p.classList.remove('sel'); });
  });
  document.getElementById('redo-seg').addEventListener('click', function (e) {
    var b = e.target.closest('button[data-stage]');
    if (b) setStage(b.getAttribute('data-stage'));
  });
  document.getElementById('btn-redo').addEventListener('click', function () {
    var stage = document.getElementById('redo-stage').value;
    var edit = (document.getElementById('redo-edit').value || '').trim();
    if (edit && stage === 'video') { NS.toast('修改意见只对首 / 尾帧；视频请直接重出', 'bad'); return; }
    var label = { first: '首帧', last: '尾帧', video: '视频' }[stage];
    NS.confirm('重出' + label + '？', (edit ? '按你的意见：<b>' + esc(edit) + '</b><br>' : '换个种子重出。<br>') +
      '<span class="muted small">会新起一行排队，占一张卡。' + (stage === 'first' ? '首帧换了，尾帧和视频会跟着重出。' : stage === 'last' ? '尾帧换了，视频会跟着重出。' : '') + '</span>', '提交')
      .then(function (ok) {
        if (!ok) return;
        var body = { run_id: run, stage: stage, mode: edit ? 'edit' : 'regen', by: 'manual' };
        if (edit) body.edit = edit;
        writeOp('重出' + label, '/api/shots/' + encodeURIComponent(shotId) + '/jobs', { method: 'POST', json: body }).then(function () {
          $('#edit-box').hidden = true; $('#redo-edit').value = '';
        });
      });
  });

  // 更多
  document.getElementById('more-btn').addEventListener('click', function (e) { e.stopPropagation(); $('#more-items').hidden = !$('#more-items').hidden; });
  document.addEventListener('click', function (e) { if (!e.target.closest('.menu')) menuClose(); });

  document.getElementById('btn-reject').addEventListener('click', function () {
    menuClose();
    NS.ask('驳回 ' + shotId, '写一句为什么不用（会记进审片记录；驳回后一键通过不会再把它定掉）。要重出请再点“修改”。', '例：表情太僵，唐瑶没在画面里', '驳回', true)
      .then(function (reason) {
        if (!reason) return;
        writeOp('驳回', '/api/shots/' + encodeURIComponent(shotId) + '/review', { method: 'PUT', json: { run_id: run, state: 'rejected', reason: reason } });
      });
  });
  // 审查 3 N8：反定稿（服务端 set_review 收 unreviewed）——一键通过后反悔用
  document.getElementById('btn-unreview').addEventListener('click', function () {
    menuClose();
    NS.confirm('撤销 ' + shotId + ' 的定稿 / 驳回？', '回到“待审”，片子和版本都不动。', '撤销').then(function (ok) {
      if (!ok) return;
      writeOp('撤销定稿', '/api/shots/' + encodeURIComponent(shotId) + '/review', { method: 'PUT', json: { run_id: run, state: 'unreviewed', reason: '项目方撤销定稿' } });
    });
  });
  document.getElementById('btn-cancel').addEventListener('click', function () {
    menuClose();
    NS.confirm('取消 ' + shotId + ' 还在排队的任务？', '只取消排队中的；已经在出的不会被打断。', '取消排队', true).then(function (ok) {
      if (!ok) return;
      writeOp('取消排队', '/api/shots/' + encodeURIComponent(shotId) + '/cancel?run=' + encodeURIComponent(run), { method: 'POST', json: {} });
    });
  });

  // 审查 3 堵点 4：默认不带 force（同输入幂等：签名没变就返回已有那一行，不出新版本）；只有明确点“强制重出成片”才带 force
  function assemble(force) {
    menuClose();
    var ep = epNumber();
    if (ep === null) { NS.toast('拼成片失败：看不出是第几集', 'bad'); return; }
    NS.confirm(force ? '强制重出第 ' + ep + ' 集成片？' : '拼第 ' + ep + ' 集成片？',
      force ? '输入没变也会出新一版成片（占机器）。一般用不到，除非上一版文件坏了。' : '本集各镜没变就不会重拼，直接给你上一版。', force ? '强制重出' : '拼成片', force)
      .then(function (ok) {
        if (!ok) return;
        var body = { run_id: run };
        if (force) body.force = true;
        writeOp(force ? '强制重出成片' : '拼成片', '/api/episodes/' + encodeURIComponent(ep) + '/assemble', { method: 'POST', json: body });
      });
  }
  document.getElementById('btn-assemble').addEventListener('click', function () { assemble(false); });
  document.getElementById('btn-assemble-force').addEventListener('click', function () { assemble(true); });

  document.getElementById('btn-adult').addEventListener('click', function () {
    NS.confirm('触发成人镜 ' + shotId + '？', '触发后这一镜的出图 / 出片才会开始（只由你操作）。触发错了可以撤销。', '触发').then(function (ok) {
      if (!ok) return;
      // B2：成人镜触发走专门接口 POST /api/shots/{id}/trigger（服务端先按剧本核实是成人镜、记 triggers 表、放行本镜排队行）
      writeOp('触发成人镜', '/api/shots/' + encodeURIComponent(shotId) + '/trigger', { method: 'POST', json: { run_id: run, by: 'owner' } });
    });
  });
  document.getElementById('btn-untrigger').addEventListener('click', function () {
    NS.confirm('撤销 ' + shotId + ' 的成人镜触发？', '本镜还在排队的出图 / 出片会重新挡住（已在跑的不打断，跑完的照记）。', '撤销触发', true).then(function (ok) {
      if (!ok) return;
      writeOp('撤销成人镜触发', '/api/shots/' + encodeURIComponent(shotId) + '/trigger?run_id=' + encodeURIComponent(run), { method: 'DELETE' });
    });
  });
  document.getElementById('btn-clear-key').addEventListener('click', function () {
    menuClose();
    window.NS2Key.clear(); NS.renderKeyChip();
    NS.toast('本机密钥已清除；下次写操作会重新要密钥');
  });

  // ---------------------------------------------------------------- 看大图 / 翻镜 / 标签页
  document.getElementById('panels').addEventListener('click', function (e) {
    var z = e.target.closest('[data-zoom]');
    if (!z || !shot) return;
    var items = [];
    [['first', '首帧'], ['last', '尾帧']].forEach(function (x) {
      var st = shot[x[0]] || {};
      if (st.url && st.state !== 'fallback') items.push({ k: x[0], url: st.url, title: shotId + ' · ' + x[1] + (st.n ? ' 第 ' + st.n + ' 版' : ''), caption: esc(reasonsOf(st.error).join('；')) });
    });
    NS.lightbox(items, Math.max(0, items.map(function (x) { return x.k; }).indexOf(z.getAttribute('data-zoom'))));
  });
  document.getElementById('tab-versions').addEventListener('click', function (e) {
    var b = e.target.closest('[data-ver]');
    if (!b || !takes) return;
    var p = b.getAttribute('data-ver').split('|'), host = NS.hostOf(shot);
    var list = (takes.takes || []).filter(function (t) { return t.stage === p[0] && t.file; }).sort(function (a, c) { return c.n - a.n; });
    NS.lightbox(list.map(function (t) {
      var pf = pfOf(t);
      return { url: '/files/' + host + '/' + t.file, title: shotId + ' · ' + (p[0] === 'image' ? '首帧' : '尾帧') + ' 第 ' + t.n + ' 版' +
        (t.n === (takes.active || {})[p[0]] ? '（当前）' : ''), caption: pf.ok ? '契约全过' : esc((pf.reasons || []).join('；') || (pf.ok === false ? '没过：' + (pf.blocking || []).join('/') : '没打分')) };
    }), list.map(function (t) { return String(t.n); }).indexOf(p[1]));
  });
  document.getElementById('tabs').addEventListener('click', function (e) {
    var b = e.target.closest('button[data-tab]');
    if (!b) return;
    Array.prototype.forEach.call(document.querySelectorAll('#tabs button'), function (x) { x.classList.toggle('on', x === b); });
    ['script', 'versions', 'jobs', 'detail'].forEach(function (k) { $('#tab-' + k).hidden = k !== b.getAttribute('data-tab'); });
  });
  function go(delta) {
    var i = order.indexOf(shotId), j = i + delta;
    if (i < 0 || j < 0 || j >= order.length) return;
    var p = new URLSearchParams(window.location.search);
    p.set('shot', order[j]);
    window.location.search = p.toString();
  }
  $('#prev-shot').addEventListener('click', function () { go(-1); });
  $('#next-shot').addEventListener('click', function () { go(1); });
  document.addEventListener('keydown', function (e) {
    if (e.target.closest && e.target.closest('input, textarea, select')) return;
    if (document.querySelector('.layer, .lightbox')) return;
    if (e.key === 'ArrowLeft') go(-1);
    else if (e.key === 'ArrowRight') go(1);
    else if (e.key === 'e' || e.key === 'E') { e.preventDefault(); openEdit(); }
  });

  // 审查 3 N6：审片页也轮询（15 s，同总览页）；写操作进行中 / 页面在后台 / 正在写修改意见 / 弹层开着时跳过
  setInterval(function () {
    var typing = document.activeElement && /TEXTAREA|INPUT/.test(document.activeElement.tagName);
    var playing = Array.prototype.some.call(document.querySelectorAll('video'), function (v) { return !v.paused; });
    if (shotId && !busy && !document.hidden && !typing && !playing && !document.querySelector('.layer, .lightbox')) loadShot();
  }, 15000);

  NS.bindKeyChip();
  NS.initTheme();
  if (!shotId) {
    $('#error').textContent = '地址里少了 ?shot=，请从总览页点一镜进来。';
    $('#error').hidden = false;
  } else {
    $('#shot-title').textContent = shotId;
    loadShot().then(function () {
      var h = window.location.hash;
      if (h === '#trigger') $('#adult-alert').scrollIntoView({ block: 'center' });
      var m = /^#tab=(\w+)$/.exec(h);                     // 深链：#tab=versions / jobs / detail
      if (m && $('#tabs button[data-tab="' + m[1] + '"]')) $('#tabs button[data-tab="' + m[1] + '"]').click();
    });
  }
})();
