"""
视频运动量质检（fredw-92/QCV，2026-10-31）：ep02 成片里 6/11 镜“只显示图片”，系统没有任何检测拦下来。

- 出视频前（keyframe_diff）：定稿首帧 ↔ 定稿尾帧的平均差。两张几乎一样 → H3 FL2VA 被夹住，视频大概率不动。
  只提示，不拦项目方自己点的出片。
- 出视频后（video_motion）：逐帧 96×168 灰度，相邻帧平均差（0–255）→ mean / p90、静止帧占比、结尾 / 开头定住秒数，
  写进 take.qc["motion"]；verdict = pass | warn | fail，labels 是给画布 / 日志看的中文短标签。
- 阈值按 ep02 现有版本校准（docs/QC_ep02_20261031.md §6）：S1 正常（mean 3.7），S2/3/4/6/7/10 静止（mean 0.15–0.32）。
  config/render_quality.json 的 motion_qc 可覆盖（部分键即可），读不到用下面的默认值。
- 只看像素变化，不看内容；不做内容检测。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

W, H = 96, 168              # 768×1344 竖屏按比例缩小
VERSION = 2

DEFAULTS = {
    "still_diff": 0.35,          # 相邻帧平均差低于它算“静止帧”
    "fail_mean": 0.5,            # 整段平均差低于它 → 静止·不合格
    "fail_p90": 0.9,             # 且 p90 低于它（防止中段一个大动作把 mean 拉高也算不合格：两者都要低才判）
    "fail_min_still": 0.5,       # 且静止帧占比 ≥ 50%（近景对白小动作——一直在动但幅度小——只算偏弱，不判不合格）
    "fail_still_frac": 0.9,      # 或静止帧占比 ≥ 90% → 不合格（S5 82% 中段有大动作，只算 warn）
    "warn_mean": 1.0,            # mean 低于它 → 偏弱（warn）
    "frozen_tail_s": 1.0,        # 结尾连续静止 ≥ 1 s → “结尾定住 Ns”（warn）
    "frozen_head_s": 1.5,        # 开头连续静止 ≥ 1.5 s → “开头定住 Ns”（warn）
    "keyframe_warn": 10.0,       # 首尾帧平均差低于它 → 出片前警告（ep02 静止镜头 4.2–6.8，有动作的 13.4–47.8）
    # RR 2026-10-31：只给首帧时 H3 偶尔中途自己“切镜”（ep02-S2 第 30 帧帧差 62，中位数 0.7，换了场景）。
    # 相邻帧差 ≥ cut_abs 且 ≥ cut_ratio × 中位数 → “中途跳切 Ns”（warn；剧本要切镜时项目方自己判断）
    "cut_abs": 20.0,
    "cut_ratio": 15.0,
}


def thresholds(path: Path | None = None) -> dict:
    """默认值 + config/render_quality.json motion_qc 的覆盖（只取认识的数值键）"""
    out = dict(DEFAULTS)
    if path is None:
        from . import pipeline as P
        path = P.ROOT / "config" / "render_quality.json"
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8")).get("motion_qc") or {}
    except (OSError, ValueError):
        cfg = {}
    for k in DEFAULTS:
        if isinstance(cfg.get(k), (int, float)) and not isinstance(cfg.get(k), bool):
            out[k] = float(cfg[k])
    return out


# ---------------------------------------------------------------- 读帧

def _probe_fps(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=60)   # K-OPS-S01
    try:
        num, den = r.stdout.strip().split("/")
        return float(num) / float(den) or 24.0
    except (ValueError, ZeroDivisionError):
        return 24.0


def video_frames(path: Path) -> np.ndarray:
    """→ (n, H, W) float32 灰度"""
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"scale={W}:{H},format=gray",
                        "-f", "rawvideo", "-"], capture_output=True, timeout=600)   # K-OPS-S01
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 读帧失败：{r.stderr[-400:].decode('utf-8', 'replace')}")
    return np.frombuffer(r.stdout, np.uint8).reshape(-1, H, W).astype(np.float32)


def image_gray(path: Path) -> np.ndarray:
    """图片 → center-crop 到 9:16 再缩到 96×168 灰度（与视频画布同比例）"""
    from PIL import Image
    im = Image.open(path).convert("L")
    sw, sh = im.size
    scale = max(W / sw, H / sh)
    rw, rh = max(W, round(sw * scale)), max(H, round(sh * scale))
    im = im.resize((rw, rh), Image.BILINEAR)
    left, top = (rw - W) // 2, (rh - H) // 2
    return np.asarray(im.crop((left, top, left + W, top + H)), np.float32)


# ---------------------------------------------------------------- 出片前

def keyframe_diff(first: Path, last: Path) -> float:
    return round(float(np.abs(image_gray(first) - image_gray(last)).mean()), 2)


def keyframe_check(first: Path, last: Path, th: dict | None = None) -> dict:
    """→ {"first_last_diff", "threshold", "warn": bool, "message"}；读不到图 → {"error"}"""
    th = th or thresholds()
    try:
        d = keyframe_diff(first, last)
    except Exception as e:  # noqa: BLE001
        return {"error": f"读不到首尾帧：{e}", "warn": False}
    warn = d < th["keyframe_warn"]
    msg = (f"尾帧和首帧几乎一样（平均差 {d:.1f}，低于 {th['keyframe_warn']:g}），视频大概率不动；"
           "建议把尾帧改出明显的动作变化，或试“只用首帧”（last_frame_mode=none）") if warn else None
    return {"first_last_diff": d, "threshold": th["keyframe_warn"], "warn": warn, "message": msg}


# ---------------------------------------------------------------- 出片后

def _run_len(mask: np.ndarray, from_end: bool) -> int:
    seq = mask[::-1] if from_end else mask
    n = 0
    for v in seq:
        if not v:
            break
        n += 1
    return n


def motion_metrics(fr: np.ndarray, fps: float, th: dict | None = None) -> dict:
    th = th or thresholds()
    if len(fr) < 2:
        return {"n": int(len(fr)), "fps": fps, "error": "帧数不足"}
    d = np.abs(np.diff(fr, axis=0)).mean(axis=(1, 2))
    still = d < th["still_diff"]
    return {"n": int(len(fr)), "fps": round(fps, 3), "duration_s": round(len(fr) / fps, 2),
            "mean_diff": round(float(d.mean()), 3), "p90_diff": round(float(np.percentile(d, 90)), 3),
            "max_diff": round(float(d.max()), 2), "still_frac": round(float(still.mean()), 3),
            "frozen_tail_s": round(_run_len(still, True) / fps, 2),
            "frozen_head_s": round(_run_len(still, False) / fps, 2),
            "first_last_diff": round(float(np.abs(fr[-1] - fr[0]).mean()), 2),
            "cut_s": [round(float((i + 1) / fps), 2) for i in np.flatnonzero(
                (d >= th["cut_abs"]) & (d >= th["cut_ratio"] * max(float(np.median(d)), 0.05)))]}


def judge(m: dict, th: dict | None = None) -> dict:
    """指标 → {verdict, labels, reasons}"""
    th = th or thresholds()
    if m.get("error"):
        return {"verdict": "unknown", "labels": ["运动质检失败"], "reasons": [m["error"]]}
    labels, reasons, verdict = [], [], "pass"
    static = (m["mean_diff"] < th["fail_mean"] and m["p90_diff"] < th["fail_p90"]
              and m["still_frac"] >= th["fail_min_still"]) or m["still_frac"] >= th["fail_still_frac"]
    if static:
        verdict = "fail"
        labels.append("静止·不合格")
        reasons.append(f"平均帧差 {m['mean_diff']:.2f}（< {th['fail_mean']:g}）、p90 {m['p90_diff']:.2f}、"
                       f"静止帧 {m['still_frac'] * 100:.0f}%：画面基本不动")
    elif m["mean_diff"] < th["warn_mean"]:
        verdict = "warn"
        labels.append("动作偏弱")
        reasons.append(f"平均帧差 {m['mean_diff']:.2f}（< {th['warn_mean']:g}）")
    if m["frozen_tail_s"] >= th["frozen_tail_s"]:
        labels.append(f"结尾定住 {m['frozen_tail_s']:.1f}s")
        reasons.append(f"最后 {m['frozen_tail_s']:.1f} s 连续静止")
        verdict = "warn" if verdict == "pass" else verdict
    if m["frozen_head_s"] >= th["frozen_head_s"]:
        labels.append(f"开头定住 {m['frozen_head_s']:.1f}s")
        reasons.append(f"开头 {m['frozen_head_s']:.1f} s 连续静止")
        verdict = "warn" if verdict == "pass" else verdict
    for c in m.get("cut_s") or []:
        labels.append(f"中途跳切 {c:.1f}s")
        reasons.append(f"第 {c:.2f} s 画面突变（像切到另一个镜头）")
        verdict = "warn" if verdict == "pass" else verdict
    return {"verdict": verdict, "labels": labels, "reasons": reasons}


def video_motion(path: Path, th: dict | None = None) -> dict:
    """→ take.qc["motion"]：{version, verdict, labels, reasons, metrics, thresholds}；读不到 → verdict unknown"""
    th = th or thresholds()
    try:
        m = motion_metrics(video_frames(Path(path)), _probe_fps(Path(path)), th)
    except Exception as e:  # noqa: BLE001  质检失败不影响出片
        m = {"error": str(e)[:300]}
    return {"version": VERSION, **judge(m, th), "metrics": m,
            "thresholds": {k: th[k] for k in ("still_diff", "fail_mean", "fail_p90", "fail_min_still", "fail_still_frac",
                                              "warn_mean",
                                              "frozen_tail_s", "frozen_head_s")}}


def failed(take: dict | None) -> bool:
    """take 的运动质检 = fail（没质检过 → False）"""
    return ((((take or {}).get("qc") or {}).get("motion") or {}).get("verdict")) == "fail"


def summary(take: dict | None) -> str | None:
    mo = (((take or {}).get("qc") or {}).get("motion") or {})
    if not mo or mo.get("verdict") == "pass":
        return None
    return "、".join(mo.get("labels") or []) or None


def backfill(shot_ids=None, *, force: bool = False, episode: str | None = None) -> list[dict]:
    """给现有视频版本补 qc.motion（已有同版本的跳过，force 重算）。→ [{shot, n, verdict, labels, metrics}]"""
    from . import pipeline as P
    th = thresholds()
    if shot_ids is None:
        shot_ids = []
        for f in sorted(P.RENDERS.glob("*/*/takes.json")):
            ep, s = f.parent.parent.name, f.parent.name
            if episode and ep != episode:
                continue
            shot_ids.append(f"{ep}-{s}")
    out = []
    for sid in shot_ids:
        with P.shot_lock(sid):
            takes = P.load_takes(sid)
            changed = False
            for t in takes.get("takes", []):
                if t.get("stage") != "video":
                    continue
                mo = ((t.get("qc") or {}).get("motion") or {})
                if mo.get("version") == VERSION and not force:
                    out.append({"shot": sid, "n": t["n"], "verdict": mo.get("verdict"), "labels": mo.get("labels"),
                                "metrics": mo.get("metrics"), "skipped": True})
                    continue
                p = P.DATA / t["file"]
                if not p.is_file():
                    continue
                mo = video_motion(p, th)
                t["qc"] = {**(t.get("qc") or {}), "motion": mo}
                changed = True
                out.append({"shot": sid, "n": t["n"], "verdict": mo["verdict"], "labels": mo["labels"],
                            "metrics": mo["metrics"]})
            if changed:
                P._save_takes(sid, takes)
    return out


if __name__ == "__main__":   # 补质检：python -m ns_render.motion_qc [--episode ep02] [--force]
    import argparse
    ap = argparse.ArgumentParser(description="给现有视频版本补运动量质检（take.qc.motion）")
    ap.add_argument("--episode", default=None)
    ap.add_argument("--shot", action="append", default=None)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    for r in backfill(a.shot, force=a.force, episode=a.episode):
        m = r.get("metrics") or {}
        print(f"{r['shot']:10s} v{r['n']:<3} {r['verdict']:7s} mean={m.get('mean_diff')} p90={m.get('p90_diff')} "
              f"still={m.get('still_frac')} tail={m.get('frozen_tail_s')}s {' '.join(r.get('labels') or [])}"
              + ("（已有，跳过）" if r.get("skipped") else ""))
