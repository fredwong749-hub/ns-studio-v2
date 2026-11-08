"""M1c 首 / 尾帧步：一次认领 = 一次出图 + 契约打分（旧 FrameExec 原代码，经 ns2/legacy_frame.py 子进程在旧部署目录里跑）。

结果映射：pass → done（旧 auto_current 已按契约设当前）；fail → retry（has_output，用尽 → best_effort，不设当前，等人工）；
na（本镜无尾帧）→ na；preflight 不过 / 成人未触发 → blocked（写清原因）；打分 / 连接出错 → infra（回队不计次，连续 5 次 → blocked）。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from ns2.step_video import PORTS

OFF = Path(os.environ.get("NS2_LEGACY_OFF", "/root/work/fredw/offline_20261101"))
RUNNER = Path(__file__).resolve().parent / "legacy_frame.py"


def legacy_env(host: str, run: str) -> dict:
    """与旧 start.sh / resident_restart.sh 同一套环境（OFFLINE_SELF 决定打分走本机、人数检测走本机常驻等）"""
    vision = OFF / "repo/models_cache/vision"
    return dict(os.environ, TZ="Asia/Tokyo", OFFLINE_SELF=host, OFFLINE_ALIASES="gpu33" if host == "gpu36" else "",
                PATH=f"{OFF}/bin:" + os.environ.get("PATH", ""), PYTHONUTF8="1", PYTHONIOENCODING="utf-8", NS_RUN=run,
                NS_QC_VISION_DIR=str(vision if vision.is_dir() else "/root/work/fredw/video10_srv/models_cache/vision"),
                PYTHONPATH="")


def call_legacy(args: dict, host: str, timeout_s: float = 3600) -> dict:
    r = subprocess.run([str(OFF / "venv/bin/python"), "-u", str(RUNNER), json.dumps(args, ensure_ascii=False)],
                       cwd=str(OFF / "repo"), env=legacy_env(host, args["run"]), capture_output=True, text=True, timeout=timeout_s)
    line = [ln for ln in (r.stdout or "").splitlines() if ln.startswith("@@NS2@@")]
    if not line:
        return {"outcome": "infra", "error": f"旧执行器无结果 rc={r.returncode}：{(r.stderr or r.stdout or '')[-600:]}"}
    return json.loads(line[-1][7:])


def run(job: dict, lane: str):
    kind = job["step"]
    p = job.get("payload") or {}
    res = (job.get("result") or {})
    prev = list(res.get("prev_blocking") or [])
    args = {"kind": kind, "sid": job["shot"], "run": job["run_id"], "lane": lane, "port": PORTS[lane],
            "attempt": int(job.get("tries") or 0) + 1, "prev_blocking": prev}
    out = call_legacy(args, lane.split(":")[0], float(p.get("timeout_s") or 3600))
    o = out.pop("outcome", "infra")
    keep = {**out, "prev_blocking": prev + ([out.get("blocking") or []] if o in ("pass", "fail") else []),
            "has_output": out.get("take") is not None}
    if o == "pass":
        return "done", keep, None
    if o == "na":
        return "na", keep, None
    if o == "fail":
        return "retry", keep, "契约没过：" + "/".join(out.get("blocking") or []) + "；" + "；".join(out.get("reasons") or [])[:300]
    if o == "blocked":
        return "blocked", keep, out.get("error")
    if o == "content_bad":
        return "retry", keep, out.get("error")
    return "infra", keep, out.get("error")
