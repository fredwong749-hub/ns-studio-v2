"""补选尽力版（审查 3 §5b：promote_only 要么接上线 + 测试 + 审计，要么删；这里接上线）。

规则上线前已经用完次数的帧（队列里最新一行是 best_effort、takes.json 没有合格当前版）按同一规则补选当前版：
    cd /root/work/fredw/ns2/code && python -m ns2.promote_best_effort --run series10_v2 --ep 5 [--dry]
- 走 legacy_frame.promote_best（同一函数、同一 best_effort 标记、同一成人拒绝：成人镜未经项目方触发一律不提升）
- 审计：每镜一行写进 --log（默认 logs/promote_best_effort.jsonl）；旧系统的 current_changes.jsonl 由 set_active 照记
范围：项目方 11-09 定“全部集”。只动 takes.json 的当前版（不改队列行状态、不造新版本）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path


def best_effort_rows(c: sqlite3.Connection, run: str, ep: int | None) -> list[dict]:
    """每镜每步（first / last）最新一行是 best_effort 的"""
    c.row_factory = sqlite3.Row
    q = ("SELECT j.* FROM jobs j WHERE j.run_id=? AND j.step IN ('first','last') AND j.id=(SELECT MAX(k.id) FROM jobs k"
         " WHERE k.run_id=j.run_id AND k.shot=j.shot AND k.step=j.step) AND j.state='best_effort'")
    args: list = [run]
    if ep is not None:
        q += " AND j.ep=?"
        args.append(ep)
    return [dict(r) for r in c.execute(q + " ORDER BY j.shot, j.step", args)]


def promote(rows: list[dict], run: str, call=None, adult_of=None, triggered_of=None) -> list[dict]:
    from ns2.step_frame import PORTS, call_legacy
    from ns2.step_video import shot_adult
    call = call or call_legacy
    adult_of = adult_of or shot_adult
    out = []
    for r in rows:
        host = r.get("host") or r.get("want_host") or "gpu33"
        lane = r.get("lane") or f"{host}:0"
        args = {"kind": r["step"], "sid": r["shot"], "run": run, "lane": lane, "port": PORTS.get(lane), "promote_only": True,
                "adult": adult_of(run, r["shot"]), "owner_triggered": bool(triggered_of and triggered_of(r["shot"]))}
        res = call(args, host, 600)
        out.append({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "job": r["id"], "shot": r["shot"], "step": r["step"], "result": res})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--ep", type=int)
    ap.add_argument("--db", default="/root/work/fredw/ns2/data/queue.db")
    ap.add_argument("--log", default="logs/promote_best_effort.jsonl")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    c = sqlite3.connect(a.db)
    rows = best_effort_rows(c, a.run, a.ep)
    trig = {r[0] for r in c.execute("SELECT shot FROM triggers WHERE run_id=?", (a.run,))}
    if a.dry:
        for r in rows:
            print(r["id"], r["shot"], r["step"])
        return 0
    res = promote(rows, a.run, triggered_of=lambda s: s in trig)
    Path(a.log).parent.mkdir(parents=True, exist_ok=True)
    with open(a.log, "a", encoding="utf-8") as fh:
        for x in res:
            fh.write(json.dumps(x, ensure_ascii=False) + "\n")
            print(x["job"], x["shot"], x["step"], json.dumps(x["result"], ensure_ascii=False)[:300])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
