# P0 B1：文件口白名单、live 与导入入队分开、密钥轮换 / 到期（审查 3 §1 B1）

## 改了什么
1. **:8901 文件口白名单**：`ns2/worker.py` 的 `serve_files` 和 API 的 `/files` 代理共用 `ns2/auth.py` 里的 `MEDIA_EXT`，只放行 png / jpg / jpeg / webp / mp4 / webm / mov / json。其他扩展名一律返回 403，`queue.db`、`secret.key`、`*.log` 都在内；持密钥也一样。
2. **live 与导入入队分开**：
   - `POST /api/jobs` 只收 `queued` / `blocked`，并且不收调用方自带的 `result`。伪造 `done`、`best_effort` 或带 `result` 的请求一律 403。
   - 迁移导入走新接口 `POST /api/import/jobs`。只有 API 以 `NS2_ALLOW_IMPORT=1` 启动时这个口才开，平时 403。
   - `import_state` 和 `enqueue_video` 按行自动选路径（`enqueue_video.job_path`）。
3. **密钥轮换和到期**：
   - 密钥文件改成每行一把，格式为 `<密钥> [到期日]`。过了到期日的行自动失效。
   - 轮换步骤：先在第一行加新密钥，各端换成新密钥，再删掉旧行。
   - 一把有效密钥都读不到时，全部拒绝（fail-closed 不变）。
4. **浏览器侧不再“永久记住”**：
   - 新增 `frontend/key.js`，总览页和审片页共用。密钥存成 `{k, at}`，**90 天后到期**，要重新粘。旧的纯字符串格式会从现在起算 90 天。
   - 审片页新增“清除本机密钥”按钮；总览页原来就有“更换”。

## 没做 / 遗留
- `POST /api/worker/finish` 仍然收任意终态：它是 worker 回报口，靠“running 且 host 对得上”的谓词护着，B3 那一轮一并复核。
- 文件口仍绑定内网 IP，没改成只绑 127.0.0.1：gpu36 视频机要经 :8901 从 gpu33 拉帧（handoff）。已改用白名单的办法。
- 90 天到期与项目方 11-09 说的“粘一次永久记住”不完全一致，是按审查要求“不得再扩大”做的折中，已写进 STATE。

## 验证
- 新测试：
  - `test_b1_file_port_whitelist_and_import_split`：持密钥取 `queue.db` / `secret.key` / `worker.log` 都是 403，`a.png` 是 200；live 入队伪造 `done` / `best_effort` / 带 `result` 都是 403；导入口默认 403，开 `NS2_ALLOW_IMPORT` 后才收。
  - `test_b1_key_rotation_and_expiry`：多密钥、到期、空文件全拒。
- 原来的 Range 测试改用 `.mp4`（原来用 `.bin`，正好证明了这个漏洞）。
- 全量测试：见 `b1_pytest.txt`。
- 前端：`node --check` 三个 js 都通过；浏览器真点仍需项目方。
