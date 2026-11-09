# P0 堵点 3：成人触发先验是否成人镜 + 撤销 + 前端只在成人镜上给按钮（审查 3 §1 堵点 3）

## 改了什么
1. `POST /api/shots/{id}/trigger`：先调 `is_adult(run, shot)`。按剧本导入稿不是成人镜就返回 400，不写 triggers 表。剧本里没有的镜仍按 fail-closed 当成人镜处理，允许项目方触发。
2. 新增撤销触发 `DELETE /api/shots/{id}/trigger?run_id=`（`db.untrigger`），要密钥：
   - 删掉 triggers 行。
   - 本镜排队中的出图 / 出片行重新挡住：`owner_trigger=1`，去掉 `payload.owner_triggered`。
   - 已在跑的不打断，返回 `running` 列表，并记 untrigger 事件。
3. `db.trigger` 的放行 UPDATE 加 `AND state='queued'` 谓词（M3 三处之一）。
4. `/api/overview` 每镜加上 `adult`（服务端按剧本判）和 `triggered`（triggers 表）。
5. 审片页：“触发成人镜”默认隐藏，只在 `adult` 为真且未触发时出现；已触发的显示“撤销成人镜触发”。

## 测试
- `test_adult_trigger_checks_is_adult_and_can_be_revoked`：
  - 非成人镜触发返回 400。
  - 触发后总览显示 adult / triggered。
  - 撤销后排队行重新被挡，worker 领不到；总览 triggered 变成 False。
  - 撤销不带密钥返回 401。
- 原有 B2 测试 `test_adult_gate_server_side` 照过。
- 全量测试：**68 passed**（`b_adult_pytest.txt`）。前端 `node --check` 通过；浏览器真点仍需项目方。

## 遗留
- 撤销不打断在跑的出图 / 出片，跑完的结果照记。要打断的话在审片页“取消排队任务”之后再人工处理。
