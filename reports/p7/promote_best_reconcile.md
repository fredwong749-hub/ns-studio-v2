# P0：promote_best 与 auto_current 对账（审查 3 §5b）

## 项目方决定（11-09，本轮问过）
“尽力版一律自动用”的范围是**全部集**，新集和 series10 ep01–04 都用同一规则。已写进 `docs/P6_PLAN.md` §4.4。

## 矛盾在哪
`auto_current` 规则 2 写的是“没过契约绝不设为当前；它若已是当前，就退回或清空”。`promote_best` 正好反过来，把没过契约的版本设为当前。下一次重打分时，`decide()` 走“清空”，会把已经自动用的尽力版悄悄撤掉。

## 选的方案：改规则，代码加标记（两边一致）
1. **规则文本**：`services/render_router/ns_render/auto_current.py` 新增第 4 条例外。仓库里这份是规则文本，服务器上执行的是旧部署目录里的那份，所以运行时由第 2 点补上。
2. **执行**：`ns2/legacy_rules._best_effort_rule()` 由 `apply()` 调用，帧步、分镜、资产三个执行器启动时都会走到。它包住 `AC.decide`：
   - 当前版是带 `best_effort[stage]` 标记的那一版、原规则要“清空” → 改成 keep，并写明原因。
   - 原规则要退回到一个过契约的版本 → 照常退回，好版本优先。
3. **标记**：`legacy_frame.promote_best` 提升后，在 takes.json 顶层写 `best_effort[stage] = {n, at, by, reason, blocking}`（经 `mutate_takes`）。这也是下游区分“尽力版来源”的依据（审查 §4 的“来源标记”，视频 payload 侧另一轮再接）。
4. **成人拒绝写进函数里**：`promote_best(..., adult, triggered)`，默认 `adult=True`（fail-closed）。成人镜未经项目方触发就返回 `refused: adult`。`step_frame` 按剧本导入稿传 `adult`（`step_video.shot_adult`，读不到当成人），触发与否看 `owner_triggered`。
5. **promote_only 接上线**（不留死代码）：新命令 `python -m ns2.promote_best_effort --run --ep [--dry]`：
   - 找每镜每步最新一行是 best_effort 的 first / last，走同一个 `promote_best`（同样的成人拒绝）。
   - 每镜写一行审计到 `logs/promote_best_effort.jsonl`；旧的 `current_changes.jsonl` 由 `set_active` 照记。
6. `step_frame.py` 头注释里过期的“不设当前，等人工”已更正。

## 测试（新文件 `tests/test_promote_best.py`，4 个）
- **选择顺序：** 不过项最少，同等取最新。
- **过滤与保护：** 坏图 / 驳回不选；项目方定稿锁不动；已有合格当前版不动。
- **成人：** 未触发拒绝，已触发照常；调用方不说时默认当成人。
- **标记：** 提升后标记写对。
- **规则 4：**
  - 带标记的当前版不被清空。
  - 没标记照原规则清空。
  - 当前版已经不是标记那版时不受保护。
  - 有过契约的版本照常退回。
- **CLI：** 只选最新一行是 best_effort 的；传 adult / triggered；审计结果带 job id。
- 全量测试：**73 passed**（`promote_pytest.txt`）。

## 待 SSH
- 部署后对 ep05 跑 `promote_best_effort --dry` → 正式跑，结果补到本报告。
