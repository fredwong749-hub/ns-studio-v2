"""胶片后期（H2a，PLAN_HOLLYWOOD_LOOK §3）：高光软拐点 + 柔 S + 黑位 + 冷暖分离 + 饱和 / vibrance（肤色减半）+ 中间调单色颗粒，
可选叠加 .cube 3D LUT（三线性插值）。纯 numpy / PIL，确定性：颗粒种子由输入图像素的 sha256 派生（同输入同输出）。
来源：tools/research/look_v2_ab.py FILM / film_grade（H1 实验）。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

VERSION = "film_look/v1"

FILM_DEFAULTS: dict = {
    "knee": 0.78, "knee_a": 0.66, "s_curve": 0.12, "lift": 4 / 255, "gamma": 1.0,
    "sat": 1.08, "vib": 0.08, "skin_keep": 0.5, "shadow": [-0.012, 0.0, 0.018], "high": [0.016, 0.008, -0.012],
    "grain": 0.012,
}


def seed_of(arr_u8: np.ndarray) -> int:
    return int.from_bytes(hashlib.sha256(np.ascontiguousarray(arr_u8).tobytes()).digest()[:8], "big")


def film_array(y: np.ndarray, p: dict | None = None, seed: int = 0) -> np.ndarray:
    """y: float32 H×W×3，0..1 → float32 0..1（未量化）"""
    p = {**FILM_DEFAULTS, **(p or {})}
    y = y.astype(np.float32)
    if p["gamma"] != 1.0:
        y = y ** p["gamma"]
    k, a = p["knee"], p["knee_a"]
    t = np.clip((y - k) / (1 - k), 0, None)
    y = np.where(y > k, k + (1 - k) * np.tanh(a * t) / a, y)              # 高光滚落：拐点处斜率 1
    sm = y * y * (3 - 2 * y)
    y = (1 - p["s_curve"]) * y + p["s_curve"] * sm                        # 柔 S
    y = p["lift"] + (1 - p["lift"]) * y                                   # 黑位微抬
    lum = (0.2126 * y[..., 0] + 0.7152 * y[..., 1] + 0.0722 * y[..., 2])[..., None]
    mx, mn = y.max(-1, keepdims=True), y.min(-1, keepdims=True)
    chroma = mx - mn
    r, g, b = y[..., 0:1], y[..., 1:2], y[..., 2:3]
    skin = ((r > g) & (g > b) & (chroma > 0.04) & (chroma < 0.45)).astype(np.float32)
    skin *= np.clip(1 - np.abs((g - b) / np.maximum(r - b, 1e-6) - 0.45) / 0.45, 0, 1)
    sat = p["sat"] + p["vib"] * (1 - np.clip(chroma / 0.5, 0, 1))
    delta = sat - 1.0
    delta = np.where(delta > 0, delta * (1 - skin * (1 - p["skin_keep"])), delta)
    y = lum + (y - lum) * (1 + delta)
    y = y + (1 - lum) ** 2 * np.array(p["shadow"], np.float32) + lum ** 2 * np.array(p["high"], np.float32)
    if p.get("grain"):
        rng = np.random.default_rng(int(seed) & 0xFFFFFFFFFFFFFFFF)
        n = rng.standard_normal(y.shape[:2]).astype(np.float32)[..., None]
        w = 1 - np.abs(2 * np.clip(lum, 0, 1) - 1)                        # 中间调最多，死黑 / 高光少
        y = y + n * p["grain"] * (0.35 + 0.65 * w)
    return np.clip(y, 0, 1)


def read_cube(path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """读 .cube（3D）→ (table[B,G,R,3] float32, domain_min, domain_max)；.cube 数据行红变化最快"""
    n, dmin, dmax, rows = 0, np.zeros(3, np.float32), np.ones(3, np.float32), []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if parts[0] == "LUT_3D_SIZE":
            n = int(parts[1])
        elif parts[0] == "DOMAIN_MIN":
            dmin = np.array(parts[1:4], np.float32)
        elif parts[0] == "DOMAIN_MAX":
            dmax = np.array(parts[1:4], np.float32)
        elif parts[0] in ("TITLE", "LUT_1D_SIZE") or not (parts[0][0].isdigit() or parts[0][0] in "-+."):
            if parts[0] == "LUT_1D_SIZE":
                raise ValueError("只支持 3D LUT")
        else:
            rows.append([float(x) for x in parts[:3]])
    if n < 2 or len(rows) != n ** 3:
        raise ValueError(f"cube 格式不对：{path}（size {n}，行数 {len(rows)}）")
    return np.array(rows, np.float32).reshape(n, n, n, 3), dmin, dmax


def apply_lut(y: np.ndarray, table: np.ndarray, dmin=None, dmax=None, strength: float = 1.0) -> np.ndarray:
    """三线性插值。y float32 H×W×3 0..1；strength 0..1 与原图线性混合"""
    n = table.shape[0]
    dmin = np.zeros(3, np.float32) if dmin is None else dmin
    dmax = np.ones(3, np.float32) if dmax is None else dmax
    u = np.clip((y - dmin) / (dmax - dmin), 0, 1) * (n - 1)
    i0 = np.minimum(np.floor(u).astype(np.int32), n - 2)
    f = (u - i0).astype(np.float32)
    r0, g0, b0 = i0[..., 0], i0[..., 1], i0[..., 2]
    fr, fg, fb = f[..., 0:1], f[..., 1:2], f[..., 2:3]
    out = 0
    for dr in (0, 1):
        for dg in (0, 1):
            for db in (0, 1):
                w = (fr if dr else 1 - fr) * (fg if dg else 1 - fg) * (fb if db else 1 - fb)
                out = out + w * table[b0 + db, g0 + dg, r0 + dr]
    s = float(max(0.0, min(1.0, strength)))
    return y * (1 - s) + out * s if s < 1.0 else out


def grade_image(im, film: dict | None = None, lut: dict | None = None, lut_path=None):
    """PIL RGB → PIL RGB。film=None 跳过调色；lut={'apply':bool,'strength':x}，lut_path 为 .cube。返回 (图, 记录)"""
    from PIL import Image
    arr = np.asarray(im.convert("RGB"))
    seed = seed_of(arr)
    y = arr.astype(np.float32) / 255.0
    rec: dict = {"version": VERSION}
    if film is not None:
        pf = {**FILM_DEFAULTS, **film}
        y = film_array(y, pf, seed)
        rec["film"] = pf
        rec["grain_seed"] = seed
    if lut and lut.get("apply") and lut_path:
        tb, dmin, dmax = read_cube(lut_path)
        y = apply_lut(y, tb, dmin, dmax, float(lut.get("strength", 1.0)))
        rec["lut"] = {"file": Path(lut_path).name, "strength": float(lut.get("strength", 1.0))}
    return Image.fromarray((np.clip(y, 0, 1) * 255 + 0.5).astype(np.uint8)), rec
