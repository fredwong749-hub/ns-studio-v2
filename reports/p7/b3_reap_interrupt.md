# P0 B3：回收时真正撤掉 ComfyUI 上的 prompt（审查 3 §1 B3）

## 改了什么
1. **prompt_id 在 wait 之前落盘并登记**（`step_video.run`）：
   - `submit` 一返回就调 `worker.comfy_mark(live=True)`。
   - 先写本机 `pending/comfy/<pid>.json`，再 `POST /api/worker/comfy`。API 记进新表 `comfy_prompts`，只认本机正在跑（running 且 host 对得上）的活。
   - `wait` 结束后（`finally`）删文件、报 done。
2. **回收（`db.reap`）**：
   - 被回收的活登记过的 prompt 标成 `cancel_req`。
   - 本机 worker 下次整机心跳时，回包里会带上要撤的 prompt（只发一次，标 `cancel_sent`）。worker 调 `step_video.cancel`：先 `/queue` 删除，正在跑的再 `/interrupt`，撤完报 done。
   - 回收后的活设 `ready_at = now + 120 s`，退避期内不许再领。这是 M13 的子项，防止旧 prompt 还在跑时新认领双跑。
3. **worker 死掉再重启**：`cancel_leftover_prompts` 把 `pending/comfy/` 里剩下的 prompt 全部撤掉。worker 进程死了也撤得掉，不依赖 API。
4. **`wait()` 的三个漏撤分支补上 `cancel()`**：`comfy_down`（宽限期满）、`comfy_error`、`no_output`。原来只有 timeout 和 HTTP 错误会撤。

## 测试
- 新测试：
  - `test_b3_reap_marks_comfy_prompt_for_cancel`：登记只认本机活；回收后标要撤；只有本机取得到、只发一次；能报 done；有退避。
  - `test_b3_worker_cancels_leftover_and_reaped_prompts`：重启撤剩下的，正常结束的不留；心跳回包要撤的也撤。
  - `test_b3_wait_cancels_on_error_branches`：三个分支都撤。
- 改的测试：`test_m3_predicates_without_api_lock` 改成先断言退避期内领不到，再模拟退避期过去后领取。
- 全量测试：**67 passed**（`b3_pytest.txt`）。

## 遗留（明说）
- 首帧 / 尾帧的 ComfyUI 提交在旧执行器 `legacy_frame` 里（旧 `FrameExec`），prompt_id 拿不到，这一轮**只覆盖视频步**。帧步有一个子进程超时（3600 s），但子进程被杀后 ComfyUI 侧不会被撤。R2 去旧代码时一起做（帧步换成 ns2 原生提交后接同一套）。
- 真机演练（kill worker → 回收 → ComfyUI 那边被中断）要等 SSH 恢复，部署后补到本报告。

## live 演练（2026-11-09 21:18–21:20 JST，项目方批“按改的做”）
脚本 `tools/evidence/b3_live_drill.sh`，原始输出 `b3_live_drill.txt`。用 smoke_v2 ep01-S2 视频重出（第 307 行）做真任务：
1. 派单 → smoke_v2 临时 producing → 10 s 内 gpu36:2 领走，comfy_prompts 登记 `2158777c… live` → **smoke_v2 立刻改回 paused**（暂停窗口 19 s）。
2. ComfyUI :8190 `queue_running` 里是这个 prompt。
3. kill gpu36 worker（只杀本目录 pid）→ 30 s 后 ComfyUI **仍在跑**（这就是 B3 原问题：worker 死了 GPU 还在烧）。
4. 回收：同一段 `D.reap` 代码、租约缩到 20 s（正常 45 min 等不到 6 min 的视频），先断言只有 307 一行心跳过期 → `reaped [307]`，行回 queued、`ready_at` 退避 120 s、prompt 标 `cancel_req`。此时 ComfyUI 仍在跑（worker 不在，没人撤）。
5. 重启 gpu36 worker → 启动时 `COMFY_CANCEL 307 2158777c… deleted,interrupted` → 25 s 后 ComfyUI 三个口 `queue_running` 全空，comfy_prompts 该行 `done`。
- 结论：**视频步 B3 live 关闭**（worker 死 → 回收 → 重启撤残留 → ComfyUI 被中断）。帧步旧执行器仍未覆盖（R2）。
- 副作用（如实）：暂停窗口里 gpu33 领走 smoke_v2 第 229 行（ep01-S3 尾帧）跑了一次，FL 不过 → 收成 best_effort；307 行留在 queued（smoke_v2 paused，不会再跑）。series10_v2 全程 paused。
