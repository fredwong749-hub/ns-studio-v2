"""R4 质量：一集（或几集）的出图 / 视频质量账（只读，离线；11-10 项目方“S8 S13 先接受占位，开始做质量提升”）。

每轮质量改动前后各跑一次，同一口径对比：
  - 首 / 尾帧：每阶段出了几版、几版过；每镜第一版过没过（首帧一次过率 = R4 指标）；没过的项按次数排（Q1 身份 / FL 首尾太像 / G1 看镜头 …）
  - Q1 身份：按角色 × 判定（过 / 复核带 / 不像）列相似度分布——分清“门太严”（复核带多）与“脸真不像”（不像多）
  - FL：尾帧与首帧的构图相关 / SSIM 分布
  - 视频：video/scores/*.json 的动作量（mean）/ 静止占比 / 门过没过

读：<renders>/<run>/ep0N/S*/takes.json（postflight 原样，不过尽力版读视图——要量的就是原始过没过）；
    <pipeline>/runs/<run>/video/scores/<镜>__<变体>.json。
用法：python -m ns2.quality_report --root <旧部署 repo> --run series10_v2 --ep 5 [--ep 1 ...] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path


def _q(xs: list[float]) -> dict:
    """分位小结（空 = {}）"""
    if not xs:
        return {}
    s = sorted(xs)
    at = lambda p: s[min(len(s) - 1, int(p * len(s)))]          # noqa: E731
    return {"n": len(s), "min": round(s[0], 3), "p25": round(at(.25), 3), "med": round(statistics.median(s), 3),
            "p75": round(at(.75), 3), "max": round(s[-1], 3)}


def frame_stats(docs: dict[str, dict]) -> dict:
    """docs = {镜号: takes.json 内容}。→ 每阶段（image / last）的过率、一次过、没过项排行、Q1 / FL 分布"""
    out: dict = {}
    tot, ok = Counter(), Counter()
    fails: dict[str, Counter] = defaultdict(Counter)
    first_fails: dict[str, Counter] = defaultdict(Counter)
    first_n, first_ok = Counter(), Counter()
    first_by_shot: dict[str, dict] = defaultdict(dict)
    titles: dict[str, str] = {}
    q1: dict[tuple, list] = defaultdict(list)
    fl: dict[str, list] = defaultdict(list)
    for sid, d in sorted(docs.items()):
        seen = set()
        for t in sorted((d or {}).get("takes") or [], key=lambda t: t.get("n") or 0):
            pf, st = t.get("postflight"), t.get("stage")
            if not isinstance(pf, dict) or st not in ("image", "last"):
                continue
            tot[st] += 1
            bad = []
            for it in pf.get("items") or []:
                iid = it.get("id")
                titles.setdefault(iid, it.get("title") or "")
                if it.get("status") == "fail":
                    bad.append(iid)
                    fails[st][iid] += 1
                ev = it.get("evidence") or {}
                if iid == "Q1":
                    for f in ev.get("per_face") or []:
                        if f.get("sim") is not None:
                            q1[(f.get("as"), f.get("verdict"))].append(float(f["sim"]))
                if iid == "FL" and ev.get("struct") is not None:
                    fl["struct"].append(float(ev["struct"]))
                    fl["ssim"].append(float(ev.get("ssim") or 0))
            if not bad:
                ok[st] += 1
            if st not in seen:
                seen.add(st)
                first_n[st] += 1
                first_by_shot[st][sid] = not bad
                if bad:
                    first_fails[st].update(bad)
                else:
                    first_ok[st] += 1
    for st in tot:
        out[st] = {"takes": tot[st], "pass": ok[st], "pass_rate": round(ok[st] / tot[st], 3),
                   "shots": first_n[st], "first_try_pass": first_ok[st],
                   "first_try_rate": round(first_ok[st] / first_n[st], 3) if first_n[st] else None,
                   "first_try_by_shot": first_by_shot[st],
                   "fails": [{"id": k, "n": v, "share": round(v / tot[st], 3), "first_try": first_fails[st][k],
                              "title": titles.get(k, "")} for k, v in fails[st].most_common()]}
    out["q1"] = [{"who": who, "verdict": vd, **_q(v)} for (who, vd), v in sorted(q1.items(), key=lambda kv: -len(kv[1]))]
    out["fl"] = {k: _q(v) for k, v in fl.items()}
    return out


def video_stats(scores: dict[str, dict]) -> dict:
    """scores = {文件名: score_clip JSON}。→ 动作量 / 静止占比分布、门过率、没过原因排行"""
    mean, still, ok, n = [], [], 0, 0
    why = Counter()
    for _, s in sorted(scores.items()):
        if not isinstance(s, dict) or s.get("mean") is None:
            continue
        n += 1
        mean.append(float(s["mean"]))
        if s.get("still") is not None:
            still.append(float(s["still"]))
        g = s.get("gate") or {}
        ok += bool(g.get("pass"))
        why.update(g.get("fails") or [])
    return {"clips": n, "gate_pass": ok, "motion_mean": _q(mean), "still_frac": _q(still),
            "low_motion_lt_1": sum(1 for m in mean if m < 1.0), "fails": dict(why.most_common())}


def load(root: Path, run: str, eps: list[int]) -> tuple[dict, dict]:
    docs, scores = {}, {}
    for ep in eps:
        for p in sorted((root / "data/renders" / run / f"ep{ep:02d}").glob("S*/takes.json")):
            try:
                docs[f"ep{ep:02d}-{p.parent.name}"] = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
        for p in sorted((root / "data/pipeline/runs" / run / "video/scores").glob(f"ep{ep:02d}-S*.json")):
            try:
                scores[p.name] = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
    return docs, scores


def report(root: Path, run: str, eps: list[int]) -> dict:
    docs, scores = load(root, run, eps)
    return {"run": run, "eps": eps, "shots": len(docs), "frames": frame_stats(docs), "video": video_stats(scores)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--run", required=True)
    ap.add_argument("--ep", type=int, action="append", required=True)
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    r = report(Path(a.root), a.run, a.ep)
    s = json.dumps(r, ensure_ascii=False, indent=1)
    if a.json:
        Path(a.json).write_text(s, encoding="utf-8")
    print(s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
