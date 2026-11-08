"""Media I/O and colour/structure maths shared by the gates (ffmpeg subprocess + numpy + OpenCV)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np

H3_SIZE = (768, 1344)  # (w, h) — rule_ids P-B3


# ---------------- binaries ----------------

def ffmpeg_bin() -> str:
    b = os.environ.get("NS_FFMPEG") or shutil.which("ffmpeg")
    if not b:
        raise RuntimeError("ffmpeg not found (set NS_FFMPEG)")
    return b


def ffprobe_bin() -> str:
    b = os.environ.get("NS_FFPROBE") or shutil.which("ffprobe")
    if not b:
        f = Path(ffmpeg_bin())
        cand = f.with_name(f.name.replace("ffmpeg", "ffprobe"))
        if cand.exists():
            return str(cand)
        raise RuntimeError("ffprobe not found (set NS_FFPROBE)")
    return b


def probe(path: Path) -> dict:
    r = subprocess.run([ffprobe_bin(), "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                        str(path)], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe {path}: {r.stderr[-300:]}")
    return json.loads(r.stdout or "{}")


def video_info(path: Path) -> dict:
    p = probe(path)
    v = next((s for s in p.get("streams", []) if s.get("codec_type") == "video"), None)
    if v is None:
        raise RuntimeError(f"{path}: no video stream")
    num, _, den = (v.get("r_frame_rate") or "24/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 24.0
    dur = float(v.get("duration") or p.get("format", {}).get("duration") or 0)
    n = int(v["nb_frames"]) if str(v.get("nb_frames", "")).isdigit() else int(round(dur * fps))
    has_audio = any(s.get("codec_type") == "audio" for s in p.get("streams", []))
    return {"width": int(v["width"]), "height": int(v["height"]), "fps": fps, "frames": n, "duration": dur,
            "color_space": v.get("color_space"), "has_audio": has_audio}


def _matrix(info: dict) -> str:
    return "bt709" if info.get("color_space") == "bt709" else "bt601"


def decode_indices(path: Path, indices: list[int], info: dict | None = None) -> dict[int, np.ndarray]:
    """Full-resolution RGB frames at the given 0-based frame indices, one ffmpeg pass (select filter)."""
    info = info or video_info(path)
    idx = sorted({max(0, min(int(i), info["frames"] - 1)) for i in indices})
    if not idx:
        return {}
    w, h = info["width"], info["height"]
    expr = "+".join(f"eq(n\\,{i})" for i in idx)
    cmd = [ffmpeg_bin(), "-v", "error", "-nostdin", "-i", str(path), "-map", "0:v:0",
           "-vf", f"select='{expr}',scale=in_color_matrix={_matrix(info)}", "-fps_mode", "passthrough",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"decode {path}: {r.stderr.decode('utf-8', 'replace')[-300:]}")
    size = w * h * 3
    frames = [np.frombuffer(r.stdout[k:k + size], np.uint8).reshape(h, w, 3)
              for k in range(0, len(r.stdout) - size + 1, size)]
    out = dict(zip(idx, frames))
    if idx[-1] not in out and frames:  # nb_frames metadata was optimistic: the last decoded frame stands in
        out[idx[-1]] = frames[-1]
    return out


def last_frame(path: Path, info: dict | None = None) -> np.ndarray | None:
    """The really-last decoded frame (decode only the final second)."""
    info = info or video_info(path)
    w, h = info["width"], info["height"]
    cmd = [ffmpeg_bin(), "-v", "error", "-nostdin", "-sseof", "-1.0", "-i", str(path), "-map", "0:v:0",
           "-vf", f"scale=in_color_matrix={_matrix(info)}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=300)
    size = w * h * 3
    if r.returncode != 0 or len(r.stdout) < size:
        return None
    k = (len(r.stdout) // size - 1) * size
    return np.frombuffer(r.stdout[k:k + size], np.uint8).reshape(h, w, 3)


def decode_small(path: Path, width: int = 192, info: dict | None = None) -> list[np.ndarray]:
    """Every frame, downscaled (area) to `width` — for whole-clip colour statistics."""
    info = info or video_info(path)
    w, h = info["width"], info["height"]
    ow, oh = width, max(2, round(h * width / w / 2) * 2)
    cmd = [ffmpeg_bin(), "-v", "error", "-nostdin", "-i", str(path), "-map", "0:v:0",
           "-vf", f"scale={ow}:{oh}:flags=area:in_color_matrix={_matrix(info)}", "-f", "rawvideo",
           "-pix_fmt", "rgb24", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"decode {path}: {r.stderr.decode('utf-8', 'replace')[-300:]}")
    size = ow * oh * 3
    return [np.frombuffer(r.stdout[k:k + size], np.uint8).reshape(oh, ow, 3)
            for k in range(0, len(r.stdout) - size + 1, size)]


def decode_audio(path: Path, sr: int = 16000, stream: str = "0:a:0") -> np.ndarray | None:
    """Mono float32 PCM at `sr` Hz, or None when the file has no audio."""
    cmd = [ffmpeg_bin(), "-v", "error", "-nostdin", "-i", str(path), "-map", stream, "-ac", "1", "-ar", str(sr),
           "-f", "f32le", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=300)
    if r.returncode != 0 or not r.stdout:
        return None
    return np.frombuffer(r.stdout, np.float32).copy()


def ebur128(path: Path) -> dict:
    """Integrated loudness (LUFS) and true peak (dBTP) via ffmpeg's ITU-R BS.1770 meter."""
    r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-nostdin", "-i", str(path), "-map", "0:a:0",
                        "-af", "ebur128=peak=true", "-f", "null", "-"], capture_output=True, text=True, timeout=300)
    txt = r.stderr
    tail = txt[txt.rfind("Summary:"):] if "Summary:" in txt else txt
    mi = re.search(r"I:\s+(-?[\d.]+|-inf)\s+LUFS", tail)
    mp = re.search(r"Peak:\s+(-?[\d.]+|-inf)\s+dBFS", tail)
    f = lambda m: (None if not m else (-120.0 if m.group(1) == "-inf" else float(m.group(1))))  # noqa: E731
    return {"lufs": f(mi), "true_peak_dbtp": f(mp), "method": "ffmpeg ebur128 (BS.1770)"}


def rms_lufs_approx(x: np.ndarray) -> float:
    """Fallback when ffmpeg's meter is unavailable: ungated, un-weighted −0.691 + 10·log10(mean square)."""
    ms = float(np.mean(np.square(x.astype(np.float64)))) if x.size else 0.0
    return -120.0 if ms <= 0 else round(-0.691 + 10 * np.log10(ms), 2)


# ---------------- files ----------------

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_rgb(path: Path) -> np.ndarray:
    from PIL import Image
    with Image.open(path) as im:
        return np.asarray(im.convert("RGB")).copy()


def h3_geometry(rgb: np.ndarray, size: tuple[int, int] = H3_SIZE) -> np.ndarray:
    """Same preparation as gpu3 h3_keyframe: centre-crop to the target aspect, then Lanczos to `size` (w, h)."""
    import cv2
    w, h = size
    H, W = rgb.shape[:2]
    target = w / h
    if W / H > target:  # too wide → crop width
        cw = int(round(H * target))
        x0 = (W - cw) // 2
        rgb = rgb[:, x0:x0 + cw]
    else:
        ch = int(round(W / target))
        y0 = (H - ch) // 2
        rgb = rgb[y0:y0 + ch]
    return np.ascontiguousarray(cv2.resize(rgb, (w, h), interpolation=cv2.INTER_LANCZOS4))


# ---------------- similarity ----------------

def psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2))
    return 99.0 if mse <= 1e-10 else round(10 * np.log10(255.0 ** 2 / mse), 3)


def ssim(a: np.ndarray, b: np.ndarray) -> float:
    """Mean SSIM on luma (Wang et al. 2004: Gaussian 11×11, σ 1.5, K1 0.01, K2 0.03)."""
    import cv2
    ga = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY).astype(np.float64) if a.ndim == 3 else a.astype(np.float64)
    gb = cv2.cvtColor(b, cv2.COLOR_RGB2GRAY).astype(np.float64) if b.ndim == 3 else b.astype(np.float64)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    blur = lambda x: cv2.GaussianBlur(x, (11, 11), 1.5)  # noqa: E731
    mu_a, mu_b = blur(ga), blur(gb)
    saa = blur(ga * ga) - mu_a ** 2
    sbb = blur(gb * gb) - mu_b ** 2
    sab = blur(ga * gb) - mu_a * mu_b
    m = ((2 * mu_a * mu_b + c1) * (2 * sab + c2)) / ((mu_a ** 2 + mu_b ** 2 + c1) * (saa + sbb + c2))
    return round(float(m.mean()), 4)


def structure_match(a: np.ndarray, b: np.ndarray) -> float:
    """Colour-invariant structure correlation of blurred, downscaled luma (same idea as ns_post.colormatch)."""
    import cv2
    size = (24, 42)
    za = cv2.resize(cv2.cvtColor(a, cv2.COLOR_RGB2GRAY), size, interpolation=cv2.INTER_AREA).astype(np.float64)
    zb = cv2.resize(cv2.cvtColor(b, cv2.COLOR_RGB2GRAY), size, interpolation=cv2.INTER_AREA).astype(np.float64)
    za, zb = za - za.mean(), zb - zb.mean()
    den = np.sqrt((za ** 2).sum() * (zb ** 2).sum())
    return float((za * zb).sum() / den) if den > 1e-9 else 0.0


# ---------------- colour ----------------

def to_lab(rgb: np.ndarray) -> np.ndarray:
    """CIELAB (L 0–100, a/b signed) from RGB uint8."""
    import cv2
    return cv2.cvtColor(rgb.astype(np.float32) / 255.0, cv2.COLOR_RGB2LAB)


_W: dict[tuple, np.ndarray] = {}


def centre_weights(h: int, w: int, sigma: float = 0.33, cy: float = 0.45) -> np.ndarray:
    """Soft Gaussian around the upper centre (where the person usually is) — same proxy as ns_post.colormatch."""
    k = (h, w, sigma, cy)
    if k not in _W:
        y = ((np.arange(h) + 0.5) / h - cy) / sigma
        x = ((np.arange(w) + 0.5) / w - 0.5) / sigma
        wt = np.exp(-0.5 * (y[:, None] ** 2 + x[None, :] ** 2))
        _W[k] = wt / wt.sum()
    return _W[k]


def weighted_lab_mean(rgb: np.ndarray) -> np.ndarray:
    lab = to_lab(rgb).astype(np.float64)
    wt = centre_weights(lab.shape[0], lab.shape[1])
    return np.einsum("hw,hwc->c", wt, lab)


def de2000(lab1, lab2) -> float:
    """CIEDE2000 between two LAB triplets (Sharma, Wu & Dalal 2005 reference formulation)."""
    L1, a1, b1 = (float(v) for v in lab1)
    L2, a2, b2 = (float(v) for v in lab2)
    C1, C2 = np.hypot(a1, b1), np.hypot(a2, b2)
    Cb = (C1 + C2) / 2
    G = 0.5 * (1 - np.sqrt(Cb ** 7 / (Cb ** 7 + 25 ** 7)))
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360 if C1p else 0.0
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360 if C2p else 0.0
    dLp, dCp = L2 - L1, C2p - C1p
    if C1p * C2p == 0:
        dhp = 0.0
    else:
        dhp = h2p - h1p
        if dhp > 180:
            dhp -= 360
        elif dhp < -180:
            dhp += 360
    dHp = 2 * np.sqrt(C1p * C2p) * np.sin(np.radians(dhp / 2))
    Lbp, Cbp = (L1 + L2) / 2, (C1p + C2p) / 2
    if C1p * C2p == 0:
        hbp = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hbp = (h1p + h2p) / 2
    else:
        hbp = (h1p + h2p + 360) / 2 if h1p + h2p < 360 else (h1p + h2p - 360) / 2
    Tt = (1 - 0.17 * np.cos(np.radians(hbp - 30)) + 0.24 * np.cos(np.radians(2 * hbp))
          + 0.32 * np.cos(np.radians(3 * hbp + 6)) - 0.20 * np.cos(np.radians(4 * hbp - 63)))
    dth = 30 * np.exp(-(((hbp - 275) / 25) ** 2))
    Rc = 2 * np.sqrt(Cbp ** 7 / (Cbp ** 7 + 25 ** 7))
    Sl = 1 + 0.015 * (Lbp - 50) ** 2 / np.sqrt(20 + (Lbp - 50) ** 2)
    Sc = 1 + 0.045 * Cbp
    Sh = 1 + 0.015 * Cbp * Tt
    Rt = -np.sin(np.radians(2 * dth)) * Rc
    return float(np.sqrt((dLp / Sl) ** 2 + (dCp / Sc) ** 2 + (dHp / Sh) ** 2 + Rt * (dCp / Sc) * (dHp / Sh)))
