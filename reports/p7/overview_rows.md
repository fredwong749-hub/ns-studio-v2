# P2 总览口径四行 + 成片 v1 标注（审查 3 §3 / §4）

| 行 | 现在 |
|---|---|
| stale | **明确延期**：输入签名比对要等 R1 takes 进库后才有可靠来源。现在恒为空，前端不据此显示“过期”。overview 的 docstring 已写明 |
| 本机文件存在检查 | API `check_local_files`：片子在 API 所在机（gpu33）上、而文件丢了时，状态从 `final` 降成 `missing_file`，并写 note、调整计数（videos.final−1、missing_file+1、video_done−1）。别的机器的文件不核（没有便宜的办法，R1 并库后统一） |
| assembled | 最新 film done，且晚于本集所有视频行的落定时间、**也晚于最近一次审片**，且（本机的）mp4 存在 |
| film.kind | 去掉写死的 `"preview"`：本集每镜都 final、没有占位时为 `final`，否则 `preview` |
| 成片 v1 标注 | `FILM_WARN`：smoke_v2 ep01 v1“用了交接前的旧首帧（视频 job 226），以 v2 为准”，总览成片行显示 ⚠。审查里说的“ep01 v1 错帧”是 smoke_v2 的 ep01（r14），不是 series10 的 ep01 |

- 前端：成片行显示 warning 和“成片文件在本机找不到”；视频汇总多一个“文件丢了”计数（为 0 时不显示）。
- 测试 `test_overview_rows_assembled_kind_warning_and_missing`。全量测试 **90 passed**（`overview_rows_pytest.txt`）。
- 仍开着：N5 重渲 / 重派按钮；`eta_all` 算法（审查只指出，没给口径，暂不改）。
