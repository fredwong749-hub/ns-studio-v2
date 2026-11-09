# P1 M10：rollback.sh 加 set -e（审查 3 §2 M10，部分）

- `set -u` 改成 `set -eu`：任何一步出错就停，不会带着半截状态继续切换。
- 可预期为空的查询都改成不会误杀：`pgrep … || true`、`readlink … || true`、`cat pid || true`。原来写成 `[ … ] && …` 的链改成 `if …; then …; fi`。
- 本机实跑：`status` 的返回码为 0（没有进程、没有 pid 文件时也能走完）；未知子命令返回 2。
- 测试 `test_m10_rollback_set_e_and_status_survives_empty_queries`。全量测试 **85 passed**（`m10_pytest.txt`）。

## 仍开着（明说）
- **API 双绑**：uvicorn 一个进程只能绑一个地址。现在只绑 10.0.0.33:8900（笔记本隧道也打到这个地址），127.0.0.1 没有需求。STATE 和文档已按“只绑内网口”口径写；要不要真双绑（起两个进程或改成 0.0.0.0 加防火墙），等项目方定，暂不改。
- **gpu36 真演练**：要在机器上做（任务书 §6.6），等 SSH 恢复、并由项目方安排。
