# P2 N5：审片页重出 / 重派按钮（审查 3 §3）

- 审片页新增“重出”区：先选步（首帧 / 尾帧 / 视频），修改意见可选填，再点“重出这一步”，调用 `POST /api/shots/{id}/jobs`。
  - 写了修改意见：`mode=edit`，只对首 / 尾帧有效；视频不收修改意见，前端直接拦下。
  - 没写：`mode=regen`。
- 成人镜新起的行照样等触发（服务端 `adult=is_adult` 不变）。不用再靠 curl。
- 测试 `test_n5_redo_button_wired`（按钮存在、调用派单接口）。接口本身原来就有测试 `test_dispatch_endpoint`。全量测试见 `n5_pytest.txt`。浏览器真点仍需项目方。
