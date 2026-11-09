# P1 M7：种子账本（审查 3 §2 M7；收尾第 6 项）

- `step_video.pick_seed()`：每个视频机按镜记账，账本在 `data/pipeline/runs/<run>/video/ns2_seed_ledger.json`，格式 `{镜: [用过的种子]}`，提交前先写（临时文件再改名）。
- 规则：想用的种子（变体自带的，或 trial 的 fresh 种子）本镜用过了，就换一个新的随机种子。所以**正常重试和 infra 重试都不会复用种子**。之前只有 trial 路径换种子。
- 图里没有 `RandomNoise` 节点时原样返回、不记账（`submit` 也换不了）。
- clip 文件名 `…_s<seed>` 和 take 的 `seed` 字段都用实际用的那个种子。
- 测试 `test_m7_seed_ledger_never_reuses`。全量测试 **87 passed**（`m7_pytest.txt`）。
- 局限：账本按机器分别记（视频分区按集固定在一台机器，同一镜不会在两台机器上出视频）；R1 搬进 gpu33 数据库时一起并入。
