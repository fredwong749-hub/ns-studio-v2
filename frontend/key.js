// 审查 3 B1：浏览器里的密钥统一由这里存取。存 {k, at}，90 天到期后要重新粘（不再“永久记住”）；两页都有清除按钮。
// 服务端另有轮换 / 到期（ns2/auth.py：密钥文件每行一把，可带到期日）。
(function () {
  var NAME = 'ns2_key', TTL_MS = 90 * 24 * 3600 * 1000;
  function now() { return new Date().getTime(); }
  window.NS2Key = {
    TTL_DAYS: 90,
    get: function () {
      var raw = null;
      try { raw = window.localStorage.getItem(NAME); } catch (e) { return null; }
      if (!raw) return null;
      var o = null;
      try { o = JSON.parse(raw); } catch (e) { o = null; }
      if (!o || typeof o !== 'object' || !o.k) { o = { k: raw, at: now() }; this.set(o.k); }   // 旧格式（纯字符串）：从现在起算 90 天
      if (now() - (o.at || 0) > TTL_MS) { this.clear(); return null; }
      return o.k;
    },
    set: function (k) {
      try { window.localStorage.setItem(NAME, JSON.stringify({ k: k, at: now() })); return true; } catch (e) { return false; }
    },
    clear: function () { try { window.localStorage.removeItem(NAME); } catch (e) { /* ignore */ } }
  };
})();
