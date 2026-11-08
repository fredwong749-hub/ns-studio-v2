# P1 r4：M2 回收线程不死

- `api.reap_once()`：一轮回收包 try/except，出错打 REAPER_ERR + 堆栈、线程继续；`/api/health` 加 `reaper.ok_age_s / err / reaped`，有错时 `ok=false`
- 测试 39 passed（r4_pytest.txt）；新增 `test_reaper_survives_errors`（出错 → health ok=false 带原因；下一轮恢复 → ok=true、计数）
- 真机（11-09 02:1x，gpu33）：部署 75 s 后 health `"ok":true`，`reaper {ok_age_s 36.9, err null, reaped 0}`（第一轮已成功跑过）
