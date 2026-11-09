"""M0 迁移校验（只读）：扫旧系统 data/renders/<run>/epNN/SNN/takes.json，给每镜 first/last/video 三步判状态，出报告。

不写任何旧文件、不建队列；报告给项目方确认后，M1 才用它生成 jobs 表初始行。
状态口径（每镜每步唯一）：
  done          active 版文件在本机 且 过门（首 / 尾帧按 ns2.gates.frame_passed，与旧 build_jobs 同口径；视频 postflight.ok）
  needs_review  active 版文件在本机，postflight 未过（旧系统的尽力版 / 待人工定）——不自动重跑
  missing_file  有 active 但文件不在本机（拉取缺口，迁移前要先拉）
  best_effort   （仅视频）takes.json 没有当前版，但旧视频子系统 state_<host>.json 记 best_effort 且片子在 → 等人工定，不重跑
  queued        没有 active 版 → 进队重出
权威来源（P5 M0 实测）：各镜各步以“干这一步的那台服务器”的副本为准——出图看出图机、视频看视频机（笔记本副本落后）；
视频尽力版只在 state_<host>.json 里（不在 takes.json），所以视频步要并读它。
合并：python -m ns2.migrate merge --images gpu33=a.json,gpu36=b.json --video gpu33=a.json,gpu36=b.json
用法：python -m ns2.migrate --data <旧仓库 data 目录> [--run series10_v2] [--out reports/]
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

STEPS = {"first": "image", "last": "last", "video": "video"}
# P5 修正 ②：出图 / 视频分区分开（来源 config/netfix/partition.json，11-08 核）
PARTITION = {"images": {"gpu33": [1, 3, 5, 7, 9, 10], "gpu36": [2, 4, 6, 8]},
             "video": {"gpu33": [3, 5, 7, 9, 10], "gpu36": [1, 2, 4, 6, 8]}}


def host_for(kind: str, ep: int) -> str | None:
    return next((h for h, eps in PARTITION[kind].items() if ep in eps), None)


# P6 11-09 项目方“把 gpu36 也用起来”：视频行不再按集钉死一台，两台谁空谁领（出图机 ≠ 视频机时 step_video 先交接首尾帧，
# 拼片机缺的片经文件口取，见 handoff.sync_frames / step_film.fetch_remote）。出图仍按集分区（takes 在出图机上）。
VIDEO_ANY_HOST = True


def video_host(ep: int) -> str | None:
    """新入队视频行的 want_host：任一台 = None"""
    return None if VIDEO_ANY_HOST else host_for("video", ep)


def step_state(d: dict, stage: str, data: Path) -> dict:
    n = (d.get("active") or {}).get(stage)
    if n is None:
        return {"state": "queued"}
    t = next((x for x in d.get("takes") or [] if x.get("stage") == stage and x.get("n") == n), None)
    if t is None:
        return {"state": "queued", "note": f"active={n} 但账本里没有这一版"}
    f = data / str(t.get("file") or "")
    out = {"n": n, "file": t.get("file"), "sig": t.get("sig")}
    if not t.get("file") or not f.is_file():
        return {**out, "state": "missing_file"}
    if stage == "video":
        ok, why = bool((t.get("postflight") or {}).get("ok")), ""
        if t.get("master_reject"):                     # M9：视频也认 master_reject（原来只有帧查）
            ok, why = False, "master_reject"
        rv = review_of(data, d.get("_shot_dir"))
        if rv == "passed" and n is not None:           # M9：旧 review.json 项目方已定 → done（postflight 过期也算）
            ok, why = True, ""
        elif rv == "rejected":
            ok, why = False, "review.json rejected"
    else:
        from ns2.gates import frame_passed
        ok, why = frame_passed(stage, t, locked=(d.get("locked") or {}).get(stage) == n)
    return {**out, "state": "done" if ok else "needs_review", **({"why": why} if not ok else {})}


def review_of(data: Path, shot_dir) -> str | None:
    """旧系统 review.json（{state: passed|rejected|unreviewed}）；没有 / 读不了 → None"""
    if not shot_dir:
        return None
    try:
        return (json.loads((Path(shot_dir) / "review.json").read_text(encoding="utf-8")) or {}).get("state")
    except (OSError, ValueError):
        return None


def video_states(data: Path, run: str) -> dict:
    out = {}
    for f in sorted((data / "pipeline/runs" / run / "video").glob("state_*.json")):
        try:
            for sid, v in (json.loads(f.read_text(encoding="utf-8")).get("shots") or {}).items():
                if isinstance(v, dict):
                    out[sid] = {"status": v.get("status"), "src": f.name,
                                "files": [r.get("file") for r in v.get("renders") or [] if r.get("file")]}
        except (OSError, ValueError):
            pass
    return out


def scan(data: Path, run: str) -> list[dict]:
    rows = []
    vs = video_states(data, run)
    for tj in sorted((data / "renders" / run).glob("ep*/S*/takes.json"),
                     key=lambda p: (int(p.parts[-3][2:]), int(p.parts[-2][1:]))):
        ep, s = int(tj.parts[-3][2:]), tj.parts[-2]
        try:                                           # 一个坏 takes.json 不 abort 整份报告（审查 §3）
            d = {**json.loads(tj.read_text(encoding="utf-8")), "_shot_dir": str(tj.parent)}
        except (OSError, ValueError) as e:
            rows.append({"shot": f"ep{ep:02d}-{s}", "ep": ep, "lane_images": host_for("images", ep), "lane_video": host_for("video", ep),
                         **{k: {"state": "missing_file", "note": f"takes.json 读不了：{e}"[:200]} for k in STEPS}})
            continue
        row = {"shot": f"ep{ep:02d}-{s}", "ep": ep,
               "lane_images": host_for("images", ep), "lane_video": host_for("video", ep)}
        for step, stage in STEPS.items():
            row[step] = step_state(d, stage, data)
        v = vs.get(row["shot"]) or {}
        if row["video"]["state"] == "queued" and v.get("status") in ("best_effort", "done"):
            files = [x for x in v["files"] if (data.parent / x).is_file() or (data / x).is_file()]
            if files:
                row["video"] = {"state": "best_effort" if v["status"] == "best_effort" else "done",
                                "file": files[-1], "note": f"仅在 {v['src']}（takes.json 无当前版）", "candidates": len(files)}
        rows.append(na_last(row))
    return rows


def merge(images: dict, video: dict) -> list[dict]:
    """images / video = {host: rows}；每镜 first/last 取出图机的行、video 取视频机的行"""
    idx = {h: {r["shot"]: r for r in rows} for h, rows in {**images, **video}.items()}
    shots = sorted({s for rows in idx.values() for s in rows}, key=lambda s: (int(s[2:4]), int(s.split("-S")[1])))
    out = []
    for s in shots:
        any_r = next(idx[h][s] for h in idx if s in idx[h])
        ih, vh = any_r["lane_images"], any_r["lane_video"]
        ir, vr = (idx.get(ih) or {}).get(s), (idx.get(vh) or {}).get(s)
        row = {**any_r, "first": (ir or {}).get("first", {"state": "queued", "note": f"{ih} 无此镜"}),
                    "last": (ir or {}).get("last", {"state": "queued", "note": f"{ih} 无此镜"}),
                    "video": (vr or {}).get("video", {"state": "queued", "note": f"{vh} 无此镜"}), "src": {"images": ih, "video": vh}}
        out.append(na_last(row))
    return out


def na_last(row: dict) -> dict:
    """视频已出 / 尽力、却没有尾帧 = 本镜不用尾帧（L-204 D8 / i2va 兜底）→ last 记 na，不重排队"""
    if row["last"]["state"] == "queued" and row["video"]["state"] in ("done", "best_effort"):
        row["last"] = {"state": "na", "note": "视频已出、无尾帧：不用尾帧的镜（L-204 D8 / i2va）"}
    return row


def main(argv=None) -> int:
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["merge"]:
        ap = argparse.ArgumentParser()
        ap.add_argument("--images", required=True); ap.add_argument("--video", required=True)
        ap.add_argument("--run", default="series10_v2"); ap.add_argument("--out", default="reports")
        a = ap.parse_args(argv[1:])
        load = lambda spec: {h: json.loads(Path(p).read_text(encoding="utf-8")) for h, p in (x.split("=", 1) for x in spec.split(","))}
        return report(merge(load(a.images), load(a.video)), a.run, Path(a.out))
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--run", default="series10_v2")
    ap.add_argument("--out", default="reports")
    a = ap.parse_args(argv)
    return report(scan(Path(a.data), a.run), a.run, Path(a.out))


def report(rows: list[dict], run: str, out: Path) -> int:
    class A: pass
    a = A(); a.run = run
    out.mkdir(parents=True, exist_ok=True)
    (out / f"migrate_{a.run}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [f"# 迁移校验报告 {a.run}（只读，{len(rows)} 镜）", "",
             "| 步 | done | best_effort | na | needs_review | missing_file | queued |", "|---|---|---|---|---|---|---|"]
    for step in STEPS:
        c = Counter(r[step]["state"] for r in rows)
        lines.append(f"| {step} | {c['done']} | {c['best_effort']} | {c['na']} | {c['needs_review']} | {c['missing_file']} | {c['queued']} |")
    lines += ["", "## 按集", "", "| 集 | 镜 | first done | last done/na | video done/尽力 | 出图机 | 视频机 |", "|---|---|---|---|---|---|---|"]
    for ep in sorted({r["ep"] for r in rows}):
        rs = [r for r in rows if r["ep"] == ep]
        c = lambda st, s: sum(r[st]["state"] == s for r in rs)
        lines.append(f"| ep{ep:02d} | {len(rs)} | {c('first','done')} | {c('last','done')}/{c('last','na')} | {c('video','done')}/{c('video','best_effort')} | {rs[0]['lane_images']} | {rs[0]['lane_video']} |")
    lines += ["", "## 非 done 明细", ""]
    for r in rows:
        bad = [f"{st}={r[st]['state']}" for st in STEPS if r[st]["state"] not in ("done", "na")]
        if bad:
            lines.append(f"- {r['shot']}：" + "，".join(bad) )
    (out / f"migrate_{a.run}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:8]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
