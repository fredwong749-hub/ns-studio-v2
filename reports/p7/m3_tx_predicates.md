# P1 M3：三处 check-then-act 加谓词 + finish 单事务（审查 3 §2 M3）

- `db.tx(c)`：`BEGIN IMMEDIATE … COMMIT`，出异常就 `ROLLBACK`；已经在事务里就并入外层。
- `finish()` 整个包进一个事务，主 UPDATE、事件、成人挡、资产后重派、成片入队要么都写进去、要么都不写。中途崩了，行保持 running，worker 的 `finish_durable` 重报时还能收。
- 三处谓词：
  - 成片替换：`UPDATE … WHERE id=? AND state='queued'`，按 rowcount 记事件。
  - `cancel()`：同上，只返回真取消掉的。
  - `trigger()`：上一轮堵点 3 已加。
- 测试 `test_m3_finish_is_atomic_and_cancel_predicate`：
  - 用 monkeypatch 让成片入队抛错，主 UPDATE 回滚，行仍是 running、连接不残留事务；恢复后重报成功，只有一行 film。
  - 已被领走的行 cancel 不动。
  - 说明：这条只覆盖单连接下的谓词；双连接竞态沿用 `test_m3_predicates_without_api_lock` 的做法。
- 全量测试：**77 passed**（`m3_pytest.txt`）。
