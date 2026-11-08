"""FIX（ep02_iter v0 R1，DIR 原型 tools/ep02_director/skin_guard_proto.py 的生产版）：林晚脸颊 a* 异常块限幅。

问题（docs/ep02_iter/v0_baseline_review.md §3）：H3 视频（尤其 Pika Flex、只给首帧）把提示词里的 flush 描述画成两块
饱和圆形红斑，脸颊 LAB a*p95 0.8 s 内从 19 跳到 40。后期没有加红；这是事后限幅，不改生成。

做法（只动 a*，L*/b* 不动）：
1. YuNet 找脸（跟踪分辨率 360×640），浅蓝发得分最高且 ≥ hair_min 的那张 = 林晚（同 DIR review.py 规则）；
2. 阈值 T = min(第一张检到林晚的帧的脸颊带（脸框宽 10–90 %、高 50–80 %）a*p95, ref_cap_a) + margin；
3. 异常块 = 脸框外扩 box_expand 范围内 a* > T 的连通块，面积 ≥ min_blob_frac × 脸框面积（零星像素不动）；
4. 嘴唇排除：YuNet 嘴角关键点 → 椭圆（半宽 = 嘴角距 × lips_w，半高 = 嘴角距 × lips_h），羽化后从权重里扣掉；
5. a*' = a* − w × max(a* − T, 0) × (1 − knee)（超出部分只保留 knee）；权重高斯羽化，边缘无硬边。
输出 <画面>_sg.mp4（bt709 矩阵 + bt709 标记，crf 14，只有画面），旁边 _sg.json 记设置、源文件 sha256、逐帧统计；
同设置同源文件复用。参数在 config/render_quality.json post.skin_guard（缺键用 DEFAULTS）。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(os.environ.get("NS_PROJECT_ROOT") or Path(__file__).resolve().parents[3])
QUALITY_CONFIG = ROOT / "config" / "render_quality.json"
VERSION = "skin_guard_v1"
DEFAULTS = {"margin": 6.0, "ref_cap_a": 22.0, "knee": 0.25, "box_expand": 0.25, "exclude_lips": True, "lips_w": 0.75, "lips_h": 0.45,
            "min_blob_frac": 0.005, "hair_min": 0.35, "track_w": 360, "track_h": 640, "crf": 14}
SETTING_KEYS = tuple(DEFAULTS)


def settings(config: Path | None = None, **over) -> dict:
    try:
        cfg = (json.loads(Path(config or QUALITY_CONFIG).read_text(encoding="utf-8")).get("post") or {}).get(
            "skin_guard") or {}
    except (OSError, ValueError, AttributeError):
        cfg = {}
    out = {k: cfg.get(k, v) for k, v in DEFAULTS.items()}
    out.update({k: v for k, v in over.items() if k in DEFAULTS and v is not None})
    return out


# ---------------------------------------------------------------- 纯函数（可单测）

def lab_of(rgb: np.ndarray) -> np.ndarray:
    import cv2
    return cv2.cvtColor(rgb.astype(np.float32) / 255.0, cv2.COLOR_RGB2LAB)


def cheek_box(face, shape) -> tuple[int, int, int, int] | None:
    x, y, w, h = (float(v) for v in face[:4])
    H, W = shape[:2]
    x0, x1 = int(max(0, x + 0.10 * w)), int(min(W, x + 0.90 * w))
    y0, y1 = int(max(0, y + 0.50 * h)), int(min(H, y + 0.80 * h))
    return (x0, y0, x1, y1) if x1 - x0 >= 4 and y1 - y0 >= 3 else None


def cheek_a_p95(rgb: np.ndarray, face) -> float | None:
    b = cheek_box(face, rgb.shape)
    if b is None:
        return None
    x0, y0, x1, y1 = b
    return float(np.percentile(lab_of(rgb[y0:y1, x0:x1])[..., 1], 95))


def lips_mask(face, shape, lips_w: float, lips_h: float) -> np.ndarray:
    """YuNet 行：x y w h, 右眼, 左眼, 鼻尖, 右嘴角(10,11), 左嘴角(12,13), score → 嘴唇椭圆（羽化，0–1）"""
    import cv2
    H, W = shape[:2]
    m = np.zeros((H, W), np.float32)
    f = [float(v) for v in face]
    if len(f) >= 14:
        (rx, ry), (lx, ly) = (f[10], f[11]), (f[12], f[13])
        d = max(math.hypot(lx - rx, ly - ry), 0.12 * f[2])
        cx, cy = (rx + lx) / 2, (ry + ly) / 2 + 0.08 * d
        ang = math.degrees(math.atan2(ly - ry, lx - rx))
    else:   # 没有关键点：脸框下部的几何位置
        d = 0.38 * f[2]
        cx, cy, ang = f[0] + 0.5 * f[2], f[1] + 0.80 * f[3], 0.0
    cv2.ellipse(m, (int(round(cx)), int(round(cy))), (max(1, int(d * lips_w)), max(1, int(d * lips_h))), ang, 0, 360,
                1.0, -1)
    return cv2.GaussianBlur(m, (0, 0), max(1.0, 0.15 * d))


def guard_weight(lab: np.ndarray, face, T: float, s: dict) -> np.ndarray:
    """本帧要压的权重（0–1）：外扩脸框内 a* > T 的大连通块，羽化，扣掉嘴唇"""
    import cv2
    H, W = lab.shape[:2]
    x, y, w, h = (float(v) for v in face[:4])
    e = float(s["box_expand"])
    x0, x1 = int(max(0, x - e * w)), int(min(W, x + (1 + e) * w))
    y0, y1 = int(max(0, y - 0.4 * e * h)), int(min(H, y + (1 + 1.8 * e) * h))
    wgt = np.zeros((H, W), np.float32)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return wgt
    hot = (lab[y0:y1, x0:x1, 1] > T).astype(np.uint8)
    if hot.any():
        n, lbl, st, _ = cv2.connectedComponentsWithStats(hot, connectivity=8)
        min_area = float(s["min_blob_frac"]) * w * h
        keep = np.zeros(n, bool)
        keep[1:] = st[1:, cv2.CC_STAT_AREA] >= min_area
        wgt[y0:y1, x0:x1] = keep[lbl].astype(np.float32)
    sig = max(1.0, 0.04 * w)
    wgt = cv2.GaussianBlur(cv2.dilate(wgt, np.ones((3, 3), np.uint8), iterations=max(1, int(sig))), (0, 0), sig)
    if s.get("exclude_lips"):
        wgt *= 1.0 - lips_mask(face, (H, W), float(s["lips_w"]), float(s["lips_h"]))
    return np.clip(wgt, 0, 1)


def guard_frame(rgb: np.ndarray, face, T: float, s: dict) -> tuple[np.ndarray, float]:
    """→ (新 RGB uint8, 被压像素占比)"""
    import cv2
    lab = lab_of(rgb)
    wgt = guard_weight(lab, face, T, s)
    a = lab[..., 1]
    over = np.maximum(a - T, 0)
    lab[..., 1] = a - wgt * over * (1 - float(s["knee"]))
    out = cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)
    return (np.clip(out, 0, 1) * 255 + 0.5).astype(np.uint8), float((wgt * (over > 0) > 0.05).mean())


# ---------------------------------------------------------------- 找林晚

def _blue_share(rgb, x0, y0, x1, y1) -> float:
    import cv2
    if x1 <= x0 or y1 <= y0:
        return 0.0
    hsv = cv2.cvtColor(np.ascontiguousarray(rgb[y0:y1, x0:x1]), cv2.COLOR_RGB2HSV).reshape(-1, 3)
    return float(((hsv[:, 0] >= 85) & (hsv[:, 0] <= 125) & (hsv[:, 1] >= 60) & (hsv[:, 2] >= 40)).mean())


def blue_hair_share(rgb, face) -> float:
    """同 DIR review.py：头顶带 + 两侧各半个脸宽的带里蓝色像素占比（林晚 0.55–1.5，他人 0–0.2）"""
    H, W = rgb.shape[:2]
    x, y, w, h = (int(v) for v in face[:4])
    t = _blue_share(rgb, max(0, x - int(0.2 * w)), max(0, y - int(0.45 * h)), min(W, x + w + int(0.2 * w)),
                    max(1, y + int(0.05 * h)))
    yy0, yy1 = max(0, y), min(H, y + h)
    return t + (_blue_share(rgb, max(0, x - w // 2), yy0, max(0, x), yy1)
                + _blue_share(rgb, min(W, x + w), yy0, min(W, x + w + w // 2), yy1)) / 2


def pick_lin_wan(rgb, faces, hair_min: float):
    if not faces:
        return None
    sh = [blue_hair_share(rgb, f) for f in faces]
    k = int(np.argmax(sh))
    return faces[k] if sh[k] >= hair_min else None


class _Detector:
    def __init__(self):
        try:
            from ns_qc import face as QF
        except ImportError:
            sys.path.insert(0, str(ROOT / "packages" / "qc"))
            from ns_qc import face as QF
        paths = QF.find_models(ROOT)
        if not paths:
            raise RuntimeError("models_cache/vision 里没有 YuNet / SFace")
        self.m = QF.FaceModels(*paths, score=0.5)

    def faces(self, rgb):
        import cv2
        return [f for f in self.m.faces(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)) if float(f[3]) >= 30]


# ---------------------------------------------------------------- 视频

def _probe(path: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,color_space,r_frame_rate", "-of", "json", str(path)],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)   # K-OPS-S01
    return (json.loads(r.stdout or "{}").get("streams") or [{}])[0]


def _matrix(v: dict) -> str:
    cs = (v.get("color_space") or "").lower()
    return "bt601" if cs in ("smpte170m", "bt470bg", "bt601") else "bt709"


def decode(path: Path, w: int, h: int, matrix: str) -> np.ndarray:
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-map", "0:v:0", "-vf",
                        f"scale={w}:{h}:flags=area:in_color_matrix={matrix}:in_range=tv", "-f", "rawvideo",
                        "-pix_fmt", "rgb24", "-"], capture_output=True, timeout=600)   # K-OPS-S01
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-300:].decode("utf-8", "replace"))
    return np.frombuffer(r.stdout, np.uint8).reshape(-1, h, w, 3)


def file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def sg_path(clip: Path) -> Path:
    return clip.with_name(clip.stem + "_sg.mp4")


def guard_video(src: Path, dst: Path, s: dict | None = None, log=print) -> dict:
    s = s or settings()
    v = _probe(src)
    W, H = int(v["width"]), int(v["height"])
    num, _, den = (v.get("r_frame_rate") or "24/1").partition("/")
    fps = float(num) / float(den or 1)
    mtx = _matrix(v)
    full = decode(src, W, H, mtx)
    tw, th = int(s["track_w"]), int(s["track_h"])
    small = decode(src, tw, th, mtx)
    det = _Detector()
    sx, sy = W / tw, H / th
    T, last, guarded, per = None, None, 0, []
    out = []
    for f, sm in zip(full, small):
        fc = pick_lin_wan(sm, det.faces(sm), float(s["hair_min"]))
        last = fc if fc is not None else last
        if last is None:
            out.append(f)
            per.append(0.0)
            continue
        face = np.array([float(last[k]) * (sx if k % 2 == 0 else sy) for k in range(min(14, len(last)))], np.float32)
        if T is None:
            # 首帧本身偏红（S4 image_003 起点 27.9）时不拿它当基准：基准封顶 ref_cap_a（正常皮肤 a*p95 15–20）
            T = min(cheek_a_p95(f, face) or 20.0, float(s["ref_cap_a"])) + float(s["margin"])
        g, frac = guard_frame(f, face, T, s)
        guarded += frac > 0
        per.append(round(frac, 4))
        out.append(g)
    dst.parent.mkdir(parents=True, exist_ok=True)
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
                            "-r", f"{fps:g}", "-i", "-", "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,"
                            "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709",
                            "-c:v", "libx264", "-crf", str(int(s["crf"])), "-colorspace", "bt709",
                            "-color_primaries", "bt709", "-color_trc", "bt709", str(dst)], stdin=subprocess.PIPE)
    for f in out:
        enc.stdin.write(np.ascontiguousarray(f).tobytes())
    enc.stdin.close()
    enc.wait()
    if enc.returncode != 0:
        raise RuntimeError(f"skin guard 编码失败 {dst}")
    rep = {"version": VERSION, "file": str(dst), "source": str(src), "source_sha256": file_sha256(src),
           "settings": {k: s[k] for k in SETTING_KEYS}, "threshold_a": None if T is None else round(T, 2),
           "frames": len(out), "frames_guarded": int(guarded), "guarded_frac": per, "decode_matrix": mtx}
    dst.with_suffix(".json").write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
    log(f"skin guard {src.name}: T={rep['threshold_a']} 压了 {guarded}/{len(out)} 帧 → {dst.name}")
    return rep


def ensure_sg(clip: Path, *, s: dict | None = None, log=print) -> tuple[Path | None, dict]:
    """同设置 + 同源 sha256 → 复用 <clip>_sg.mp4；否则重做。没检到林晚（T 为 None）→ (None, 报告)，用原画面"""
    s = s or settings()
    out = sg_path(Path(clip))
    js = out.with_suffix(".json")
    if out.is_file() and js.is_file():
        try:
            rep = json.loads(js.read_text(encoding="utf-8"))
            if rep.get("version") == VERSION and rep.get("settings") == {k: s[k] for k in SETTING_KEYS} \
                    and rep.get("source_sha256") == file_sha256(Path(clip)):
                return (out if rep.get("threshold_a") is not None else None), rep
        except (OSError, ValueError):
            pass
    rep = guard_video(Path(clip), out, s, log=log)
    return (out if rep.get("threshold_a") is not None else None), rep


if __name__ == "__main__":
    a = sys.argv[1:]
    r = guard_video(Path(a[0]), Path(a[1]), settings())
    print(json.dumps({k: v for k, v in r.items() if k != "guarded_frac"}, ensure_ascii=False))
