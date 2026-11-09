# P1 M8：enqueue_video 一镜坏不连坐（审查 3 §2 M8 PARTIAL → 关）

- `build()` 的异常捕获范围加上 `AttributeError` / `TypeError`：jobs.json 结构畸形时也只跳过这一镜。
- `post()`（HTTP 入队）挪进逐镜的 try（新函数 `post_rows`）：API 中途断了只记这一镜 `POST_FAIL`，后面的镜照常尝试。最后返回码为 1，并列出跳过的镜。
- 测试 `test_m8_post_failure_and_malformed_json_only_skip_that_shot`：S1 结构畸形、S2 入队失败，S3 照常入队。全量测试 **84 passed**（`m8_pytest.txt`）。
