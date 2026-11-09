# P3：“没有可派的变体”线上路径也带原因（审查 3 §4；ep05-S2 / S12 / S13）

- 原来：`841156c` 只修了编译路径（build_jobs 给 0 个变体时）。线上 ep05 那三镜走的是 `pick_variant() is None` 分支，原因从变体自身的 status / reason 字段拼出来，字段为空时报错就是“没有可派的变体：”后面什么都没有。
- 现在：`step_video.no_variant_reason()` 保证原因不为空，按情况写出：
  - 0 个变体，并附 ladder；
  - 每个变体的状态与原因（没写原因时写缺什么）；
  - 超长、只能派长道的；
  - 阶梯里 ready 的有哪些；
  - 本道是哪一道。
- 测试：`test_no_variant_reason_never_empty`。全量测试 **83 passed**（`p3_novariant_pytest.txt`）。
- 真正的根因（S2 / S12 / S13 为什么 0 个变体）要部署后重派那三镜，看新报错。ep05 停跑到 P0 关闭为止，所以先不重派。
