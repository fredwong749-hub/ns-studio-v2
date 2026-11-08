# 回滚到旧系统（P5）

旧仓库：https://github.com/fredwong749-hub/NS-Final-Studio ，tag `archive/ns-final-20261108`（be08e778）。服务器部署目录 `/root/work/fredw/offline_20261101/repo`（sv-20261107-13）原样保留。

1. 停新 worker / 新 API（M1 起写入 systemd 单元名）。
2. 两台服务器重起旧派单：`cd /root/work/fredw/offline_20261101/repo && nohup /root/work/fredw/offline_20261101/venv/bin/python -u tools/autopilot/producer.py serve >> logs/producer.log 2>&1 &`
3. 视频：笔记本 `python tools/series10_video/night.py --run series10_v2`（night 经 deploy.py 起各机 vidgen）。
4. 前端：revert 新前端数据源那一个提交。
数据不用回滚：takes.json 写入口语义不变，版本号只增不覆盖。
