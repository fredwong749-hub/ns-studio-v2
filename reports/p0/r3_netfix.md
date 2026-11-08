# P1 r3：内网拓扑移出公开仓库（审查 §3 末条 / 任务书红线）

- `git rm`：config/netfix/（ssh_config、known_hosts、tunnels*.json、partition.json、override.json、ssh_callers.txt）、
  config/factory/hosts.json、tools/netfix/rssh.py；.gitignore 加这三处与 *.key
- 运行影响：无。ns2 不读它们（分区表硬编码在 ns2/migrate.py PARTITION）；帧步 / 打分跑的是服务器旧部署目录，不读本仓库 vendored 树；
  服务器上 code/ 目录的副本不受 git rm 影响（redeploy 只解 ns2/ 与 tests/）
- 剩余跳板主机名 / 中转地址：git grep 0 命中
- **未解决（需项目方）**：这些文件仍在 git 历史里（仓库是 PUBLIC）。清除历史 = 改仓库为私有，或改写历史强推，二者都要项目方定
- 测试：见 r3_pytest.txt
