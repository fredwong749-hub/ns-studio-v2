# 任务书第二版：线上部署 + 只读 / 无副作用的 live 证据（2026-11-09 20:4x–20:5x JST）

## 连接
- 深圳跳板直连（`194relay`）仍然 banner 超时；改走项目自带的香港中转路线 `config/netfix/ssh_config` 的 `gpu33-hk` / `gpu36-hk`（NS-Final Studio 仓库，不在本公开仓库）。主机指纹按项目 known_hosts 严格校验通过。

## 部署
- 部署前备份：gpu33 `data/queue.db.bak_brief2_1109`（sqlite backup API，06:44 EST）。当时 running = 0。
- `deploy_both`（香港路线版）：tar sha `daca9cd6…` 两台上传核对一致；redeploy 后在服务器 grep 到新符号（i2va_fallback / board_no_text / comfy_prompts / recheck_remote_frames / MEDIA_EXT），worker 启动时间 gpu33 20:46:07、gpu36 20:46:51 JST；API 重启后 `/api/health` ok。

## 部署后的意外（如实记录）
- 旧线上代码前置不认 best_effort；新代码按项目方“尽力版一律自动用”认 → 积压的 smoke_v2 ep01 尾帧行（19 / 23 / 221 / 229）被领走真跑，gpu36 上 smoke_v2 ep01 film（301）重拼 done。
- 20:50:21–20:50:39 gpu33 领了 ep05 的 11 行（242/251/254/258/263/266/270/275/278/282/304）：全部 preflight / 0 变体 / 无视频 **秒级 blocked**，没有出图、没有出片、没有写 takes。行状态 queued → blocked（可再 requeue）。
- 项目方 20:5x 选 A：`PUT /api/runs` 把 series10_v2、smoke_v2 设 paused（已核 runs 表）。之后不再领新行。

## live 证据
| 项 | 结果 |
|---|---|
| B1 文件口：`/files/data/queue.db` 无密钥 | 401 |
| B1 文件口：`queue.db` / `data/ns2.key` 带密钥（gpu33、gpu36） | 403 / 403 / 403 |
| B1 live 入队伪造 done | 403 |
| B1 导入口（合法 body，未开 NS2_ALLOW_IMPORT） | 403，jobs max id 不变（304） |
| 堵点 3 非成人镜 ep01-S1 trigger | 400“按剧本不是成人镜”；triggers 表该镜 0 行 |
| 收尾第 10 项：series10 定稿 | reviews 表 series10_v2 passed = 55，时间 11-09 05:01:40–05:10:15 JST，按分钟 7–9 条分布（不是同一秒一次写入）；项目方确认本人所点 |
| 审片史每日导出 | gpu33 root crontab 已装 `10 15 * * *`（EST）；首份导出 `backup/reviews/reviews_20261109065604.json`（7920 B） |

## 本轮修的 bug
- `install_review_export_cron.sh`：`set -e` 下 crontab 为空时 `grep -v` 返回 1，子 shell 在 echo 前退出、装进空表。线上第一次跑踩到（root crontab 原本无其他行，`grep -v` 返回 1 正是因为没有行可输出，所以没有丢任何既有条目）。改 `{ …; } || true`，重跑后 crontab 有该行。

## 仍未做（要往 live 队列写东西 / 起真任务，等项目方点头）
- ep05 同剧本重入队（行数 / takes seq 对照）→ r17 live 部分
- kill worker → reap → ComfyUI 被中断 → b3 live 部分（需要有一条视频在跑；现在 run 都 paused）
