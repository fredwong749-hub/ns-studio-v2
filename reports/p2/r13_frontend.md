# P2 r13：前端 M2——ns2 同源总览 + 审片页（参考包为起点，按远程适配）

## 改了什么
- `frontend/`（参考包 5 个文件）由 ns2 API 同源提供：`/` → `/ui/index.html`（总览，15 秒轮询）、`/ui/review.html?shot=`（审片）。端口 8900，笔记本经隧道 127.0.0.1:18900
- 后端 `ns2/overview.py` 状态词对齐参考包（任务书“口径按参考包”）：视频 final / pending_review / best_effort / rendering / queued / need_frames / todo / failed，
  帧 ready / running / pending / best_effort / failed / fallback；**多一个 blocked**（缺料 / 成人镜待触发，带 note 原因）。驳回 → todo +“已驳回，等重出”；
  eta 给 ISO 时间 + progress。final 口径 = 队列 reviews 表 passed（项目方 11-09 定，不读 review.json）
- 前端适配：写操作带 `X-NS2-Key`（项目方在本浏览器粘贴一次，存 localStorage；401 自动重问）；成人触发改走 `POST /api/shots/{id}/trigger`；
  review / cancel / assemble 带 run；总览加“卡住等人 / 缺首尾帧”计数与筛选；“已定稿 / 待审片”筛选改按视频状态（参考包按帧 ready 判会把几乎所有镜算成待审片）

## 测试
51 passed（r13_pytest.txt）；新增总览口径（rejected → todo、need_frames、成人排队 → blocked 不算 queued）、前端同源可达 / 页面带密钥头且无 8800

## 真机自验（11-09 02:4x，经笔记本隧道 127.0.0.1:18900，日志 r13_ops.log）
- `/` 307 → `/ui/index.html`；index / review / app.js 200；`/api/overview?run=series10_v2` 200：final 0、待审 30、尽力 24、阻断 1（与 M2b 一致）
- 审片页五个写操作（测试 run `test-ui`，不碰 series10 的项目方决定）：匿名定稿 401；定稿 200 → final 1；驳回 200 → todo“已驳回，等重出”；
  成人触发 → 放行 job 212（无剧本的 run 当成人，入队即被挡）；取消 → []（212 已被 worker 立即领走，在途不取消，符合规则）；触发成片 200 → film 行 blocked“整集一条视频都没有”（测试 run 无真片，正确）
- 测试 run test-ui / test-p0r2 / test-drill 事后都已 paused
- **未做**：浏览器里真点一遍（Chrome 扩展未连上）；JS 只做了 node --check 语法检查 + 接口层验证
