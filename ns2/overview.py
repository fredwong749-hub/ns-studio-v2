"""M2 唯一总览（PLAN §2.4）：GET /api/overview?run= 只读 jobs 表 + reviews 表 + 剧本，口径按 §2.4 口径表定死。

口径（唯一）：
  final / video_done  = 该镜最新视频行有片（done 或 best_effort）且项目方 review = passed。best_effort 不算 done，单独计数
  pending_review      = 最新视频行 done（过视频门）且还没 review
  total 分母          = 剧本镜数（script_v2.json），不是“有目录的镜”
  stale               = v1 不算（输入签名比对在 board 步上线后接）；stale_global 只作独立字段，不进 stale、不上卡片
  assembled           = 本集最新 film 行 done 且晚于本集所有视频行的落定时间
  eta                 = 分道（lane）中位数（视频行 result.secs）；无样本默认 840 秒
文件 url = /files/<host>/<相对 data 的路径>：由 API 转到产出那台机器的 worker 文件口（不依赖笔记本同步）。
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime
from pathlib import Path

ETA_DEFAULT_S = 840.0
FRAME_STATE = {"done": "passed", "na": "na", "running": "running", "queued": "pending", "failed": "failed",
               "best_effort": "best_effort", "blocked": "blocked"}


def data_rel(f: str | None) -> str | None:
    if not f:
        return None
    f = str(f).replace("\\", "/")
    for pre in ("/root/work/fredw/ns2/code/data/", "/root/work/fredw/offline_20261101/repo/data/"):
        if f.startswith(pre):
            return f[len(pre):]
    return f[5:] if f.startswith("data/") else f


def file_url(j: dict | None) -> str | None:
    if not j:
        return None
    r = j.get("result") or {}
    rel = data_rel(r.get("file") or r.get("clip"))
    host = j.get("host") or j.get("want_host")
    return f"/files/{host}/{rel}" if rel and host else None


def script_eps(root: Path, run: str) -> dict:
    """{ep: {"title", "shots": [shot ids]}}；读不到剧本 → {}（分母退回队列里的镜）"""
    try:
        d = json.loads((root / "data/import/runs" / run / "script_v2.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for e in d.get("episodes") or []:
        ep = int(e.get("number") or 0)
        if ep:
            out[ep] = {"title": e.get("title") or "", "shots": [f"ep{ep:02d}-S{int(s['index'])}" for s in e.get("shots") or [] if s.get("index")]}
    return out


def _t(s: str | None) -> float:
    try:
        return datetime.fromisoformat(s).timestamp() if s else 0.0
    except ValueError:
        return 0.0


def build(run: str, rows: list[dict], reviews: dict, script: dict, now_iso: str) -> dict:
    """rows = 该 run 全部 jobs 行；reviews = {shot: {"state", "reason", "at"}}；script = script_eps()"""
    latest: dict = {}
    for j in sorted(rows, key=lambda j: j["id"]):
        latest.setdefault(j["shot"], {})[j["step"]] = j
    eps = sorted(set(script) | {j["ep"] for j in rows})
    # 分道中位数 ETA
    by_lane: dict = {}
    for j in rows:
        if j["step"] == "video" and j["state"] in ("done", "best_effort") and (j.get("result") or {}).get("secs"):
            by_lane.setdefault(j.get("lane") or j.get("host") or "?", []).append(float(j["result"]["secs"]))
    lane_med = {k: statistics.median(v) for k, v in by_lane.items()}
    all_med = statistics.median([x for v in by_lane.values() for x in v]) if by_lane else ETA_DEFAULT_S
    queued_video = sorted((j for j in rows if j["step"] == "video" and j["state"] == "queued"), key=lambda j: (j["created_at"], j["id"]))
    qpos = {j["id"]: i + 1 for i, j in enumerate(queued_video)}

    V = dict(total=0, final=0, rendering=0, queued=0, pending_review=0, best_effort=0, need_frames=0, todo=0, failed=0, blocked=0)
    T = {"first": [0, 0], "last": [0, 0], "video": [0, 0], "cut": [0, 0]}
    episodes = []
    for ep in eps:
        sids = (script.get(ep) or {}).get("shots") or sorted({j["shot"] for j in rows if j["ep"] == ep and j["step"] != "film"},
                                                             key=lambda s: int(s.split("-S")[1]))
        shots, cnt = [], dict(first=0, last=0, video=0, fallback=0)
        last_video_t = 0.0
        for sid in sids:
            st = latest.get(sid) or {}
            f, l, v = st.get("first"), st.get("last"), st.get("video")
            rv = reviews.get(sid) or {}
            fs = FRAME_STATE.get((f or {}).get("state"), "pending")
            ls = FRAME_STATE.get((l or {}).get("state"), "pending")
            vstate = (v or {}).get("state")
            has_clip = vstate in ("done", "best_effort")
            if has_clip and rv.get("state") == "passed":
                vs = "passed"; V["final"] += 1; cnt["video"] += 1
            elif vstate == "done":
                vs = "ready"; V["pending_review"] += 1
            elif vstate == "best_effort":
                vs = "best_effort"; V["best_effort"] += 1
            elif vstate == "running":
                vs = "running"; V["rendering"] += 1
            elif vstate == "queued":
                vs = "pending"; V["queued"] += 1
            elif vstate == "failed":
                vs = "failed"; V["failed"] += 1
            elif vstate == "blocked":
                vs = "blocked"; V["blocked"] += 1
            elif fs not in ("passed",) or ls not in ("passed", "na"):
                vs = "pending"; V["need_frames"] += 1
            else:
                vs = "pending"; V["todo"] += 1
            if v and v.get("finished_at") and vstate != "queued":
                last_video_t = max(last_video_t, _t(v["finished_at"]))
            cnt["first"] += fs == "passed"
            cnt["last"] += ls in ("passed", "na")
            cnt["fallback"] += ls == "na"
            missing = [k for k, s in (("first", fs), ("last", ls)) if s not in ("passed", "na")] + ([] if has_clip else ["video"])
            lane = (v or {}).get("lane") or (v or {}).get("want_host")
            shots.append({
                "id": sid, "index": int(sid.split("-S")[1]),
                "first": {"state": fs, "url": file_url(f), "n": ((f or {}).get("result") or {}).get("take_n") or ((f or {}).get("result") or {}).get("take"),
                          "error": (f or {}).get("error")},
                "last": {"state": ls, "url": file_url(l), "n": ((l or {}).get("result") or {}).get("take_n") or ((l or {}).get("result") or {}).get("take"),
                         "error": (l or {}).get("error"), **({"fallback": {"mode": "i2va"}} if ls == "na" else {})},
                "video": {"state": vs, "url": file_url(v) if has_clip else None, "n": ((v or {}).get("result") or {}).get("take_n"),
                          "tries": (v or {}).get("tries"), "error": (v or {}).get("error"),
                          **({"queue_pos": qpos[v["id"]], "eta_s": round(qpos[v["id"]] * lane_med.get(lane, all_med))} if v and v["id"] in qpos else {}),
                          **({"eta_s": round(max(0.0, lane_med.get(lane, all_med) - (datetime.now().timestamp() - _t(v.get("started_at")))))}
                             if vstate == "running" else {})},
                "review": rv.get("state"), "review_reason": rv.get("reason"),
                "owner_trigger": any((x or {}).get("owner_trigger") for x in (f, l, v)),
                "stale": [], "stale_global": [], "missing": missing,
                "candidates": sum(1 for j in rows if j["shot"] == sid and j["step"] == "video" and (j.get("result") or {}).get("has_output")),
            })
        film = (latest.get(f"ep{ep:02d}") or {}).get("film")
        films_done = sorted((j for j in rows if j["shot"] == f"ep{ep:02d}" and j["step"] == "film" and j["state"] == "done"), key=lambda j: j["id"])
        fd = films_done[-1] if films_done else None
        assembled = bool(fd and _t(fd.get("finished_at")) >= last_video_t)
        n = len(sids)
        phase = ("frames" if cnt["first"] < n or cnt["last"] < n else
                 "videos" if sum(1 for s in shots if s["video"]["state"] in ("passed", "best_effort", "ready")) < n else
                 "done" if assembled and cnt["video"] == n else "assembly")
        fr = (fd or {}).get("result") or {}
        eprows = [j for j in rows if j["ep"] == ep and not (j.get("result") or {}).get("migrated")]
        starts = [j["started_at"] for j in eprows if j.get("started_at")]
        ends = [j["finished_at"] for j in eprows if j.get("finished_at")]
        episodes.append({
            "ep": ep, "title": (script.get(ep) or {}).get("title") or "", "total": n, "phase": phase,
            "start": min(starts, key=_t) if starts else None, "last_at": max(ends, key=_t) if ends else None,
            "first_done": cnt["first"], "last_done": cnt["last"], "fallback_n": cnt["fallback"], "video_done": cnt["video"],
            "assembled": assembled,
            "film": ({"name": Path(fr["file"]).name, "version": fr.get("version"), "url": file_url(fd), "kind": "preview",
                      "dur_s": fr.get("dur_s"), "script_s": fr.get("script_s"),
                      "missing_n": len(fr.get("placeholders") or []), "missing": fr.get("placeholders") or [],
                      "best_effort": fr.get("best_effort") or []} if fd and fr.get("file") else None),
            "film_job": ({"id": film["id"], "state": film["state"], "error": film.get("error")} if film else None),
            "shots": shots,
        })
        V["total"] += n
        T["first"][0] += cnt["first"]; T["first"][1] += n
        T["last"][0] += cnt["last"]; T["last"][1] += n
        T["video"][0] += cnt["video"]; T["video"][1] += n
        T["cut"][0] += assembled; T["cut"][1] += 1
    left = V["queued"] + V["rendering"] + V["need_frames"] + V["todo"]
    V.update(median_s=round(all_med, 1), eta_all_s=round(left * all_med / max(1, len(lane_med) or 1)))
    return {"run": run, "at": now_iso, "totals": T, "videos": V, "episodes": episodes}
