#!/usr/bin/env bash
# 新 ↔ 旧系统切换（M10：按 11-08 19:27 真实演练脚本化）。在每台服务器上跑；先 --dry 看要做什么。
#   bash ns2/rollback.sh to-old [--dry]     切回旧系统（停 ns2 worker / API → 旧 start.sh 起旧派单，start.sh 自己删 STOP）
#   bash ns2/rollback.sh to-new [--dry]     切回新系统（写 STOP → 停旧派单 → 起 ns2 API / worker）
#   bash ns2/rollback.sh status             看两边各有什么在跑
# 只停 pid 文件 / cwd 能确认是自己的进程；不碰 queue.db、takes.json（数据两边共用，版本号只增不覆盖）。
# 笔记本那一步（W18 lifecycle 会在旧心跳过期时拉起旧派单并删 STOP）见 docs/ROLLBACK.md 第 3 步，脚本管不到笔记本。
set -eu    # 审查 3 M10：任何一步出错就停（不带着半截状态往下切）；可预期为空的查询都写了 || true
MODE=${1:-status}; DRY=${2:-}
OFF=/root/work/fredw/offline_20261101; N=/root/work/fredw/ns2; PY=$OFF/venv/bin/python; A=http://10.0.0.33:8900
H=$(hostname); if [ "$H" = gpu6 ]; then HOST=gpu36; LANES=gpu36:0,gpu36:1,gpu36:2; IP=10.0.0.36; else HOST=gpu33; LANES=gpu33:0,gpu33:1; IP=10.0.0.33; fi
run() { echo "+ $*"; [ "$DRY" = --dry ] || eval "$@"; }
mine() { for p in $(pgrep -f "$1" || true); do if [ "$(readlink /proc/$p/cwd 2>/dev/null || true)" = "$2" ]; then echo $p; fi; done; }

status() {
  echo "== $HOST $(date '+%F %T %Z')"
  echo "STOP: $([ -f $OFF/STOP ] && echo 在 || echo 无)"
  echo "ns2 worker: $(mine '[n]s2.worker --api' $N/code | tr '\n' ' ')"
  echo "ns2 api:    $(mine '[u]vicorn ns2.api:app' $N/code | tr '\n' ' ')"
  echo "旧 producer/night: $(for n in producer night; do p=$(cat $OFF/run/$n.pid 2>/dev/null); [ -n "$p" ] && kill -0 $p 2>/dev/null && echo -n "$n=$p "; done)"
  echo "旧 vidgen:  $(pgrep -f "[v]idgen.py run $HOST" | tr '\n' ' ')"
}

case "$MODE" in
status) status ;;
to-old)
  status
  for p in $(mine '[n]s2.worker --api' $N/code); do run kill $p; done
  if [ $HOST = gpu33 ]; then for p in $(mine '[u]vicorn ns2.api:app' $N/code); do run kill $p; done; fi
  run "bash $OFF/start.sh $HOST"          # 旧启动脚本：起 producer / night，并删 STOP
  [ "$DRY" = --dry ] || { sleep 20; status; } ;;
to-new)
  status
  run "echo 'P5 ns2 cutover $HOST $(date -Iseconds)' > $OFF/STOP"
  for n in night producer; do p=$(cat $OFF/run/$n.pid 2>/dev/null || true); if [ -n "$p" ] && kill -0 $p 2>/dev/null; then run kill $p; fi; done
  for p in $(pgrep -f "[v]idgen.py run $HOST" || true); do run kill $p; done
  cd $N/code || exit 1
  export TZ=Asia/Tokyo PYTHONPATH=services/agent:services/render_router:services/recipes:services/post:packages/schema:packages/qc:.
  if [ $HOST = gpu33 ] && [ -z "$(mine '[u]vicorn ns2.api:app' $N/code)" ]; then
    run "NS2_DB=$N/data/queue.db nohup $PY -m uvicorn ns2.api:app --host 10.0.0.33 --port 8900 --log-level warning >> $N/logs/api.log 2>&1 < /dev/null & echo \$! > $N/api.pid"
    [ "$DRY" = --dry ] || sleep 5
  fi
  if [ -z "$(mine '[n]s2.worker --api' $N/code)" ]; then
    run "nohup $PY -u -m ns2.worker --api $A --host $HOST --lanes $LANES --files $IP:8901 >> $N/logs/worker.log 2>&1 < /dev/null & echo \$! > $N/worker.pid"
  fi
  [ "$DRY" = --dry ] || { sleep 8; status; curl -s $A/api/health | head -c 300; echo; } ;;
*) echo "用法：rollback.sh status | to-old [--dry] | to-new [--dry]"; exit 2 ;;
esac
