#!/bin/bash
# B3 live 演练：smoke_v2 ep01-S2 视频重出 → 跑到 ComfyUI → 杀 gpu36 worker → reap（同 D.reap 代码，短租约、只动本行）→ 重启 worker → 看 ComfyUI 被撤
F="/d/NS-Final Studio/config/netfix/ssh_config"
G33() { timeout 120 /usr/bin/ssh -F "$F" gpu33-hk "$@"; }
G36() { timeout 120 /usr/bin/ssh -F "$F" gpu36-hk "$@"; }
ts() { date +%H:%M:%S; }
PYS='P=/root/work/fredw/offline_20261101/venv/bin/python; cd /root/work/fredw/ns2/code; K=$($P -c "from ns2 import auth;print(auth.load_key())")'
api() { G33 "$PYS; curl -s -X $1 -H \"X-NS2-Key: \$K\" -H Content-Type:application/json -d '$3' 10.0.0.33:8900$2"; echo; }
q() { G33 "cd /root/work/fredw/ns2 && /root/work/fredw/offline_20261101/venv/bin/python -c \"import sqlite3;c=sqlite3.connect('file:data/queue.db?mode=ro',uri=True)
$1\""; }
comfyq() { G36 'for p in 8188 8189 8190; do echo "port $p: $(curl -s -m 5 127.0.0.1:$p/queue | head -c 300)"; done'; }

echo "[$(ts)] 0 before"; comfyq
echo "[$(ts)] 1 dispatch"; OUT=$(api POST /api/shots/ep01-S2/jobs '{"run_id":"smoke_v2","stage":"video","mode":"regen","by":"drill-b3"}'); echo "$OUT"
J=$(echo "$OUT" | python -c "import sys,json;print(json.load(sys.stdin)['job']['id'])")
echo "J=$J"
echo "[$(ts)] 2 unpause smoke_v2"; api PUT /api/runs '{"run_id":"smoke_v2","title":"smoke_v2 M1c","status":"producing"}'
LIVE=""
for i in $(seq 1 30); do
  R=$(q "print(c.execute('select state,host,lane from jobs where id=$J').fetchone(), c.execute('select pid,lane,state from comfy_prompts where job_id=$J').fetchall())")
  echo "[$(ts)] poll $i $R"
  case "$R" in *live*) LIVE=1; break;; esac
  sleep 10
done
echo "[$(ts)] 3 pause smoke_v2 again"; api PUT /api/runs '{"run_id":"smoke_v2","title":"smoke_v2 M1c","status":"paused"}'
[ -n "$LIVE" ] || { echo "NO LIVE PROMPT — abort drill (smoke_v2 paused)"; exit 1; }
sleep 5; echo "[$(ts)] 4 comfy while running"; comfyq
echo "[$(ts)] 5 kill gpu36 worker"; G36 'W=$(cat /root/work/fredw/ns2/worker.pid); [ "$(readlink /proc/$W/cwd)" = /root/work/fredw/ns2/code ] && kill $W && echo killed $W'
sleep 30; echo "[$(ts)] 6 comfy 30s after kill (prompt should still run = the B3 problem)"; comfyq
echo "[$(ts)] 7 reap (D.reap, lease 20s, only if J is the only stale running row)"
G33 "cd /root/work/fredw/ns2/code && /root/work/fredw/offline_20261101/venv/bin/python -c \"
import sqlite3,time
from ns2 import db as D
c=sqlite3.connect('/root/work/fredw/ns2/data/queue.db',timeout=30, isolation_level=None); c.row_factory=sqlite3.Row
stale=[r['id'] for r in c.execute('select id from jobs where state=? and (beat_at is null or beat_at < ?)',('running',time.time()-20))]
print('stale',stale)
assert stale==[$J], stale
with D.tx(c):
    print('reaped', D.reap(c, lease_s=20))
print(c.execute('select id,state,error,ready_at from jobs where id=$J').fetchone()[:])
print([tuple(r) for r in c.execute('select pid,state from comfy_prompts where job_id=$J')])
\""
echo "[$(ts)] 8 comfy after reap, before worker restart"; comfyq
echo "[$(ts)] 9 restart gpu36 worker"
G36 'cd /root/work/fredw/ns2/code || exit 1; export TZ=Asia/Tokyo; PY=/root/work/fredw/offline_20261101/venv/bin/python; PYTHONPATH=services/agent:services/render_router:services/recipes:services/post:packages/schema:packages/qc:. nohup $PY -u -m ns2.worker --api http://10.0.0.33:8900 --host gpu36 --lanes gpu36:0,gpu36:1,gpu36:2 --files 10.0.0.36:8901 >> /root/work/fredw/ns2/logs/worker.log 2>&1 < /dev/null & echo $! > /root/work/fredw/ns2/worker.pid; echo started $!'
sleep 25
echo "[$(ts)] 10 comfy after restart"; comfyq
q "print([tuple(r) for r in c.execute('select pid,job_id,lane,state,at,done_at from comfy_prompts where job_id=$J')]); print(c.execute('select id,state,error from jobs where id=$J').fetchone()); print(c.execute('select status from runs where run_id=?',('smoke_v2',)).fetchone())"
G36 'tail -8 /root/work/fredw/ns2/logs/worker.log | cut -c1-200; ls /root/work/fredw/ns2/pending/comfy/ 2>&1 | head'
