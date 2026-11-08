"""光学模拟后期（H4，PLAN_HOLLYWOOD_LOOK §8）：单目深度 → 以说话人 / 焦点主体的脸为焦平面 → 按视差差 × 光圈系数分层散焦
（disk 核、按深度前后合成、归一化卷积防前景渗色）+ 高光圆斑（线性光里扩展高光能量，可选变形椭圆 + 横向光晕）
+ 镜头特征（轻暗角、极弱横向色散、Kodak 式暖红 halation）。

顺序：放大出图缩回 → **lens_sim** → film_look（胶片调色 / 颗粒）。理由：散焦 / 光晕 / 暗角是镜头和底片上发生的事，
印片曲线、颗粒在其后；颗粒若先加会被散焦抹掉，高光滚落若先做则圆斑没有能量。

确定性：纯 numpy / cv2 / torch-CPU，没有随机数；深度图可另存 .npy（replay 时直接读，逐字节可复现）。
enabled=false（默认）→ apply() 原样返回输入对象，像素逐字节不变。

深度：Depth Anything V2 Small（HF transformers 格式，models_cache/depth_anything_v2_small/；本机 CPU 推理 ~2–4 s/张）。
输出是相对视差（仿射不变，大 = 近），CoC ∝ |1/d − 1/s| 正好与视差差成线性，所以模糊半径 = k × |disp − disp_focus|。
焦点：YuNet 找脸（packages/qc/ns_qc/face.py），取“面积 × 居中度”最高的脸（可用 focus=[x,y] 指定 / "center"）；
焦点带 = 该脸眼睛区视差 20–80 分位 ± dof_band（整张脸都在景深内）。

参数版本 VERSION；record 里记参数、焦点、深度模型、半径统计。
"""
from __future__ import annotations

import hashlib
import math
import os
import sys
import threading
from pathlib import Path

import cv2
import numpy as np

VERSION = "lens_sim/v1"
ROOT = Path(__file__).resolve().parents[3]

DEFAULTS: dict = {
    "enabled": False,
    "focal_mm": 50.0,          # 等效大画幅焦段（近景用 85）
    "t_stop": 2.0,             # T 值；模糊系数 ∝ (focal/50)^2 × (2/T)
    "coc_px": 16.0,            # 50mm/T2、短边 768 时，归一化视差差 1.0 对应的最大模糊直径（像素）
    "max_radius": 28.0,        # 模糊半径上限（像素，按短边 768 标定）
    "dof_band": 0.05,          # 焦点带半宽（归一化视差）：带内不虚
    "near_gain": 0.8,          # 前景（比焦点近）的模糊系数折减：前景过虚会吃掉主体
    "anamorphic": 0.0,         # 0 = 球面圆斑；2.0 = 2× 变形宽银幕（竖椭圆，高宽比 2）
    "streak": 0.0,             # 变形横向光晕强度（0 关）
    "hl_gain": 1.8,            # 高光能量扩展（线性光里把 > hl_thr 的亮点抬高，散焦后成圆斑）
    "hl_thr": 0.72,
    "focal_contrast": 0.0,     # 焦平面极轻微局部反差（0 = 焦平面像素不动；0.05 为徕卡“微反差”档）
    "vignette": 0.10,          # 角落亮度衰减比例
    "ca": 0.0006,              # 横向色散：R / B 通道径向缩放 ±ca
    "halation": {"gain": 0.10, "thr": 0.80, "sigma": 0.012, "tint": [1.0, 0.36, 0.12]},
    "depth_model": "models_cache/depth_anything_v2_small",
    "depth_refine": {"radius": 6, "eps": 2e-3},     # 以灰度图为引导的导向滤波（发丝边缘跟随图像）
    "focus": "auto",           # auto | center | all_faces | [x, y]（归一化）；all_faces = 够大的脸全部算焦点（双人对手戏）
    # L-191 / H4 修复（PS-MASTER 亲看 S4：焦外人物白晕、同平面人物被虚得过重）
    "occlusion_dilate": 0.008,     # 遮挡感知：视差在深度跳变处取近侧值（灰度膨胀，半径 = 比例 × 短边）；0 关。防焦外人物轮廓的像素被当成远景大半径散到白幕上
    "hl_near_disp": 0.3,           # 视差高于此 = 人物层
    "hl_guard": 0.06,             # 人物层外扩多少（× 短边）内不扩高光能量
    "hl_skip_near": True,          # 高光扩能量不用在“人物层”（视差高于背景）：防止人物边缘的亮点被抬亮后散成白晕
    "person_limit": {"enabled": True, "tol": 0.6, "min_ratio": 0.12, "min_face_ratio": 0.15},
    #   其他人物（脸面积 ≥ 焦点脸 × min_face_ratio）与焦点的视差差 Δ：半径 × (min_ratio + (1-min_ratio) × smoothstep(Δ/tol))
    #   → 与焦点主体几乎同一深度的人物几乎不虚，前后拉开才逐渐虚到满
    "all_faces_min_ratio": 0.3,    # focus=all_faces：脸面积 ≥ 最大脸 × 此比例的都算焦点
    # L-192 ③ v2 真实光学（默认关；参数全集见 lens_sim_v2.V2_DEFAULTS；enabled 与 optics_v2.enabled 都为真才走 v2，v1 路径不变）
    "optics_v2": {"enabled": False},
}

LEVELS = (0.0, 0.7, 1.4, 2.2, 3.2, 4.5, 6.0, 8.0, 10.5, 13.5, 17.0, 21.0, 26.0, 32.0, 40.0)


def _sha16(arr: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(arr).tobytes()).hexdigest()[:16]


# ---------------------------------------------------------------- 深度
_DEPTH = {"lock": threading.Lock(), "model": None, "proc": None, "dir": None}


def depth_model_dir(p: str | Path | None = None) -> Path:
    d = Path(p or DEFAULTS["depth_model"])
    return d if d.is_absolute() else ROOT / d


def depth_available(p: str | Path | None = None) -> bool:
    d = depth_model_dir(p)
    return (d / "config.json").is_file() and (d / "model.safetensors").is_file()


def _load_depth(model_dir: Path):
    with _DEPTH["lock"]:
        if _DEPTH["model"] is None or _DEPTH["dir"] != str(model_dir):
            import torch
            from transformers import AutoImageProcessor, AutoModelForDepthEstimation
            torch.set_num_threads(max(1, min(8, os.cpu_count() or 4)))
            proc = AutoImageProcessor.from_pretrained(str(model_dir))
            model = AutoModelForDepthEstimation.from_pretrained(str(model_dir), torch_dtype=torch.float32).eval()
            _DEPTH.update(model=model, proc=proc, dir=str(model_dir))
        return _DEPTH["model"], _DEPTH["proc"]


def estimate_depth(rgb: np.ndarray, model_dir: str | Path | None = None) -> np.ndarray:
    """rgb uint8 H×W×3 → 归一化相对视差 float32 H×W，0..1（1 = 最近）；1/99 分位拉伸"""
    import torch
    from PIL import Image
    model, proc = _load_depth(depth_model_dir(model_dir))
    h, w = rgb.shape[:2]
    inp = proc(images=Image.fromarray(rgb), return_tensors="pt")
    with torch.no_grad():
        out = model(**inp).predicted_depth          # 1×h'×w'，相对视差
    d = torch.nn.functional.interpolate(out[None], size=(h, w), mode="bicubic", align_corners=False)[0, 0].numpy()
    lo, hi = np.percentile(d, 1), np.percentile(d, 99)
    return np.clip((d - lo) / max(hi - lo, 1e-6), 0, 1).astype(np.float32)


def guided_filter(guide: np.ndarray, src: np.ndarray, radius: int, eps: float) -> np.ndarray:
    """He 导向滤波（灰度引导）：深度边缘贴合图像边缘（发丝 / 轮廓），避免深度图块状"""
    k = (2 * radius + 1, 2 * radius + 1)
    box = lambda x: cv2.boxFilter(x, -1, k, normalize=True, borderType=cv2.BORDER_REFLECT)   # noqa: E731
    mi, mp = box(guide), box(src)
    var = box(guide * guide) - mi * mi
    cov = box(guide * src) - mi * mp
    a = cov / (var + eps)
    b = mp - a * mi
    return box(a) * guide + box(b)


# ---------------------------------------------------------------- 焦点
def detect_faces(rgb: np.ndarray) -> list[list[float]]:
    """YuNet：[x, y, w, h, score]…（找不到模型 / 没脸 → []）"""
    try:
        try:
            from ns_qc import face as QF
        except ImportError:
            sys.path.insert(0, str(ROOT / "packages" / "qc"))
            from ns_qc import face as QF
        paths = QF.find_models(ROOT)
        if not paths:
            return []
        m = QF.FaceModels(*paths, score=0.5)
        rows = m.faces(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    except Exception:  # noqa: BLE001  检测失败不阻断后期
        return []
    return [[float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[-1])] for r in rows if float(r[3]) >= 24]


def _face_disp(disp: np.ndarray, f: list[float]) -> tuple[float, float, float]:
    """脸眼睛 / 鼻梁区视差 (p20, p50, p80)"""
    h, w = disp.shape
    x0, y0 = max(0, int(f[0] + f[2] * 0.2)), max(0, int(f[1] + f[3] * 0.15))
    x1, y1 = min(w, int(f[0] + f[2] * 0.8) + 1), min(h, int(f[1] + f[3] * 0.65) + 1)
    reg = disp[y0:y1, x0:x1]
    if reg.size == 0:
        reg = disp
    return tuple(float(np.percentile(reg, q)) for q in (20, 50, 80))


def pick_focus(disp: np.ndarray, faces: list[list[float]], focus, dof_band: float, min_ratio: float = 0.3) -> dict:
    """焦点带：脸（面积 × 居中度最高）眼睛区视差 20–80 分位 ± dof_band；没脸 → 中心 30% 区域；focus=[x,y] → 该点 6% 窗"""
    h, w = disp.shape
    mode, box = "face", None
    if isinstance(focus, (list, tuple)) and len(focus) == 2:
        cx, cy = float(focus[0]) * w, float(focus[1]) * h
        r = 0.03 * max(h, w)
        box, mode = (cx - r, cy - r, 2 * r, 2 * r), "point"
    elif focus == "center" or not faces:
        box, mode = (w * 0.35, h * 0.35, w * 0.3, h * 0.3), "center"
    else:
        def score(f):
            fx, fy = f[0] + f[2] / 2, f[1] + f[3] / 2
            dist = math.hypot((fx - w / 2) / w, (fy - h / 2) / h) / 0.71
            return f[2] * f[3] * (1 - 0.5 * dist)
        f = max(faces, key=score)
        box = (f[0] + f[2] * 0.2, f[1] + f[3] * 0.15, f[2] * 0.6, f[3] * 0.5)     # 眼睛 / 鼻梁区
    x0, y0 = max(0, int(box[0])), max(0, int(box[1]))
    x1, y1 = min(w, int(box[0] + box[2]) + 1), min(h, int(box[1] + box[3]) + 1)
    reg = disp[y0:y1, x0:x1]
    if reg.size == 0:
        reg = disp
    p20, p50, p80 = (float(np.percentile(reg, q)) for q in (20, 50, 80))
    info = {"mode": mode, "box": [round(x0), round(y0), round(x1 - x0), round(y1 - y0)], "disp": round(p50, 4),
            "band": [round(max(0.0, p20 - dof_band), 4), round(min(1.0, p80 + dof_band), 4)], "faces": len(faces)}
    if mode == "face" and faces:
        info["face"] = [round(v, 1) for v in f]
    if mode == "face" and len(faces) > 1:      # 其他人物：脸框 + 视差（person_limit 用）
        info["others"] = [{"box": [round(g[0], 1), round(g[1], 1), round(g[2], 1), round(g[3], 1)], "disp": round(_face_disp(disp, g)[1], 4)}
                          for g in faces if g is not f]
    if focus == "all_faces" and len(faces) > 1:
        amax = max(g[2] * g[3] for g in faces)
        keep = [g for g in faces if g[2] * g[3] >= amax * min_ratio]
        bands = []
        for g in keep:
            a20, _a50, a80 = _face_disp(disp, g)
            bands.append([round(max(0.0, a20 - dof_band), 4), round(min(1.0, a80 + dof_band), 4)])
        info["bands"] = bands
        info["mode"] = "all_faces"
        info["band"] = [min(b[0] for b in bands), max(b[1] for b in bands)]    # 记录用（真正用 bands）
    return info


# ---------------------------------------------------------------- 遮挡 / 同平面人物
def occlusion_disp(disp: np.ndarray, frac: float, short_side: int) -> np.ndarray:
    """遮挡感知视差：在深度跳变处取近侧值（灰度膨胀）。焦外人物轮廓一圈的像素视差被平滑成“半远半近”，
    原来会被当成远景按大半径散到白幕上（= 白晕）；膨胀后这圈像素归人物层，和人物一起按人物半径散焦"""
    r = int(round(frac * short_side))
    if r < 1:
        return disp
    se = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    return cv2.dilate(disp, se, borderType=cv2.BORDER_REPLICATE)


def person_ratio_field(disp: np.ndarray, fi: dict, pl: dict) -> np.ndarray | None:
    """同深度人物限幅：对焦点以外的每个人物（脸），Δ = |其脸视差 − 焦点视差|，半径倍率
    min_ratio + (1−min_ratio)·smoothstep(Δ/tol)，只乘在该人物的区域（脸框扩出的身体框内、视差在其脸 ±0.10 以内）。→ H×W 倍率（无 → None）"""
    pl = {**DEFAULTS["person_limit"], **(pl or {})}
    others = fi.get("others") or []
    if not pl or not pl.get("enabled") or not others or fi.get("mode") != "face" or not fi.get("face"):
        return None
    h, w = disp.shape
    fx = fi["face"]
    fa = fx[2] * fx[3]
    ratio = np.ones((h, w), np.float32)
    for o in others:
        bx, by, bw, bh = o["box"]
        if bw * bh < fa * pl["min_face_ratio"]:
            continue
        delta = abs(o["disp"] - fi["disp"])
        rt = pl["min_ratio"] + (1 - pl["min_ratio"]) * float(_smooth(np.array(delta / max(pl["tol"], 1e-6)), 0.0, 1.0))
        x0, x1 = max(0, int(bx - 1.2 * bw)), min(w, int(bx + 2.2 * bw))
        y0 = max(0, int(by - 0.4 * bh))
        m = np.zeros((h, w), np.float32)
        m[y0:, x0:x1] = (np.abs(disp[y0:, x0:x1] - o["disp"]) < 0.10)
        m = _gauss(m, max(2.0, 0.004 * min(h, w)))
        ratio = np.minimum(ratio, 1 - m * (1 - rt))
    return ratio


# ---------------------------------------------------------------- 散焦
def radius_map(disp: np.ndarray, band: tuple[float, float], p: dict, short_side: int) -> np.ndarray:
    """带符号模糊半径（像素）：负 = 比焦点远（背景），正 = 比焦点近（前景）；带内 0"""
    lo, hi = band
    far = np.clip(lo - disp, 0, None)          # 比焦点带远多少
    near = np.clip(disp - hi, 0, None)
    bands = p.get("_bands")
    if bands:                                   # all_faces：落在任一人脸焦点带内 = 清楚；其余按离最近一条带的距离、在它哪一侧算远 / 近
        fa = np.stack([np.clip(b[0] - disp, 0, None) for b in bands])
        na = np.stack([np.clip(disp - b[1], 0, None) for b in bands])
        dist = fa + na                      # 点在带外只会有一边非 0
        i = dist.argmin(0)[None]
        far = np.take_along_axis(fa, i, 0)[0]
        near = np.take_along_axis(na, i, 0)[0]
    k = p["coc_px"] / 2 * (p["focal_mm"] / 50.0) ** 2 * (2.0 / p["t_stop"]) * (short_side / 768.0)
    mx = p["max_radius"] * short_side / 768.0
    r = -np.minimum(far * k, mx) + np.minimum(near * k * p["near_gain"], mx)
    return r.astype(np.float32)


def disk_kernel(r: float, anamorphic: float = 0.0) -> np.ndarray:
    """抗锯齿圆盘核（r 为半径，像素）；anamorphic>1 → 竖椭圆（高宽比 anamorphic，面积不变）"""
    if r <= 0.05:
        return np.ones((1, 1), np.float32)
    a = max(1.0, float(anamorphic or 1.0))
    rx, ry = r / math.sqrt(a), r * math.sqrt(a)
    nx, ny = int(math.ceil(rx)) + 1, int(math.ceil(ry)) + 1
    ss = 4                                                   # 4×4 超采样抗锯齿
    ys, xs = np.mgrid[-ny:ny + 1, -nx:nx + 1].astype(np.float32)
    acc = np.zeros_like(xs)
    for dy in range(ss):
        for dx in range(ss):
            ox, oy = (dx + 0.5) / ss - 0.5, (dy + 0.5) / ss - 0.5
            acc += (((xs + ox) / rx) ** 2 + ((ys + oy) / ry) ** 2 <= 1.0)
    k = acc / (ss * ss)
    return (k / k.sum()).astype(np.float32)


def _conv(img: np.ndarray, k: np.ndarray) -> np.ndarray:
    if k.shape == (1, 1):
        return img
    return cv2.filter2D(img, -1, k, borderType=cv2.BORDER_REFLECT)


def layered_defocus(lin: np.ndarray, r_signed: np.ndarray, anamorphic: float = 0.0) -> tuple[np.ndarray, dict]:
    """线性光 H×W×3 按带符号半径分层散焦：每层归一化卷积（只用本层像素，前景不渗进背景），由远到近 over 合成，
    最后按覆盖率归一；焦平面层（r=0）像素原样保留"""
    rmax = float(np.abs(r_signed).max())
    lv_pos = [l for l in LEVELS if l <= rmax]
    nxt = next((l for l in LEVELS if l > rmax), None)
    if nxt is not None:
        lv_pos.append(nxt)
    levels = sorted({-l for l in lv_pos} | set(lv_pos))              # 远（负）→ 焦平面 0 → 近（正）
    r = np.clip(r_signed, levels[0], levels[-1])
    out = np.zeros_like(lin)
    cov = np.zeros(lin.shape[:2], np.float32)
    used = 0
    for i, lv in enumerate(levels):
        lo = levels[i - 1] if i > 0 else lv - 1
        hi = levels[i + 1] if i + 1 < len(levels) else lv + 1
        m = np.where(r < lv, (r - lo) / max(lv - lo, 1e-6), (hi - r) / max(hi - lv, 1e-6))
        m = np.clip(m, 0, 1).astype(np.float32)
        if lv == levels[0]:
            m = np.where(r <= lv, 1.0, m).astype(np.float32)
        if lv == levels[-1]:
            m = np.where(r >= lv, 1.0, m).astype(np.float32)
        if m.max() <= 0:
            continue
        used += 1
        k = disk_kernel(abs(lv), anamorphic)
        a = _conv(m, k)
        c = _conv(lin * m[..., None], k) / np.maximum(a, 1e-4)[..., None]
        a = np.clip(a, 0, 1)
        out = out * (1 - a)[..., None] + c * a[..., None]
        cov = cov * (1 - a) + a
    out = out / np.maximum(cov, 1e-4)[..., None]
    return out.astype(np.float32), {"levels": used, "r_max": round(rmax, 2)}


# ---------------------------------------------------------------- 镜头特征
def _lum(y: np.ndarray) -> np.ndarray:
    return 0.2126 * y[..., 0] + 0.7152 * y[..., 1] + 0.0722 * y[..., 2]


def _smooth(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    t = np.clip((x - lo) / max(hi - lo, 1e-6), 0, 1)
    return t * t * (3 - 2 * t)


def _tophat(lum: np.ndarray, short_side: int, frac: float = 0.03) -> np.ndarray:
    """白顶帽：亮度减去“大于结构元的开运算”→ 只剩比结构元小的亮点（灯泡、反光），大面积亮区（白幕）为 0，
    而且不会在人物的暗边旁边误判（均值高通会在那里把白幕当亮点）"""
    r = max(3, int(frac * short_side))
    se = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    return cv2.morphologyEx(lum.astype(np.float32), cv2.MORPH_TOPHAT, se, borderType=cv2.BORDER_REFLECT)


def _gauss(x: np.ndarray, sigma: float) -> np.ndarray:
    if sigma <= 0.3:
        return x
    k = int(sigma * 3) * 2 + 1
    return cv2.GaussianBlur(x, (k, k), sigma, borderType=cv2.BORDER_REFLECT)


def halation(y: np.ndarray, hp: dict, short_side: int) -> np.ndarray:
    """Kodak 式 halation：高光（> thr）向外晕开（σ = sigma × 短边），只加暖红（R 最多、B 几乎没有）"""
    if not hp or hp.get("gain", 0) <= 0:
        return y
    lum = _lum(y)
    m = _smooth(lum, hp["thr"], 1.0) * lum
    glow = np.clip(_gauss(m, hp["sigma"] * short_side) - m, 0, None)   # 只在亮区边缘外侧晕开；大面积均匀亮区内部为 0
    tint = np.array(hp.get("tint") or [1.0, 0.36, 0.12], np.float32)
    return y + hp["gain"] * glow[..., None] * tint


def streak(y: np.ndarray, gain: float, short_side: int) -> np.ndarray:
    """变形镜头横向光晕：很亮的点沿水平方向拉开（偏蓝的镀膜色）"""
    if gain <= 0:
        return y
    lum = _lum(y)
    m = np.clip(_tophat(lum, short_side) - 0.08, 0, None) * 4 * _smooth(lum, 0.8, 1.0)     # 只有小而亮的点才拉横线
    k = int(0.12 * short_side) * 2 + 1
    s = cv2.GaussianBlur(m, (k, 3), sigmaX=0.04 * short_side, sigmaY=0.8, borderType=cv2.BORDER_REFLECT)
    return y + gain * s[..., None] * np.array([0.55, 0.75, 1.0], np.float32)


def chromatic(y: np.ndarray, ca: float) -> np.ndarray:
    """横向色散：R 通道径向放大 (1+ca)、B 通道缩小 (1−ca)，G 不动（角落错位 ≈ ca × 半对角线像素）"""
    if ca <= 0:
        return y
    h, w = y.shape[:2]
    out = y.copy()
    for ch, s in ((0, 1 + ca), (2, 1 - ca)):
        M = cv2.getRotationMatrix2D((w / 2 - 0.5, h / 2 - 0.5), 0, s)
        out[..., ch] = cv2.warpAffine(y[..., ch], M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    return out


def vignette(y: np.ndarray, v: float) -> np.ndarray:
    if v <= 0:
        return y
    h, w = y.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    rn = np.hypot((xx - w / 2) / (w / 2), (yy - h / 2) / (h / 2)) / math.sqrt(2)
    return y * (1 - v * rn ** 2.4)[..., None]


def soft_clip(y: np.ndarray, knee: float = 0.92) -> np.ndarray:
    t = np.clip((y - knee) / (1 - knee), 0, None)
    return np.where(y > knee, knee + (1 - knee) * np.tanh(t), y)


# ---------------------------------------------------------------- 主入口
def lens_array(y: np.ndarray, disp: np.ndarray, p: dict, focus_info: dict | None = None) -> tuple[np.ndarray, dict]:
    """y float32 H×W×3 显示空间 0..1；disp 归一化视差（已细化）；返回 (float32 0..1, 记录)。
    焦平面像素：focal_contrast=0 且不开暗角 / 色散 / halation 时逐值不变（分层合成里 r=0 层核为 1×1）"""
    p = {**DEFAULTS, **(p or {})}
    h, w = y.shape[:2]
    ss = min(h, w)
    fi = focus_info or pick_focus(disp, [], "center", p["dof_band"])
    disp_raw = disp
    if p.get("occlusion_dilate"):
        disp = occlusion_disp(disp, float(p["occlusion_dilate"]), ss)
    p = {**p, "_bands": fi.get("bands")}
    r = radius_map(disp, tuple(fi["band"]), p, ss)
    pr = person_ratio_field(disp_raw, fi, p.get("person_limit") or {})
    if pr is not None:
        r = (r * pr).astype(np.float32)
    lin = np.power(np.clip(y, 0, 1), 2.2).astype(np.float32)
    if p["hl_gain"] > 0:
        lum = _lum(lin)
        peak = _tophat(lum, ss)                                           # 只认小亮点（灯、反光）；大面积白幕 / 人物边上的白幕不算
        boost = 1 + p["hl_gain"] * _smooth(lum, p["hl_thr"] ** 2.2, 1.0) * np.clip((peak - 0.02) / 0.08, 0, 1)
        boost = np.where(np.abs(r) > 0.3, boost, 1.0).astype(np.float32)   # 只给要散焦的像素扩能量（焦平面不动）
        if p.get("hl_skip_near"):      # 白晕根因（S4 实测：hl_gain=0 时无晕）：人物旁窄条白幕被白顶帽当成“小亮点”扩能量后散成一圈白晕。
            # 人物层 + 其外 hl_guard × 短边以内都不扩能量；圆斑只来自离人物较远的背景灯光
            fg = (disp > p["hl_near_disp"]).astype(np.uint8)
            g = max(1, int(p["hl_guard"] * ss))
            fg = cv2.dilate(fg, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * g + 1, 2 * g + 1)))
            boost = np.where(fg > 0, 1.0, boost).astype(np.float32)
        lin = lin * boost[..., None]
    out, dinfo = layered_defocus(lin, r, p["anamorphic"])
    if p["focal_contrast"] > 0:
        m0 = np.clip(1 - np.abs(r) / 0.7, 0, 1).astype(np.float32)
        out = out + p["focal_contrast"] * (lin - _gauss(lin, 0.01 * ss)) * m0[..., None]
    out = np.power(np.clip(out, 0, None), 1 / 2.2)
    out = soft_clip(out)
    out = np.where((r == 0)[..., None], y, out).astype(np.float32)     # 焦平面像素原样（不经线性往返的舍入）
    if p["streak"] > 0:
        out = streak(out, p["streak"], ss)
    out = halation(out, p.get("halation") or {}, ss)
    out = chromatic(out, p["ca"])
    out = vignette(out, p["vignette"])
    bg = r < -0.3
    rec = {"version": VERSION, "params": {k: v for k, v in p.items() if k != "enabled"}, "focus": fi,
           "radius": {"max": dinfo["r_max"], "bg_mean": round(float(np.abs(r[bg]).mean()) if bg.any() else 0.0, 2),
                      "bg_frac": round(float(bg.mean()), 3), "near_frac": round(float((r > 0.3).mean()), 3), "levels": dinfo["levels"]}}
    return np.clip(out, 0, 1).astype(np.float32), rec


def prepare_depth(rgb: np.ndarray, p: dict, depth: np.ndarray | None = None) -> np.ndarray:
    """估深度（或用传入的）→ 导向滤波细化"""
    p = {**DEFAULTS, **(p or {})}
    d = depth if depth is not None else estimate_depth(rgb, p["depth_model"])
    if d.shape != rgb.shape[:2]:
        d = cv2.resize(d, (rgb.shape[1], rgb.shape[0]), interpolation=cv2.INTER_CUBIC)
    rf = p.get("depth_refine") or {}
    if rf and rf.get("radius", 0) > 0:
        g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255
        d = guided_filter(g, d.astype(np.float32), int(rf["radius"]), float(rf["eps"]))
    return np.clip(d, 0, 1).astype(np.float32)


def apply(im, p: dict | None = None, *, depth: np.ndarray | None = None, depth_out: Path | None = None):
    """PIL RGB → (PIL RGB, 记录)。p.enabled 不为真 → 原对象原样返回（逐字节不变），记录 {"version", "enabled": False}。
    depth：预先算好的视差（replay / 测试）；depth_out：把细化后的视差存成 .npy（可复现）"""
    p = {**DEFAULTS, **(p or {})}
    if not p.get("enabled"):
        return im, {"version": VERSION, "enabled": False}
    from PIL import Image
    rgb = np.asarray(im.convert("RGB"))
    if (p.get("optics_v2") or {}).get("enabled"):
        from . import lens_sim_v2
        return lens_sim_v2.apply_v2(im, p, depth=depth, depth_out=depth_out)
    d = prepare_depth(rgb, p, depth)
    if depth_out is not None:
        Path(depth_out).parent.mkdir(parents=True, exist_ok=True)
        np.save(str(depth_out), d)
    faces = detect_faces(rgb) if p["focus"] in ("auto", "all_faces") else []
    fi = pick_focus(d, faces, p["focus"], p["dof_band"], p["all_faces_min_ratio"])
    y = rgb.astype(np.float32) / 255.0
    out, rec = lens_array(y, d, p, fi)
    rec.update(enabled=True, input_sha16=_sha16(rgb), depth_model=str(Path(p["depth_model"]).name) if depth is None else "given",
               depth_sha16=_sha16((d * 65535).astype(np.uint16)))
    return Image.fromarray((out * 255 + 0.5).astype(np.uint8)), rec


def depth_preview(d: np.ndarray):
    """视差 → 灰度 PIL（看图用）"""
    from PIL import Image
    return Image.fromarray((np.clip(d, 0, 1) * 255 + 0.5).astype(np.uint8))
