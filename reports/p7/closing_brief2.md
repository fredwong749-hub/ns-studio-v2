# 任务书第二版（ns2-auto-iterate-brief-2）收尾报告：本机部分（2026-11-09）

- 起点 HEAD `042dd21`（第三次审查，结论 STILL FIX-FIRST），到本报告共 25 个提交。每个提交都有对应报告和 pytest 记录（`reports/p7/`）。
- 测试：**92 passed / 0 failed**。全新 venv 按 README（`pip install -r requirements.txt` → `pytest tests/ -q`）跑，原始输出见 `closing_fresh_venv_pytest.txt`。
- **重要前提**：SSH 跳板从 11-09 约 11:00 JST 起两台都连不上（API 隧道的只读接口还通）。所以以下改动**全部只在本机测过，都还没部署到 gpu33 / gpu36**；凡是要 live 证据的项，都标成“待 SSH”，不标完成。

## 逐项

| 审查编号 | 结论 | 证据 |
|---|---|---|
| Pillow 进 requirements | 本机关闭（全新 venv 62/62） | p0_pillow.md |
| B1 文件口 / 伪造终态 / 密钥 | 代码关闭：白名单 403、live 入队拒终态和 result、导入口默认关、多密钥轮换 + 到期、浏览器 90 天到期 + 两页可清除。**finish 口仍收终态**（有 running + host 谓词保护） | b1_files_import_key.md |
| B3 回收 interrupt | 视频步代码关闭（落盘登记、回收撤销、重启撤残留、wait 三个分支也撤、回收退避）。**帧步旧执行器未覆盖**（R2）；**真机演练待 SSH** | b3_reap_interrupt.md |
| 堵点 3 成人触发 | 代码关闭（is_adult 校验、撤销、按钮 gating） | b_adult_trigger.md |
| 堵点 4 成片 force | 代码关闭 | b4_assemble_no_force.md |
| 堵点 5 同输入 0 新版本 | 假 ✅ 已更正为 ⚠️；本机证据齐。**live 重跑 + takes seq 对照待 SSH** | p3/r17_idempotent_local.md |
| promote_best ↔ auto_current | 代码关闭（规则 4、成人拒绝、promote_only 接上线、4 个测试）；范围 = 全部集（项目方 11-09） | promote_best_reconcile.md |
| N7 / N8 批量定稿 | 代码关闭 | n7n8_bulk_review.md |
| 审片史导出 | 代码关闭；**gpu33 定时任务安装待 SSH**（项目方已批） | review_export.md |
| M3 | 关闭（finish 单事务 + 谓词） | m3_tx_predicates.md |
| M4 | 关闭（真 HTTP 假 ComfyUI） | m4_fake_comfy.md |
| M5 | 关闭（preflight fail-closed + 内容 hash） | m5_preflight_fail_closed.md |
| M6 | 关闭（入队锁长道、前置失败 → 视频行 blocked / 恢复） | m6_lane_prereq.md |
| M7 | 关闭（种子账本） | m7_seed_ledger.md |
| M8 | 关闭 | m8_enqueue_isolation.md |
| M9 帧步 review.json | **不做，要项目方确认**：任务书要求“帧步补 review.json 检查”，但项目方 11-09 定 final 口径 = reviews 表、不写也不读 review.json（审查 §3 也认这条）。两者冲突，按红线“验收标准有问题时停下问”，没有自己改 | — |
| M10 | 部分：`set -eu` 已改并在本机跑过。**API 双绑、gpu36 真演练仍开** | m10_rollback_set_e.md |
| M13 | 部分：force 重派加围栏。schema CHECK / events 归档 / overview 分页放到 R1 迁移 | m13_force_fencing.md |
| P2 前端 N1 / N2 / N5 / N6 + 口径四行 | 代码关闭（stale 明确延期）；**浏览器真点要项目方** | n1n2n6_frontend.md、n5_redo_button.md、overview_rows.md |
| P3 stale-frame + handoff N2–N4 | 代码关闭（提交前复核出图机当前帧） | p3_handoff_stale_frame.md |
| P3 0 变体空原因（线上路径） | 代码关闭 | p3_no_variant_reason.md |
| film v1 错帧标注 | 关闭（smoke_v2 ep01 v1，总览 ⚠） | overview_rows.md |
| r14 / STATE 文档更正 | 关闭（23→37 分钟、GPU 14.9 作废、“新队列，执行层仍旧部署”、PLAN 接口文字、STATE P6 节） | git 2a4fa9b、e8ebf13 |
| 收尾第 10 项（series10 批量定稿） | 项目方 11-09 确认是本人在总览页点的。**具体时间和镜数待 SSH 后从 reviews 表核实**，核完再关 | STATE |

## SSH 恢复后按顺序做（脚本已备好）
1. 两台都部署（`scratchpad/deploy_both.sh`：上传核 sha → redeploy → 在服务器上 grep 新符号、核 worker 启动时间）。
2. 跑 `install_review_export_cron.sh`，安装审片导出定时任务。
3. live 证据：
   - ep05 同剧本重入队（行数 / takes seq 前后对照）→ 补到 r17；
   - kill worker → 回收 → 看 ComfyUI 被中断 → 补到 b3 报告；
   - 非成人镜 trigger 返回 400、`/files/…/queue.db` 返回 403。
4. 从 reviews 表核实 55 镜批量定稿的时间 / 镜数，关掉收尾第 10 项。
5. 以上齐了 = P0 关闭。之后才按任务书恢复 ep05：`promote_best_effort --dry` → 正式跑 → 重派 S2 / S12 / S13 → 量 R4 退出标准（首帧一次过率、尽力版比例）。

## 要项目方定 / 做的（任务书 §6 + 本轮新发现）
1. **M9 冲突**：帧步要不要读 review.json？（与“final = reviews 表、不写 review.json”的决定冲突）
2. **ep06 不适合做“全自动验收集”**：剧本写好了（16 镜 / 92 秒，机检 0 阻断），但这一集按 P1 是亲密集，导入器把整集 16/16 判成成人镜，每镜都要项目方触发。验收集要不要换成一集非成人的（比如 ep07），或者接受 ep06 等触发的时间不计？
3. ep03-S4 首帧复核 + 触发；浏览器真点总览 / 审片页；gpu36 回滚真演练（任务书 §6）。
4. API 双绑（10.0.0.33 + 127.0.0.1）要不要真做？现在笔记本隧道只打 10.0.0.33，没有实际需求。
