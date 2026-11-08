# r16 一键用这版 + 密钥一次保存（2026-11-09，项目方：逐镜点太累）

- 新接口 POST /api/reviews/bulk {run_id, shots, state:"passed"}：要密钥；只定最新视频行有片（done / best_effort）的镜，其他跳过并写原因；已 passed 不重写；同一张 reviews 表（final 口径不变）
- 新接口 POST /api/auth/check：前端保存密钥时核对
- 总览页：顶部密钥框（粘一次存 localStorage，审片页共用）；“全部用这版（N 镜）”按钮（随按集筛选）；每集“本集全部用这版”；每行“用这版”
- 驳回仍逐镜写原因（批量只收 passed）
- 测试：test_review_bulk；全量 54 passed（r16_pytest.txt）
