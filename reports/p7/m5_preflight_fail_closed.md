# P1 M5：视频 preflight 三处 fail-open 改成 fail-closed（审查 3 §2 M5）

| 原来 | 现在 |
|---|---|
| `comfy_loras()` 问不到返回 None 时，跳过 LoRA 检查 | 变体用了 LoRA、清单又问不到 → 抛 `PreflightInfra`，`run()` 记 **infra**（回队不计次，退避，连续 5 次转 blocked）。不用 LoRA 的变体不去问 |
| `_active_takes()` 读不到 takes.json 返回 `{}`，“首尾帧仍是当前版”和“尾帧不老于首帧”两项静默跳过 | 读不到就抛 `PreflightInfra`，记 infra |
| 上传缓存键只有 `(lane, rel, mtime, size)` | 键里加内容 sha256，内容变了就重传 |

- 测试：
  - `test_video_preflight` 补了三条：问不到 LoRA → infra；不用 LoRA 的变体不报 infra；takes.json 读不到 → infra。
  - 流程测试 `test_run_flow_with_fake_comfy`、`test_score_infra_error…` 补了本机首尾帧账本和 LoRA 清单。原来它们靠 fail-open 才通过，这正是这个漏洞。
  - 内容 hash 缓存在 `test_fake_comfy` 里覆盖。
- 全量测试：**82 passed**（`m5_pytest.txt`）。
