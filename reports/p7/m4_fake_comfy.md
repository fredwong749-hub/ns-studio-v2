# P1 M4：真 HTTP 假 ComfyUI 测试（审查 3 §2 M4；收尾第 5 项）

`tests/test_fake_comfy.py` 起一个本地 `ThreadingHTTPServer`，实现 ComfyUI 的 `/upload/image`、`/prompt`、`/history/{id}`、`/queue`（GET / POST delete）和 `/interrupt`。`step_video` 的 `submit` / `wait` / `upload` / `cancel` **不 monkeypatch**，走的是真实请求代码，只把 PORTS / OUTDIR / ROOT 指向假服务。

| 用例 | 覆盖 |
|---|---|
| `test_submit_upload_wait_real_http` | `@` 输入真上传，并换成 Comfy 文件名；换种子写进 RandomNoise；SaveVideo 前缀；同指纹不重传，文件变了重传（M5 缓存键）；wait 解析出 mp4 路径和 gpu_s |
| `test_wait_error_branches_cancel_real_http` ×2 | comfy_error / no_output：infra，并经真 `/queue` delete 撤掉 |
| `test_wait_timeout_cancels_and_interrupts_running` | 超时：delete，且正在跑的是自己的 prompt 时 interrupt |
| `test_cancel_not_running_only_deletes` | 正在跑的是别人的 prompt 时不打断 |

全量测试：**82 passed**（`m4_pytest.txt`）。
