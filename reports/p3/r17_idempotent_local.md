# r17：“同输入 0 新版本”证据（审查 3 §1 堵点 5）

## 先更正
`reports/closing_20261109.md` 原来把这项标成 ✅（“同剧本再入队 217 / 218 existing、行数不变”），但 r14 证据里根本没有这次重入队（`r14_smoke_ep01_jobs.jsonl` 只有 19 行，0 次 existing）。**这是假 ✅，已改成 ⚠️ 未证实**，并指向本报告。

## 本机证据（2026-11-09，真代码路径，临时库）
- 跑法：`scratchpad/idem/run.py` 起一个真的 `uvicorn ns2.api:app`（临时 queue.db、测试密钥），然后：
  - 用 CLI 跑 `python -m ns2.enqueue_episode --run series10_v2 --ep 5 --md P4_ep05.md --version v3`；
  - 再用 `ns2.step_script.shot_rows` 经 `POST /api/jobs` 把 13 镜的镜头行入队；
  - 同一份剧本跑两遍。
- 结果（原始输出 `r17_idempotent_local.txt`）：

| | script 行 | 镜头行（13 × 4 + assets 1） | 库里总行数 |
|---|---|---|---|
| 第 1 次 | 2 new | 53 new | 55 |
| 第 2 次 | 2 existing（id 1、2） | 0 new / 53 existing | 55（不变） |

## 还缺（不标 ✅）
- **live 重跑**：在 gpu33 对 ep05 同剧本再跑一次 `enqueue_episode`，记录重跑前后行数和 takes.json `seq` 前后对照。要等 SSH 恢复。（本机没有密钥，也不该经隧道带密钥写 live。）
- 本机证据只证明队列层幂等，覆盖不了“出图 / 出片层同输入不出新 take”，后者要 live 的 takes seq 对照。
