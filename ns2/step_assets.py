"""P6 R1a 资产两步（出图机，经 ns2/legacy_assets.py 子进程在旧部署目录里跑）：

- assets（一集一行，shot = epNN，排在 script 后）：规划。查缺口、当场写不用 GPU 的登记（造型骨架 / 本集排期 / 名字），
  每个要出图的资产排一行 asset（payload：asset 作业 id、服务哪几镜 shots、要先等的资产 after）
- asset（一个资产一行）：任何空闲道都能领（各道并行）；出到 1 张过检按分数自动选用登记；2 张都不过 → blocked 写根因，不盲试
board 只等“覆盖本镜”的 asset 行（db._claim_sql），不缺资产的镜不等整集（总方案病因 5：fail-closed 不能放在整集粒度）
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from ns2.step_frame import OFF, legacy_env
from ns2.step_video import PORTS

RUNNER = Path(__file__).resolve().parent / "legacy_assets.py"


def _call(args: dict, host: str, run: str, timeout_s: float) -> dict:
    r = subprocess.run([str(OFF / "venv/bin/python"), "-u", str(RUNNER), json.dumps(args, ensure_ascii=False)],
                       cwd=str(OFF / "repo"), env=legacy_env(host, run), capture_output=True, text=True, timeout=timeout_s)
    line = [ln for ln in (r.stdout or "").splitlines() if ln.startswith("@@NS2@@")]
    if not line:
        return {"outcome": "infra", "error": f"资产执行器无结果 rc={r.returncode}：{(r.stderr or r.stdout or '')[-600:]}"}
    return json.loads(line[-1][7:])


def asset_rows(job: dict, assets: list[dict]) -> list[dict]:
    return [{"run_id": job["run_id"], "ep": int(job["ep"]), "shot": job["shot"], "step": "asset", "want_host": job.get("want_host"),
             "max_tries": 1, "sig": {"asset": a["asset"], "plan": job["id"]},
             "payload": {"asset": a["asset"], "kind": a.get("kind"), "shots": a.get("shots") or [], "after": a.get("after") or []}}
            for a in assets]


def run(job: dict, lane: str):
    """assets：规划"""
    host = lane.split(":")[0]
    out = _call({"mode": "plan", "run": job["run_id"], "ep": int(job["ep"]), "host": host}, host, job["run_id"], 1800)
    o = out.pop("outcome", "infra")
    if o != "ok":
        return "infra", out, out.get("error")
    from ns2.enqueue_video import post
    api = os.environ.get("NS2_API", "http://10.0.0.33:8900")
    made = []
    for r in asset_rows(job, out.get("assets") or []):
        res = post(api, "/api/jobs", r)
        made.append({"asset": r["payload"]["asset"], "job": res["job"]["id"], "new": res["created"]})
    return "done", {**out, "asset_jobs": made, "has_output": True}, None


def run_one(job: dict, lane: str):
    """asset：出一个资产"""
    host = lane.split(":")[0]
    p = job.get("payload") or {}
    out = _call({"mode": "one", "run": job["run_id"], "ep": int(job["ep"]), "host": host, "lane": lane, "port": PORTS[lane],
                 "asset": p["asset"]}, host, job["run_id"], 3600)
    o = out.pop("outcome", "infra")
    if o == "ok":
        return "done", out, None
    if o == "blocked":
        return "blocked", out, out.get("error")
    return "infra", out, out.get("error")
