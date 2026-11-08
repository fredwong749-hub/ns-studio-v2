"""
首帧/尾帧调色（A4/A6，research v6 §6）：确定性颜色迁移到项目方色调参考图 assets/style/color_reference_01.png。

本机 ComfyUI 没有 KJNodes ColorMatch（只有核心 ColorTransfer 节点），这里用 Python 做同类的 LAB Reinhard 迁移，
好处：可复现、能同时保留未调色原图、能按镜头做暗场保护。

- 色度（a/b 均值与方差）按 strength 向参考图靠拢 → 粉/冰蓝的浓郁配色；
- 亮度（L）只按 strength × L_SHARE 轻推，且暗场/夜景镜头完全不动 L、不提亮（剧本光源优先）；
- 皮肤保护：均值偏移的色度上限 MAX_SHIFT，避免肤色整体偏粉（v6 ⚠）。
尾帧调色以定稿首帧为参考（strength 更高），保证首尾帧之间不闪色。
"""
from functools import lru_cache
import os
from pathlib import Path

import numpy as np

ROOT = Path(os.environ.get("NS_PROJECT_ROOT") or Path(__file__).resolve().parents[3])  # T42：子进程可指向 projects/<slug>
COLOR_REF = ROOT / "assets" / "style" / "color_reference_01.png"
METHOD = "reinhard_lab"
STRENGTH = 0.6
L_SHARE = 0.5            # 亮度只迁移一半力度
MAX_SHIFT = 8.0          # a/b 均值最大偏移（LAB 单位）
DARK_L = 38.0            # 平均亮度低于此值按暗场处理
DARK_SHIFT_SHARE = 0.35  # 暗场的色度均值偏移只用 35%（否则黑位偏红）


def _lab(rgb: np.ndarray) -> np.ndarray:
    import cv2
    return cv2.cvtColor(rgb.astype(np.float32) / 255.0, cv2.COLOR_RGB2LAB)


def _rgb(lab: np.ndarray) -> np.ndarray:
    import cv2
    out = cv2.cvtColor(lab.astype(np.float32), cv2.COLOR_LAB2RGB)
    return np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8)


def _load(path: Path) -> np.ndarray:
    from PIL import Image
    return np.asarray(Image.open(path).convert("RGB"))


def stats(lab: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    flat = lab.reshape(-1, 3)
    return flat.mean(0), flat.std(0) + 1e-6


@lru_cache(maxsize=8)
def ref_stats(path: str = str(COLOR_REF)) -> tuple[tuple, tuple]:
    mu, sd = stats(_lab(_load(Path(path))))
    return tuple(mu.tolist()), tuple(sd.tolist())


def transfer(rgb: np.ndarray, ref_mu, ref_sd, strength: float = STRENGTH, dark: bool = False,
             l_share: float = L_SHARE, max_shift: float = MAX_SHIFT) -> tuple[np.ndarray, dict]:
    lab = _lab(rgb)
    mu, sd = stats(lab)
    ref_mu, ref_sd = np.asarray(ref_mu, np.float32), np.asarray(ref_sd, np.float32)
    dark = dark or float(mu[0]) < DARK_L
    out = lab.copy()
    for c in range(3):
        k = strength * (0.0 if dark else l_share) if c == 0 else strength * (0.5 if dark else 1.0)
        if k <= 0:
            continue
        tgt_mu = ref_mu[c]
        if c > 0:
            lim = max_shift * (DARK_SHIFT_SHARE if dark else 1.0)
            tgt_mu = mu[c] + np.clip(ref_mu[c] - mu[c], -lim, lim)
        # 只增不减：反差和饱和度向参考图看齐，但不会被压平（参考图的亮度方差比多数剧照小）
        tgt_sd = max(float(sd[c]), float(ref_sd[c]))
        moved = (lab[..., c] - mu[c]) / sd[c] * tgt_sd + tgt_mu
        out[..., c] = lab[..., c] + k * (moved - lab[..., c])
    if dark:  # 暗场：绝不提亮
        out[..., 0] = np.minimum(out[..., 0], lab[..., 0])
    out[..., 0] = np.clip(out[..., 0], 0, 100)
    info = {"method": METHOD, "strength": strength, "dark": bool(dark), "l_share": 0.0 if dark else l_share,
            "src_mean": [round(float(x), 2) for x in mu], "ref_mean": [round(float(x), 2) for x in ref_mu]}
    return _rgb(out), info


def grade_file(src: Path, dst: Path, *, reference: Path | None = None, strength: float = STRENGTH,
               dark: bool = False, l_share: float = L_SHARE) -> dict:
    """src（原图）→ dst（调色后）。reference 缺省 = 项目方色调参考图；尾帧传定稿首帧"""
    from PIL import Image
    ref = Path(reference or COLOR_REF)
    mu, sd = ref_stats(str(ref)) if reference is None else tuple(map(tuple, stats(_lab(_load(ref)))))
    out, info = transfer(_load(src), mu, sd, strength=strength, dark=dark, l_share=l_share)
    dst.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(out).save(dst)
    info["reference"] = ref.relative_to(ROOT).as_posix() if ref.is_relative_to(ROOT) else str(ref)
    return info


def colour_distance(path: Path, reference: Path | None = None, dark: bool = False) -> float:
    """与参考图在 LAB 统计上的距离（色度均值/方差，非暗场再加亮度）；越小越接近"""
    ref = Path(reference or COLOR_REF)
    rmu, rsd = (np.asarray(x) for x in ref_stats(str(ref))) if reference is None else stats(_lab(_load(ref)))
    mu, sd = stats(_lab(_load(path)))
    d = np.abs(mu[1:] - rmu[1:]).sum() + 0.5 * np.abs(sd[1:] - rsd[1:]).sum()
    if not dark:
        d += 0.3 * abs(mu[0] - rmu[0])
    return float(d)
