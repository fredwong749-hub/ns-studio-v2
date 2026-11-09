# P1 M13（部分）：force 重派加心跳围栏（审查 3 §2 M13）

## 已做
- **force 重派**：`db.requeue(force=True)` 收 running 行的前提是心跳已经停了 `FORCE_STALE_S = 5 分钟` 以上；worker 还在心跳就拒绝（API 回 409）。原来只靠 docstring 一句“只在确认其 worker 已停时用”。测试 `test_m13_force_requeue_fenced_by_heartbeat`。
- **回收退避**：在 B3 那轮做了，回收后 120 s 内不许再领。
- **infra 退避**：P1 r12 已做。

## 还开着（明说，不标完成）
- **retry（内容没过）重试无退避**：保持现状，属于有意设计。内容没过换种子 / 换阶梯立即重来是最快路径，不会和 infra 一样连续霸占队首。
- **schema CHECK / 外键**：在线 queue.db 加 CHECK 要重建表，需要停机迁移。放到 R1（takes 进库）同一次迁移里做，不单独动 live 库（红线：不直接改 live 的 queue.db 做实验）。
- **events 归档**、**overview 持锁读全表（limit 1 000 000）**：同样放到 R1。现在 series10 一共几百行，对性能没有影响。

全量测试 **92 passed**（`m13_pytest.txt`）。
