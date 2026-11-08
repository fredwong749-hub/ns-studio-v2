"""M2 迁移导入：把 M0 合并报告（reports/migrate_<run>.json，项目方 11-08 已确认）里每镜 first/last/video 的状态登记进队列。

    python -m ns2.import_state --api http://10.0.0.33:8900 --report reports/migrate_series10_v2.json [--dry]

只登记，不重出（PLAN 决策③：4 集原样迁）：
  done / na            → 同状态（result.migrated=True，带 take 号与文件）
  best_effort（视频）  → best_effort，等项目方定（不自动重出）
  needs_review（首帧） → blocked，error 写清“等项目方看”；它的视频行也 blocked（前置没过，不自动出）
  missing_file / queued → blocked，写清缺什么（迁移不触发出图 / 出片；要出走 enqueue / requeue）
每集最后登记一条 film 行（排队，本集视频机），整集合成 v1。幂等：sig = 迁移 take 号，重跑不重复。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ns2.enqueue_video import post

STATE_MAP = {"done": "done", "na": "na", "best_effort": "best_effort"}


def rel(f: str | None) -> str | None:
    """报告里 takes 文件相对 data/、state_ 尽力版相对仓库根 → 统一成相对仓库根"""
    if not f:
        return None
    return f if f.startswith("data/") else f"data/{f}"


def rows_for(r: dict, run: str) -> list[dict]:
    out = []
    first_ok = r["first"]["state"] == "done"
    for step in ("first", "last", "video"):
        m = r[step]
        st = STATE_MAP.get(m["state"])
        err = None
        if st is None:
            st = "blocked"
            err = {"needs_review": f"迁移：{m.get('why') or '未过门'}，等项目方看（不自动重出）",
                   "missing_file": "迁移：当前版文件不在本机，先拉取再重派",
                   "queued": "迁移：旧系统没有这一步的当前版；" + ("首帧未过门，等项目方定首帧后再派" if not first_ok else "要出请重派")}[m["state"]]
        host = r["lane_video"] if step == "video" else r["lane_images"]
        out.append({"run_id": run, "ep": r["ep"], "shot": r["shot"], "step": step, "state": st, "want_host": host,
                    "sig": {"migrated": m.get("n"), "file": rel(m.get("file"))},
                    "result": {"migrated": True, "take_n": m.get("n"), "file": rel(m.get("file")), "note": m.get("note"),
                               "has_output": bool(m.get("file"))},
                    **({"error": err} if err else {})})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--run", default="series10_v2")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    rows = json.loads(Path(a.report).read_text(encoding="utf-8"))
    n = {"new": 0, "existing": 0}
    for r in rows:
        for j in rows_for(r, a.run):
            if a.dry:
                print(j["shot"], j["step"], j["state"], j["want_host"], j.get("error") or "")
                continue
            out = post(a.api, "/api/jobs", j)
            n["new" if out["created"] else "existing"] += 1
    for ep in sorted({r["ep"] for r in rows}):
        host = next(r["lane_video"] for r in rows if r["ep"] == ep)
        if a.dry:
            print(f"ep{ep:02d} film queued {host}")
            continue
        out = post(a.api, f"/api/episodes/{ep}/assemble", {"run_id": a.run, "want_host": host})
        print(f"ep{ep:02d} film", out["job"]["id"], out["job"]["state"], "new" if out["created"] else "existing")
    print(json.dumps(n))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
