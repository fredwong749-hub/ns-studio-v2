#!/bin/bash
# 审片史每日导出的定时任务（项目方 11-09 批准）。只在 gpu33 上跑一次；幂等（已有同一行不重复加）。撤掉：crontab -l | grep -v ns2.export_reviews | crontab -
set -eu
PY=/root/work/fredw/offline_20261101/venv/bin/python
LINE="10 15 * * * cd /root/work/fredw/ns2/code && $PY -m ns2.export_reviews >> /root/work/fredw/ns2/logs/export_reviews.log 2>&1"
# 服务器时区美东：15:10 EST = 次日 05:10 JST（错开凌晨生产高峰）
# 注意 set -e：crontab 为空时 grep -v 返回 1，会让子 shell 在 echo 前退出、装进空表（11-09 实测踩到），所以 || true
( { crontab -l 2>/dev/null | grep -v "ns2.export_reviews"; } || true ; echo "$LINE" ) | crontab -
crontab -l | grep ns2.export_reviews
cd /root/work/fredw/ns2/code && $PY -m ns2.export_reviews
