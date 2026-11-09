# 两台都出视频（2026-11-09 22:3x JST，项目方：“同时把 gpu36 也用起来”）

## 为什么之前 gpu36 闲着
`migrate.PARTITION` 按集钉机器：ep05 出图、出片都在 gpu33（两道），gpu36 三道一直空（总览“主要慢在 GPU 调度”）。

## 改了什么（一件事：视频行两台谁空谁领）
- `migrate.video_host()`：新入队的视频行 `want_host=None`（任一台）；出图仍按集分区（takes 在出图机上）。剧本步入队（step_script.shot_rows）与人工派单（/api/shots/{id}/jobs）都走它。
- 已经排队的行：新接口 `POST /api/retarget`（要密钥，只动 queued / blocked，每行记 `retarget` 事件，可改回）。
- 视频机 ≠ 出图机时本来就有交接（step_video → handoff.sync_frames），补两样：**尽力版标记**、**尾帧 I2VA 兜底标记**一起带过来（否则 gpu36 按“没过契约”判、编 0 个变体）。
- 拼片：一集的片可能分在两台 → `step_film.fetch_remote` 经对方只读文件口取到 `data/episodes/_remote/<host>/…`（只读缓存，不改对方 takes）；取不到照旧占位、不挡整集。
- 测试：tests/test_both_hosts_video.py 4 个；两条旧断言（视频钉 gpu33）按新规则改；全量 103 passed（both_hosts_video_pytest.txt）。
