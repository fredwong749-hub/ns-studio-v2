# 回滚到旧系统 / 切回新系统（P5，M10 按真实演练重写）

旧仓库：https://github.com/fredwong749-hub/NS-Final-Studio ，tag `archive/ns-final-20261108`（be08e778）。
服务器旧部署目录 `/root/work/fredw/offline_20261101/repo`（sv-20261107-13）原样保留；新系统在 `/root/work/fredw/ns2/`。
**数据不回滚**：两边读写同一份 `data/`（ns2 的 `code/data` 是旧部署 data 的软链）；takes.json 版本号只增不覆盖；
queue.db（新系统的 165 迁移行 + film 行等）原地保留，旧系统不读它。唯一要守的是**双写禁令**：同一台机器上新旧派单不能同时开。

## 切回旧系统（to-old）

1. **每台服务器**（先 gpu33 再 gpu36），先看要做什么，再真做：
   ```bash
   cd /root/work/fredw/ns2/code
   bash ns2/rollback.sh to-old --dry
   bash ns2/rollback.sh to-old
   ```
   做的事：停本目录的 ns2 worker（gpu33 还停 ns2 API）→ `bash $OFF/start.sh <host>` 起旧 producer / night（start.sh 自己删 STOP）→ 打印 status。
2. **视频**：笔记本 `D:\NS-Final Studio` 里 `.venv\Scripts\python.exe tools/series10_video/night.py --run series10_v2`（night 经 deploy.py 起各机 vidgen）。
3. **笔记本 W18 lifecycle**（P5 关掉的，旧系统要它在心跳过期时自动拉起）：
   `copy "D:\NS-Final Studio\config\pipeline\sop_gate.json.bak_P5_1108" "D:\NS-Final Studio\config\pipeline\sop_gate.json"`（恢复 lifecycle.apply=true）。
4. **前端**：不用动。旧页的数据源已于 11-09 恢复（旧仓库 a1c2f6da revert 了 a12f5890）；保留的 87c78b47（隧道 18900 / 18893 + /ns2 桥）对旧系统无影响。
5. 核对：`bash ns2/rollback.sh status` 两台都是“STOP 无、旧 producer/night 在、ns2 worker 无”。

## 切回新系统（to-new）

1. **先关笔记本 lifecycle**（否则它会删 STOP 把旧派单拉回来——11-08 19:05 / 19:44 实测两次）：
   `config\pipeline\sop_gate.json` 的 `lifecycle.apply` 改 `false`（P5 版即现文件；从备份恢复过的话再改回）。
2. **每台服务器**（先 gpu33：它起 API）：
   ```bash
   cd /root/work/fredw/ns2/code
   bash ns2/rollback.sh to-new --dry
   bash ns2/rollback.sh to-new
   ```
   做的事：写 STOP → 停旧 night / producer（pid 文件）和本机 vidgen → 起 ns2 API（只 gpu33、没在跑时）和 worker → 打印 status 与 /api/health。
3. 笔记本停 night.py（如果第 2 步“视频”起过）。
4. 核对：status 两台“STOP 在、旧派单无、ns2 worker 在”；`/api/health` 的 workers 两台 age < 60 s；
   可选 echo 验证（要密钥，见 README“鉴权”）：入一条 `payload.echo=true` 的 board 行，15 秒内 done。

## 已知
- 脚本只停 pid 文件 / cwd 能确认是自己的进程（不 pkill）；`pgrep` 模式带 `[x]` 防误杀自己的 shell（11-08 实测踩过）。
- 演练记录：11-08 19:27 手工版（reports/m1/rollback_drill.log）；11-09 脚本版见 reports/p0/r9_M10.md。
