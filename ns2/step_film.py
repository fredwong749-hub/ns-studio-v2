"""M2 整集合成步 film v1（PLAN 决策：v1 不带配音 / BGM，只把各镜原片按镜号顺序接成一条可看的整集）。

在本集视频机上跑（片子都在本机）。每镜取片顺序（与旧 night.finalize / assemble.find_source 同口径，L-204 D8 不挡整集）：
  1. takes.json 当前视频版（done；视频步最终失败时也已把罚分最低的一版设为当前）
  2. 队列结果里记的片（迁移来的尽力版只在 state_<host>.json：result.file / result.clip）
  3. 首帧已过门 → 首帧静帧 STILL_S 秒占位；首帧没过门（含待项目方看的成人镜）→ 黑场占位（不拿未过门的图进片）
输出 data/episodes/<run>/ns2/epNN_vN.mp4（N 递增，不覆盖；与旧 epNN_vN 分目录）+ 同名 .json（每镜来源 / 时长 vs 剧本 / 占位原因）；响度归一 -14 LUFS。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from ns2.step_video import FFMPEG, ROOT

W, H, FPS, SR = 720, 1280, 24, 48000
STILL_S = 3.0          # 剧本里查不到时长时的占位秒数
LUFS = -14.0           # PLAN §2.6：v1 响度归一 -14 LUFS


def script_durations(run: str, ep: int, root: Path) -> dict:
    """{shot: 剧本秒数}（data/import/runs/<run>/script_v2.json）；读不到 → {}"""
    try:
        d = json.loads((root / "data/import/runs" / run / "script_v2.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    e = next((e for e in d.get("episodes") or [] if int(e.get("number") or 0) == ep), None)
    return {f"ep{ep:02d}-S{int(s['index'])}": float(s.get("duration") or 0) for s in (e or {}).get("shots") or [] if s.get("index")}


def api_jobs(run: str) -> list[dict]:
    api = os.environ.get("NS2_API", "http://10.0.0.33:8900")
    from ns2 import auth as AU
    req = urllib.request.Request(f"{api.rstrip('/')}/api/jobs?run={run}&limit=100000", headers=AU.headers())
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def latest(rows: list[dict], ep: int) -> dict:
    """{shot: {step: 最新行}}，只取本集的镜（不含 film 行）"""
    out: dict = {}
    for j in sorted(rows, key=lambda j: j["id"]):
        if j["ep"] == ep and j["step"] in ("first", "last", "video"):
            out.setdefault(j["shot"], {})[j["step"]] = j
    return dict(sorted(out.items(), key=lambda kv: int(kv[0].split("-S")[1])))


def _active(run: str, sid: str, stage: str, root: Path) -> str | None:
    ep, s = sid.split("-")
    tj = root / "data/renders" / run / ep / s / "takes.json"
    try:
        d = json.loads(tj.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    n = (d.get("active") or {}).get(stage)
    t = next((x for x in d.get("takes") or [] if x.get("stage") == stage and x.get("n") == n), None)
    return f"data/{t['file']}" if t and t.get("file") else None


def _exists(rel: str | None, root: Path) -> str | None:
    if not rel:
        return None
    for p in (root / rel, root / "data" / rel):
        if p.is_file():
            return str(p.relative_to(root)).replace("\\", "/")
    return None


def pick_sources(run: str, ep: int, rows: list[dict], root: Path = ROOT) -> list[dict]:
    out = []
    for sid, st in latest(rows, ep).items():
        v, f = st.get("video") or {}, st.get("first") or {}
        res = v.get("result") or {}
        src = None
        if v.get("state") in ("done", "best_effort"):
            src = _exists(_active(run, sid, "video", root), root) or _exists(res.get("file"), root) or _exists(res.get("clip"), root)
        if src:
            out.append({"shot": sid, "kind": "video", "path": src, "video_state": v["state"]})
            continue
        why = f"视频{v.get('state') or '未入队'}" + (f"：{(v.get('error') or '')[:80]}" if v.get("error") else "")
        still = _exists(_active(run, sid, "image", root), root) if f.get("state") == "done" else None
        out.append({"shot": sid, "kind": "still" if still else "black", "path": still, "missing": True,
                    "missing_reason": why + ("" if still or f.get("state") == "done" else f"；首帧{f.get('state') or '未入队'}，黑场占位")})
    return out


def _ff(host: str) -> str:
    d = FFMPEG.get(host)
    return str(Path(d) / "ffmpeg") if d and (Path(d) / "ffmpeg").exists() else "ffmpeg"


def _has_audio(ff: str, path: Path) -> bool:
    r = subprocess.run([ff, "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    return bool(re.search(r"Stream #\S+.*Audio:", r.stderr or ""))


def duration(ff: str, path: Path) -> float | None:
    r = subprocess.run([ff, "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr or "")
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else None


def render_segment(ff: str, src: dict, out: Path, root: Path) -> None:
    vf = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS},format=yuv420p"
    enc = ["-threads", "8", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "160k", "-ar", str(SR), "-ac", "2"]
    silent = ["-f", "lavfi", "-i", f"anullsrc=r={SR}:cl=stereo"]
    if src["kind"] == "video":
        p = root / src["path"]
        if _has_audio(ff, p):
            d = duration(ff, p)           # 声轨补齐到片长（-shortest 配 apad 实测会按声轨截短），拼接不累积音画漂移
            cmd = [ff, "-y", "-i", str(p), "-vf", vf, "-af", "apad", *(["-t", f"{d:.3f}"] if d else ["-shortest"]), *enc, str(out)]
        else:
            cmd = [ff, "-y", "-i", str(p), *silent, "-map", "0:v", "-map", "1:a", "-shortest", "-vf", vf, *enc, str(out)]
    elif src["kind"] == "still":
        t = str(src.get("script_s") or STILL_S)
        cmd = [ff, "-y", "-loop", "1", "-t", t, "-i", str(root / src["path"]), *silent, "-map", "0:v", "-map", "1:a",
               "-t", t, "-vf", vf, *enc, str(out)]
    else:
        t = str(src.get("script_s") or STILL_S)
        cmd = [ff, "-y", "-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={FPS}:d={t}", *silent, "-map", "0:v", "-map", "1:a",
               "-t", t, "-vf", vf, *enc, str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode or not out.is_file():
        raise RuntimeError(f"{src['shot']} 段渲染失败 rc={r.returncode}：{(r.stderr or '')[-400:]}")


def run(job: dict, lane: str, rows: list[dict] | None = None, root: Path = ROOT):
    run_id, ep = job["run_id"], int(job["ep"])
    host = lane.split(":")[0]
    srcs = pick_sources(run_id, ep, rows if rows is not None else api_jobs(run_id), root)
    if not srcs:
        return "blocked", None, f"ep{ep:02d} 队列里没有镜"
    if not any(s["kind"] == "video" for s in srcs):
        return "blocked", {"shots": srcs}, "整集一条视频都没有，不出全占位片"
    ff = _ff(host)
    sd = script_durations(run_id, ep, root)
    for s in srcs:
        s["script_s"] = sd.get(s["shot"])
    outdir = root / "data/episodes" / run_id / "ns2"          # 新系统成片单独目录，版本号不和旧 epNN_vN 撞
    outdir.mkdir(parents=True, exist_ok=True)
    vers = [int(m[1]) for p in outdir.glob(f"ep{ep:02d}_v*.mp4") if (m := re.match(rf"ep{ep:02d}_v(\d+)\.mp4$", p.name))]
    ver = 1 + max(vers + [0])
    final = outdir / f"ep{ep:02d}_v{ver}.mp4"
    with tempfile.TemporaryDirectory(prefix=f"ns2film_{ep:02d}_") as td:
        segs = []
        for i, s in enumerate(srcs):
            seg = Path(td) / f"{i:03d}.mp4"
            render_segment(ff, s, seg, root)
            s["dur_s"] = round(duration(ff, seg) or 0, 2)
            segs.append(seg)
        lst = Path(td) / "list.txt"
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in segs), encoding="utf-8")
        cat = Path(td) / "cat.mp4"
        r = subprocess.run([ff, "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(cat)],
                           capture_output=True, text=True, timeout=900)
        if r.returncode or not cat.is_file():
            raise RuntimeError(f"拼接失败 rc={r.returncode}：{(r.stderr or '')[-400:]}")
        r = subprocess.run([ff, "-y", "-i", str(cat), "-c:v", "copy", "-af", f"loudnorm=I={LUFS}:TP=-1.5:LRA=11",
                            "-c:a", "aac", "-b:a", "160k", "-ar", str(SR), "-movflags", "+faststart", str(final)],
                           capture_output=True, text=True, timeout=900)
        if r.returncode or not final.is_file():
            raise RuntimeError(f"响度归一失败 rc={r.returncode}：{(r.stderr or '')[-400:]}")
    total = round(duration(ff, final) or 0, 2)
    script_s = round(sum(sd.values()), 2) if sd else None
    meta = {"run": run_id, "ep": ep, "job": job["id"], "version": ver, "size": [W, H], "fps": FPS, "lufs": LUFS,
            "tts": False, "bgm": False, "dur_s": total, "script_s": script_s, "shots": srcs}
    final.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    ph = [s["shot"] for s in srcs if s.get("missing")]
    off = [f"{s['shot']} {s['dur_s']}/{s['script_s']}" for s in srcs if s.get("script_s") and abs(s["dur_s"] - s["script_s"]) > 0.5]
    return "done", {"file": str(final.relative_to(root)).replace("\\", "/"), "version": ver, "shots": len(srcs), "placeholders": ph,
                    "best_effort": [s["shot"] for s in srcs if s.get("video_state") == "best_effort"],
                    "dur_s": total, "script_s": script_s, "delta_s": round(total - script_s, 2) if script_s else None,
                    "off_script": off, "has_output": True}, None
