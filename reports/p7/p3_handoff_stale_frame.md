# P3：视频交接 stale-frame 窗口 + 三个小项（审查 3 §4）

- **stale-frame 竞态**：出图机和视频机不是同一台时，提交 ComfyUI 之前调 `step_video.recheck_remote_frames()`，经 `handoff.remote_active()` 再问一次出图机当前的首 / 尾帧版本号。
  - 和本次变体绑定的 `first_take` / `last_take` 不同 → 返回 **infra** 回队，原因写“出图机的首帧当前版已从 vX 变成 vY（交接后又换了）”。下次领到先重新交接、重新编译，不拿旧帧出片。
  - 问不到出图机时也按 infra 处理，不放行。
  - 没绑定帧版本的（echo）或本机就是出图机，跳过。
- **N2**：出图机没有当前首帧时，本机 `active.image` 也去掉。原来只处理了 last。
- **N3**：本机已有同名帧文件时，用 Range 0-0 问出图机文件大小；对不上就抛 `HandoffConflict`（视频步记 blocked，不覆盖、不拿错图）。问不到大小时不挡。
- **N4**：`op_id` 加随机后缀，14→13→14 这样的回摆不会被重放保护吞掉（mutate 本身按内容幂等）。
- 测试 `test_p3_recheck_remote_frames_and_handoff_fixes`。原有的交接测试照过。全量测试 **89 passed**（`p3_handoff_pytest.txt`）。
