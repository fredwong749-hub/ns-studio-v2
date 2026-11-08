"""把一镜的视频活入队（在该镜视频机上跑：读本机 build_jobs 产物 video/jobs.json）。

    python -m ns2.enqueue_video --api http://10.0.0.33:8900 --run series10_v2 --shots ep03-S4 [--dry]

同时把该镜 first / last 作为已完成（迁移导入）登记，视频步的前置才满足；前置以 takes.json 当前版为准，不过就不入视频。
幂等：sig = 首帧 take + 尾帧 take + jobs.json cache_key，同输入重复入队返回已有行。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

ROOT = Path(os.environ.get("NS2_ROOT") or Path(__file__).resolve().parents[1])
KEEP = ("graph", "inputs", "seed", "prompt", "status", "recipe", "frames", "variant", "mode", "long")
MAX_RENDERS = 4                      # 旧阶梯上限（vidgen MAX_RENDERS）


def post(api: str, path: str, body: dict) -> dict:
    import urllib.request
    req = urllib.request.Request(api.rstrip("/") + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def build(run: str, sid: str, host: str) -> list[dict]:
    jobs = json.loads((ROOT / "data/pipeline/runs" / run / "video/jobs.json").read_text(encoding="utf-8"))
    sh = next((s for s in jobs["shots"] if s.get("shot") == sid), None)
    if sh is None:
        raise SystemExit(f"{sid} 不在本机 jobs.json")
    ep = int(sh["ep"])
    tj = json.loads((ROOT / "data/renders" / run / f"ep{ep:02d}" / sid.split("-")[1] / "takes.json").read_text(encoding="utf-8"))
    act = tj.get("active") or {}

    from ns2.gates import frame_passed

    def ok(stage):
        n = act.get(stage)
        t = next((x for x in tj.get("takes") or [] if x.get("stage") == stage and x.get("n") == n), None)
        return n, frame_passed(stage, t, locked=(tj.get("locked") or {}).get(stage) == n)[0]
    fn, fok = ok("image")
    ln, lok = ok("last")
    rows = [{"run_id": run, "ep": ep, "shot": sid, "step": "first", "state": "done" if fok else "blocked",
             "sig": {"take": fn}, "result": {"take_n": fn, "migrated": True}}]
    no_last = sh.get("last_frame") is None
    rows.append({"run_id": run, "ep": ep, "shot": sid, "step": "last", "state": "na" if no_last else ("done" if lok else "blocked"),
                 "sig": {"take": ln}, "result": {"take_n": ln, "migrated": True}})
    vs = {k: {kk: v.get(kk) for kk in KEEP} for k, v in (sh.get("variants") or {}).items()}
    ready = [k for k in sh.get("ladder_order") or list(vs) if (vs.get(k) or {}).get("status") == "ready"]
    rows.append({"run_id": run, "ep": ep, "shot": sid, "step": "video", "want_host": host,
                 "max_tries": max(1, min(MAX_RENDERS, len(ready))),
                 "sig": {"first": sh.get("first_frame_take"), "last": sh.get("last_frame_take"), "cache": sh.get("cache_key")},
                 "payload": {"run": run, "variants": vs, "ladder": sh.get("ladder_order"),
                             "score_meta": {"refs": sh.get("score_refs") or {}, "size": sh.get("size"), "first_frame": sh.get("first_frame"),
                                            "prompt": (vs.get(ready[0]) or {}).get("prompt", "") if ready else ""}}})
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", required=True)
    ap.add_argument("--run", default="series10_v2")
    ap.add_argument("--shots", required=True)
    ap.add_argument("--host", default=os.uname().nodename if hasattr(os, "uname") else "")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--trial", action="store_true", help="验收试跑：只记 take 不动当前版、换新种子；视频行不做幂等去重")
    a = ap.parse_args(argv)
    host = {"gpu6": "gpu36"}.get(a.host, a.host)
    for sid in a.shots.split(","):
        for r in build(a.run, sid, host):
            if a.trial and r["step"] == "video":
                r["payload"].update(trial=True, fresh_seed=True)
                r["sig"] = {**r["sig"], "trial": __import__("time").time()}
            if a.dry:
                print(r["shot"], r["step"], r.get("state", "queued"), "variants=" + ",".join((r.get("payload") or {}).get("variants") or []))
                continue
            out = post(a.api, "/api/jobs", r)
            print(r["shot"], r["step"], out["job"]["state"], "id", out["job"]["id"], "new" if out["created"] else "existing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
