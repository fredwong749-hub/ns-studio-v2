# P3 r15：总览“缺首尾帧”判定 + 预计完成时间

- 首帧已卡在等人（best_effort / blocked / failed）时，排着的尾帧永远领不到 → 不再算“系统在办”，视频记 need_frames（原来显示 todo）
- 预计完成时间（eta_all）不再计 need_frames / blocked（等人的镜给不出时间）
- 真机 smoke_v2 ep01：S1 / S3 → need_frames，S2 → pending_review；need_frames 2、eta_all 空
- 测试：r15_pytest.txt
