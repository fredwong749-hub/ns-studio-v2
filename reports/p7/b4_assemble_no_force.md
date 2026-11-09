# P0 堵点 4：成片默认不带 force（审查 3 §1 堵点 4）

## 改了什么
- `frontend/review.js`：“触发成片”只发 `{run_id}`，不再写死 `force: true`。签名是本集每镜最新视频行，输入没变时返回已有那一行，不出新版本、不起 GPU。
- 新增“强制重出成片”按钮（红色，确认文案写明“输入没变也会出新一版、占 GPU”）。只有点这个按钮才发 `force: true`。后端的 force 路径不变。

## 测试
- `test_assemble_default_idempotent_and_frontend_no_force`：
  - 同输入点两次，第二次返回 `created=False`，是同一行。
  - 带 force 才新出一行。
  - 前端源码默认请求体不含 `force: true`，“触发成片”按钮绑定的是 `assemble(false)`。
- 全量测试：**69 passed**（`b4_pytest.txt`）。浏览器真点仍需项目方。
