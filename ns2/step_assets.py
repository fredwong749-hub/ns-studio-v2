"""P6 R1a assets 步：一集一行（shot = epNN，出图机），排在 script 之后、board 之前。
在出图机本机把这一集出首帧前缺的前期资产（身份脸 / 场景底图 / 造型设定图 / 单品图 / 派生参考图）补齐，经 ns2/legacy_assets.py 子进程跑。

ok → done（db.finish 把本集因预检 blocked 的 board 行放回队列）；资产补不齐 → blocked（写清缺什么）；出错 → infra。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ns2.step_frame import OFF, legacy_env
from ns2.step_video import PORTS

RUNNER = Path(__file__).resolve().parent / "legacy_assets.py"


def run(job: dict, lane: str):
    host = lane.split(":")[0]
    args = {"run": job["run_id"], "ep": int(job["ep"]), "lane": lane, "host": host, "port": PORTS[lane],
            "max_passes": int((job.get("payload") or {}).get("max_passes") or 3)}
    r = subprocess.run([str(OFF / "venv/bin/python"), "-u", str(RUNNER), json.dumps(args, ensure_ascii=False)],
                       cwd=str(OFF / "repo"), env=legacy_env(host, job["run_id"]), capture_output=True, text=True, timeout=4 * 3600)
    line = [ln for ln in (r.stdout or "").splitlines() if ln.startswith("@@NS2@@")]
    if not line:
        return "infra", None, f"资产执行器无结果 rc={r.returncode}：{(r.stderr or r.stdout or '')[-600:]}"
    out = json.loads(line[-1][7:])
    o = out.pop("outcome", "infra")
    if o == "ok":
        return "done", out, None
    if o == "blocked":
        return "blocked", out, out.get("error")
    return "infra", out, out.get("error")
