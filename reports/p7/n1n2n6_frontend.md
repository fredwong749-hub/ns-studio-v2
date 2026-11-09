# P2 前端：N1 / N2 / N6（审查 3 §3）

- **N1**：`/api/overview` 的 `totals` 加上 `script: [剧本已导入的集, 全部集]`。总览页“剧本”一栏不再永远显示“–”。
- **N2**：审片页候选明细改读视频步的真实字段 `clip` / `score`（原来只读 `file|path|output` 和 `qc`，永远是“–”）。旧字段留作回退。
- **N6**：审片页加 15 秒轮询，和总览页一样。写操作进行中、或页面在后台时跳过。
- 测试 `test_n1_n2_n6_frontend_fields`。全量测试 **86 passed**（`n1n2n6_pytest.txt`）。`node --check` 通过。
- **仍开着**：
  - N5“重渲 / 重派按钮”和总览口径四行（stale、本机文件存在检查、assembled 规则、film.kind）下一轮做。
  - 浏览器真点要项目方来。
