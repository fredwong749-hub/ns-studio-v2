/* NS2 审片定稿页：先调 /api/overview 找镜，候选详情再试 /api/jobs?shot=。 */
(function () {
  'use strict';

  var DEFAULT_RUN = 'series10_v2';
  var params = new URLSearchParams(window.location.search);
  var shotId = params.get('shot') || '';
  var run = params.get('run') || DEFAULT_RUN;

  var titleEl = document.getElementById('shot-title');
  var detailEl = document.getElementById('shot-detail');
  var currentEl = document.getElementById('current');
  var candidatesEl = document.getElementById('candidates');
  var errorEl = document.getElementById('error');
  var messageEl = document.getElementById('message');
  var backLink = document.getElementById('back-link');

  var shot = null;      // overview 里该镜的对象
  var episode = null;   // 该镜所属集
  var jobRows = [];     // /api/jobs?shot= 返回的行（候选详情来源）

  backLink.href = 'index.html?run=' + encodeURIComponent(run);

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function showError(text) {
    errorEl.textContent = text;
    errorEl.hidden = false;
  }

  function clearError() {
    errorEl.textContent = '';
    errorEl.hidden = true;
  }

  function showMessage(text, isErr) {
    messageEl.textContent = text;
    messageEl.className = isErr ? 'message err' : 'message';
    messageEl.hidden = false;
  }

  function clearMessage() {
    messageEl.textContent = '';
    messageEl.hidden = true;
  }

  function badge(state) {
    if (!state) return '<span class="badge">–</span>';
    return '<span class="badge ' + esc(state) + '">' + esc(state) + '</span>';
  }

  function stageLine(label, stage) {
    if (!stage) return '<dt>' + esc(label) + '</dt><dd>–</dd>';
    var parts = [badge(stage.state)];
    if (stage.n !== null && stage.n !== undefined) parts.push('当前版 #' + esc(stage.n));
    if (stage.url) parts.push('<a href="' + esc(stage.url) + '">打开文件</a>');
    if (stage.progress !== null && stage.progress !== undefined) {
      parts.push(Math.round(stage.progress * 100) + '%');
    }
    if (stage.eta) parts.push('预计 ' + esc(stage.eta));
    return '<dt>' + esc(label) + '</dt><dd>' + parts.join(' ') + '</dd>';
  }

  function render() {
    if (!shot) return;
    titleEl.textContent = '审片定稿 · ' + shotId;
    var stale = (shot.stale && shot.stale.length) ? esc(shot.stale.join('、')) : '无';
    // stale_global 只作独立字段显示，不进 stale。
    var staleGlobal = (shot.stale_global && shot.stale_global.length)
      ? esc(shot.stale_global.join('、')) : '无';
    var missing = (shot.missing && shot.missing.length) ? esc(shot.missing.join('、')) : '无';
    var note = '';
    if (shot.note) {
      note = '<dt>备注</dt><dd>' + esc(shot.note) + '</dd>';
    } else if (shot.video && shot.video.state === 'todo' &&
        shot.missing && shot.missing.indexOf('video') !== -1) {
      note = '<dt>备注</dt><dd>片子还没拉到本机</dd>';
    }
    detailEl.innerHTML = '<dl class="kv">' +
      '<dt>镜号</dt><dd>' + esc(shot.id) + '</dd>' +
      '<dt>所属集</dt><dd>' + (episode ? '第 ' + esc(episode.ep) + ' 集' +
        (episode.title ? ' · ' + esc(episode.title) : '') : '–') + '</dd>' +
      stageLine('首帧', shot.first) +
      stageLine('尾帧', shot.last) +
      stageLine('视频', shot.video) +
      '<dt>审片状态</dt><dd>' + esc(shot.review || '未审') + '</dd>' +
      '<dt>过期（本镜输入变化）</dt><dd>' + stale + '</dd>' +
      '<dt>全局过期（仅提示）</dt><dd>' + staleGlobal + '</dd>' +
      '<dt>缺失</dt><dd>' + missing + '</dd>' +
      '<dt>候选数</dt><dd>' + esc(shot.candidates === null || shot.candidates === undefined
        ? '–' : shot.candidates) + '</dd>' +
      note +
      '</dl>';

    currentEl.innerHTML = '<dl class="kv">' +
      stageLine('首帧当前版', shot.first) +
      stageLine('尾帧当前版', shot.last) +
      stageLine('视频当前版', shot.video) +
      '</dl>';

    renderCandidates();
  }

  function renderCandidates() {
    // 候选详情来自 /api/jobs 的行：每行 result 含 take n、文件、qc 分数。
    if (!jobRows.length) {
      var count = (shot && shot.candidates !== null && shot.candidates !== undefined)
        ? shot.candidates : 0;
      candidatesEl.innerHTML = '<p class="muted">候选共 ' + esc(count) + ' 版；' +
        'jobs 接口未返回明细（可能后端未提供 shot 筛选），请以总览页的候选数为准。</p>';
      return;
    }
    candidatesEl.innerHTML = jobRows.map(function (job) {
      var r = job.result || {};
      var takeN = (r.take_n !== undefined) ? r.take_n : (r.take !== undefined ? r.take : '–');
      var file = r.file || r.path || r.output || '–';
      var qc = r.qc;
      var qcText = (qc === null || qc === undefined) ? '–'
        : (typeof qc === 'object' ? JSON.stringify(qc) : String(qc));
      return '<div class="candidate">' +
        '<div><b>' + esc(job.step || job.stage || '任务') + '</b> ' +
        badge(job.state) +
        ' <span class="muted">job #' + esc(job.id) + '</span></div>' +
        '<div>take n：' + esc(takeN) + '</div>' +
        '<div class="file">文件：' + esc(file) + '</div>' +
        '<div>qc 分数：' + esc(qcText) + '</div>' +
        (job.error ? '<div class="note">错误：' + esc(job.error) + '</div>' : '') +
        '</div>';
    }).join('');
  }

  function loadShot() {
    clearError();
    clearMessage();
    return fetch('/api/overview?run=' + encodeURIComponent(run))
      .then(function (resp) {
        if (resp === null) return null;
        if (!resp.ok) {
          return resp.text().then(function (t) {
            throw new Error('总览接口返回 ' + resp.status + '：' + (t || resp.statusText));
          });
        }
        return resp.json();
      })
      .then(function (data) {
        (data.episodes || []).forEach(function (ep) {
          (ep.shots || []).forEach(function (s) {
            if (s.id === shotId) { shot = s; episode = ep; }
          });
        });
        if (!shot) throw new Error('总览中未找到镜 ' + shotId + '（run=' + run + '）');
        render();
        // 再试 jobs 接口拿候选明细；失败不影响主展示。
        return fetch('/api/jobs?shot=' + encodeURIComponent(shotId) +
          '&run=' + encodeURIComponent(run));
      })
      .then(function (resp) {
        if (!resp.ok) return null;
        return resp.json();
      })
      .then(function (jobsData) {
        if (!jobsData) return;
        var rows = Array.isArray(jobsData) ? jobsData : (jobsData.jobs || []);
        jobRows = rows.filter(function (j) {
          return !j.shot || j.shot === shotId;
        });
        renderCandidates();
      })
      .catch(function (err) {
        showError('加载失败：' + err.message);
        detailEl.innerHTML = '<p class="muted">未能加载该镜详情。</p>';
      });
  }

  // B1：写操作要 X-NS2-Key。密钥只在服务器 /root/work/fredw/ns2/secret.key，由项目方自己粘贴一次，存在本浏览器 localStorage。
  var KEY_NAME = 'ns2_key';
  function getKey(forcePrompt) {
    var k = null;
    try { k = window.localStorage.getItem(KEY_NAME); } catch (e) { k = null; }
    if (!k || forcePrompt) {
      k = window.prompt('写操作需要密钥：粘贴那一长串（64 位字母数字），只要粘一次，以后这台电脑都记住（总览页顶上也能粘）：');
      if (k) { k = k.trim(); try { window.localStorage.setItem(KEY_NAME, k); } catch (e) { /* 存不了就只用这一次 */ } }
    }
    return k;
  }

  function writeOp(label, url, options, retried) {
    clearMessage();
    clearError();
    var key = getKey(false);
    if (!key) { showMessage(label + '已取消：没有密钥。', true); return Promise.resolve(); }
    options = options || {};
    options.headers = Object.assign({}, options.headers || {}, { 'X-NS2-Key': key });
    return fetch(url, options)
      .then(function (resp) {
        if (resp.status === 401 && !retried) {      // 密钥错 / 换过 → 重新问一次
          try { window.localStorage.removeItem(KEY_NAME); } catch (e) { /* ignore */ }
          if (getKey(true)) return { retry: true };
        }
        return resp;
      })
      .then(function (resp) {
        if (resp && resp.retry) return writeOp(label, url, options, true).then(function () { return { done: true }; });
        return resp;
      })
      .then(function (resp) {
        if (resp && resp.done) return null;
        return resp;
      })
      .then(function (resp) {
        if (resp === null) return null;
        if (!resp.ok) {
          return resp.text().then(function (t) {
            throw new Error(label + '失败（' + resp.status + '）：' + (t || resp.statusText));
          });
        }
        return resp.text();
      })
      .then(function (r) {
        if (r === null) return;
        showMessage(label + '成功，正在刷新数据……', false);
        return loadShot();
      })
      .catch(function (err) {
        // 失败时把后端返回的错误原文显示出来。
        showMessage(err.message, true);
      });
  }

  function epNumber() {
    if (episode && episode.ep !== undefined && episode.ep !== null) return episode.ep;
    var m = /^ep(\d+)-/i.exec(shotId);
    return m ? parseInt(m[1], 10) : null;
  }

  document.getElementById('btn-pass').addEventListener('click', function () {
    writeOp('定稿通过', '/api/shots/' + encodeURIComponent(shotId) + '/review', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: run, state: 'passed' })
    });
  });

  document.getElementById('btn-reject').addEventListener('click', function () {
    var reason = window.prompt('驳回原因（必填）：');
    if (reason === null) return;
    reason = reason.trim();
    if (!reason) {
      showMessage('驳回已取消：原因不能为空。', true);
      return;
    }
    writeOp('驳回', '/api/shots/' + encodeURIComponent(shotId) + '/review', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: run, state: 'rejected', reason: reason })
    });
  });

  document.getElementById('btn-cancel').addEventListener('click', function () {
    if (!window.confirm('确定取消该镜的排队任务吗？在途任务不会被取消。')) return;
    writeOp('取消排队任务', '/api/shots/' + encodeURIComponent(shotId) + '/cancel?run=' + encodeURIComponent(run), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });
  });

  document.getElementById('btn-assemble').addEventListener('click', function () {
    var ep = epNumber();
    if (ep === null) {
      showMessage('触发成片失败：无法从镜号解析集号。', true);
      return;
    }
    if (!window.confirm('确定触发第 ' + ep + ' 集的成片吗？')) return;
    writeOp('触发成片', '/api/episodes/' + encodeURIComponent(ep) + '/assemble', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: run, force: true })
    });
  });

  document.getElementById('btn-adult').addEventListener('click', function () {
    if (!window.confirm('确定触发成人镜 ' + shotId + ' 吗？触发后本镜的出图 / 出片任务才会被认领（L-128，只由项目方操作）。')) return;
    // B2：成人镜触发走专门接口 POST /api/shots/{id}/trigger（服务端记 triggers 表、放行本镜排队行）；定稿不再顺带放行
    writeOp('触发成人镜', '/api/shots/' + encodeURIComponent(shotId) + '/trigger', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: run, by: 'owner' })
    });
  });

  if (!shotId) {
    showError('缺少 ?shot= 参数，无法加载镜详情。');
    detailEl.innerHTML = '<p class="muted">请从总览页点击一镜进入。</p>';
  } else {
    titleEl.textContent = '审片定稿 · ' + shotId;
    loadShot();
  }
})();
