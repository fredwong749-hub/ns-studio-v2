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
