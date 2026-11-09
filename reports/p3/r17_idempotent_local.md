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

## 还缺（已于下方 live 部分补齐）
- **live 重跑**：在 gpu33 对 ep05 同剧本再跑一次 `enqueue_episode`，记录重跑前后行数和 takes.json `seq` 前后对照。要等 SSH 恢复。（本机没有密钥，也不该经隧道带密钥写 live。）
- 本机证据只证明队列层幂等，覆盖不了“出图 / 出片层同输入不出新 take”，后者要 live 的 takes seq 对照。

## live 证据（2026-11-09 21:0x JST，gpu33，项目方批“都做”）
- 输入：从 queue.db 第 238 行（ep05 script 行）payload 原样取出 md_text（82131 字符）写成文件，`python -m ns2.enqueue_episode --run series10_v2 --ep 5 --md …` 再跑一次。series10_v2 当时 paused。
- 结果：`script gpu33 238 existing`、`script gpu36 239 existing`。
- 前后对照（原始数据 `r17_idempotent_live.txt`）：ep05 行数 66 → 66，ep05 max id 304 → 304，全库 299 行 → 299；ep05 13 个 takes.json 的 sha256 / takes 数 / 最大 n / active **前后完全一致**（`takes identical: True`）。
- 结论：队列层和 takes 层“同输入 0 新版本”live 关闭。出图 / 出片层的“同输入不重出”另由 enqueue 签名 + 已有 done 行保证，本次重入队没有产生任何新行，所以也没有新 take。
