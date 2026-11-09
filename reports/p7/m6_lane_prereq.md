# P1 M6：长镜头入队锁道 + 前置帧失败时视频行自动改 blocked（审查 3 §2 M6 PARTIAL → 关）

- **入队锁道**：`db.enqueue` 对视频行检查一遍：阶梯里所有 ready 的变体都超过 260 帧时，入队就设 `want_lane=gpu36:2`，别的道不会再白领一次。没带变体（到运行时才编译）的行仍走原来的运行时改派（infra 加 want_lane）。`db.LONG_*` 与 `step_video.LONG_*` 两边一致，有断言。
- **前置帧失败**：首帧 / 尾帧 finish 成 `failed` / `blocked` 时，本镜排队中的视频行改成 `blocked`，error 写“前置不过：first blocked：<原因>”，记 `prereq_blocked` 事件。视频行不会再永远停在 queued。
- **前置恢复**：同一步后来 finish 成 done / na / best_effort，因它被挡的视频行自动放回 queued，记 requeue 事件 `by=prereq_recovered`。
- 两段都在 `finish()` 的同一个事务里（M3）。
- 测试 `test_m6_long_only_locks_lane_and_prereq_blocks_video`。全量测试 **88 passed**（`m6_pytest.txt`）。
