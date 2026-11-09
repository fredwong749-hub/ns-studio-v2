# 恢复 ep05：尽力版接线补全（2026-11-09 21:2x–21:36 JST）

## 经过
1. `promote_best_effort --dry` → 10 帧（7 首帧、3 尾帧）；正式跑 10/10 promoted，审计 logs/promote_best_effort.jsonl。
2. 21 行 blocked requeue + series10_v2 producing（21:26）。**20 秒内 21 行全部又 blocked**，没有出图出片：
   - 7 个尾帧：旧 producer 尾帧 preflight 只认首帧 `postflight.ok`（“首帧没有过契约的当前版本”），不认 best_effort 标记。
   - 视频（S2/S3/S6/S9/S12/S13）：旧 build_jobs 里 sop_gate“视频”闸门要“导入闸门 autoflow 记录 + 提示词探针证据”——ep05 是 ns2 导入的，没有这两本旧账 → 全集 0 变体（之前 S2/S12/S13“没有可派的变体、原因空”的根因）。
   - 结论：promote_best 只做了“设当前 + 打标记 + auto_current 不撤”，下游读 ok 的三处没接上 —— 是 a8f3c5e 的漏接，不是新规则。
## 修（提交本报告同一提交）
- `ns2/best_effort_view.py`：带标记且仍是当前版的那一版 → 读视图 `ok + review_needed`（旧系统 L-183 / L-196 同约定），原结果留 orig_ok / orig_blocking；只在内存，不写回 takes.json。
- `ns2/legacy_frame.py`：尾帧作业时只对本镜那一张带标记的当前首帧放行 preflight（PR.take_pf 局部包一层；不全局改 active_take——它在 producer 里有 15+ 处，含“跳过重出”判定）。
- `ns2/legacy_build_jobs.py` + `step_video.compile_payload` 改走它：build_jobs 读 takes.json 过读视图；尽力版不再被 Q9 / anomaly 单独挡；sop_gate 视频闸门放行（ns2 队列前置 FRAME_PASSED 代替；首尾帧步本就不走 sop_gate）。
- 测试 5 个（tests/test_best_effort_view.py），全量 97 passed（`resume_ep05_best_effort_view_pytest.txt`）。
## 部署后
- 两台 21:34–21:35 部署；gpu33 手跑一次新入口编译：ep05 13 镜 12 镜有变体（S1/S4/S5/S8/S10/S11 各 7、S2/S3/S6/S9/S12/S13 各 5）。
- **S7 仍 0 变体**：`master_rejects first:ep05-S7 2026-11-02T01:36:38 未 cleared`（旧系统驳回记录，早于 v2 ep05），不擅自清，等项目方。
- 21:36 再 requeue 21 行 → 21:36:41 gpu33 领 ep05-S1 尾帧、ep05-S2 视频，真跑。
