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
KEEP = ("graph", "inputs", "seed", "prompt", "status", "recipe", "frames", "variant", "mode", "long", "reason", "missing", "ref_slots")   # B4：reason 必须带
MAX_RENDERS = 4                      # 旧阶梯上限（vidgen MAX_RENDERS）


def post(api: str, path: str, body: dict) -> dict:
    import urllib.request
    from ns2 import auth as AU
    req = urllib.request.Request(api.rstrip("/") + path, json.dumps(body).encode(), {"Content-Type": "application/json", **AU.headers()})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def job_path(row: dict) -> str:
    """审查 3 B1：live 入队只收 queued / blocked 且不带 result；迁移登记的终态行（first/last 已完成）走导入口（API 须 NS2_ALLOW_IMPORT=1）"""
    return "/api/jobs" if row.get("state", "queued") in ("queued", "blocked") and row.get("result") is None else "/api/import/jobs"


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
    why = [] if fok else [f"首帧当前版 v{fn} 没过门"]
    if not no_last and not lok:
        why.append(f"尾帧当前版 v{ln} 没过门")
    if not ready:
        why.append("没有可派的变体：" + "；".join(f"{k}={v.get('status')}" + (f"（{v.get('reason')}）" if v.get("reason") else "")
                                           for k, v in vs.items()))
    rows.append({"run_id": run, "ep": ep, "shot": sid, "step": "video", "want_host": host,
                 **({"state": "blocked", "error": "视频不入排队：" + "；".join(why)[:900]} if why else {}),   # M6 / M8：前置不过直接 blocked 写原因
                 "max_tries": max(1, min(MAX_RENDERS, len(ready))),
                 "sig": {"first": sh.get("first_frame_take"), "last": sh.get("last_frame_take"), "cache": sh.get("cache_key")},
                 "payload": {"run": run, "variants": vs, "ladder": sh.get("ladder_order"),
                             "first_take": sh.get("first_frame_take"), "last_take": None if no_last else sh.get("last_frame_take"),   # M7：preflight 比对当前版
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
    bad = []
    for sid in a.shots.split(","):
        try:                                # M8：一镜坏（jobs.json / takes.json 缺、镜不在表里）只跳过这一镜，不连坐后面的
            rows = build(a.run, sid, host)
        except (OSError, ValueError, KeyError, SystemExit) as e:
            bad.append(sid)
            print(sid, "SKIP", f"{type(e).__name__}: {e}"[:300])
            continue
        for r in rows:
            if a.trial and r["step"] == "video":
                r["payload"].update(trial=True, fresh_seed=True)
                r["sig"] = {**r["sig"], "trial": __import__("time").time()}
            if a.dry:
                print(r["shot"], r["step"], r.get("state", "queued"), "variants=" + ",".join((r.get("payload") or {}).get("variants") or []))
                continue
            out = post(a.api, job_path(r), r)
            print(r["shot"], r["step"], out["job"]["state"], "id", out["job"]["id"], "new" if out["created"] else "existing")
    if bad:
        print("跳过的镜：", ",".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
