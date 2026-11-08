"""
多候选自动评分（REQUIREMENTS C5）：每张候选首帧/尾帧打 0–1 分，选最高的设为当前版本。

  fidelity  剧本细节：本地看图模型（ComfyUI TextGenerate + Qwen3-VL 8B，与出图共用编码器）逐条核对 facts
            （剧本 img 原文分句 + motion 起点 + 导演加强），返回满足比例与缺失条目
  face      林晚脸与身份底板的相似度：NudeNet 找脸 → CLIP ViT-L/14 图像嵌入余弦（无 ArcFace 时的替代）
  colour    与色调参考图的 LAB 距离（暗场/夜景放宽）；尾帧对比首帧
  sharp     主体（脸框或画面中央）Laplacian 方差 → Leica 锐度
  bokeh     主体锐度 / 背景锐度 → 大光圈焦外
  nudity    只记录在 Take.qc，不参与评分（2026-10-30 取消成人过滤器）
权重见 WEIGHTS；缺项（如背影镜头没有脸）按剩余权重归一。
"""
import json
import math
import re
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import grade as G
from . import workflows as W

WEIGHTS = {"fidelity": 0.40, "face": 0.15, "realism": 0.15, "colour": 0.10, "sharp": 0.10, "bokeh": 0.10}
PASS = 0.70                 # 低于此分进入下一轮（≤3 轮），三轮后仍低 → needs_review
MAX_FACTS = 14
VLM_PROMPT = ("你是严格的短剧分镜质检员。请仔细看图，逐条判断下列描述在画面中是否成立；"
              "看不出、不符合或只部分符合都判 false。\n{items}\n"
              "只输出 JSON，键是编号，值是 true 或 false，例如 {{\"1\": true, \"2\": false}}。")
VLM_PROMPT_LAST = ("图中左半边是视频首帧，右半边是候选尾帧。请判断右半边是否满足下列描述；"
                   "看不出、不符合都判 false。\n{items}\n"
                   "只输出 JSON，键是编号，值是 true 或 false。")
CONTINUITY = "右半边与左半边是同一场景、同一人物、同一服装、同一机位景别"
# A1 真人感（项目方：“林晚还是很假”）：本地看图模型给 0–10 分
REALISM_PROMPT = ("这张图看起来是真实相机拍摄的照片，还是 AI 生成/CG 渲染的图？从 0 到 10 打分：10 = 完全像真实照片"
                  "（真实皮肤毛孔与瑕疵、自然光线、真实比例的五官），0 = 明显的 AI 图（塑料皮肤、娃娃脸、过度磨皮、CG 感）。"
                  "只输出一个 0 到 10 的整数。")


def realism_check(client, image: Path, timeout_s: float = 300) -> float | None:
    name = client.upload_image(image, subfolder="ns_final/qc")
    entry, _ = client.run(W.text_generate(REALISM_PROMPT, "ns_final/vlm", image=name, max_length=8), timeout_s=timeout_s)
    text = "".join(entry.get("outputs", {}).get("90", {}).get("text", []) or [])
    m = re.search(r"\d+(?:\.\d+)?", text)
    return round(min(10.0, float(m.group(0))) / 10, 3) if m else None


# ------------------------------------------------------------------ fidelity（本地 VLM）

def parse_bools(text: str, n: int) -> dict[int, bool]:
    out = {}
    for k, v in re.findall(r"\"?(\d+)\"?\s*[:：]\s*(true|false|是|否)", text, re.I):
        i = int(k)
        if 1 <= i <= n:
            out[i] = v.lower() in ("true", "是")
    return out


def vlm_check(client, image: Path, facts: list[str], prompt_tpl: str = VLM_PROMPT, timeout_s: float = 300) -> dict:
    facts = facts[:MAX_FACTS]
    if not facts:
        return {"score": None, "missing": [], "raw": ""}
    items = "\n".join(f"{i}. {f}" for i, f in enumerate(facts, 1))
    name = client.upload_image(image, subfolder="ns_final/qc")
    wf = W.text_generate(prompt_tpl.format(items=items), "ns_final/vlm", image=name, max_length=40 + 12 * len(facts))
    entry, secs = client.run(wf, timeout_s=timeout_s)
    text = "".join(entry.get("outputs", {}).get("90", {}).get("text", []) or [])
    got = parse_bools(text, len(facts))
    ok = [got.get(i, False) for i in range(1, len(facts) + 1)]
    return {"score": round(sum(ok) / len(facts), 3), "missing": [f for f, k in zip(facts, ok) if not k],
            "raw": text[:400], "seconds": round(secs, 1), "parsed": len(got)}


# ------------------------------------------------------------------ 人脸（NudeNet 找脸 + CLIP 嵌入）

def _gray(path: Path) -> np.ndarray:
    import cv2
    return cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)


def face_box(path: Path) -> tuple[int, int, int, int] | None:
    from .qc import _nudenet
    faces = [d for d in _nudenet().detect(str(path)) if d["class"].startswith("FACE_") and d["score"] >= 0.35]
    if not faces:
        return None
    x, y, w, h = max(faces, key=lambda d: d["box"][2] * d["box"][3])["box"]
    return int(x), int(y), int(w), int(h)


@lru_cache(maxsize=32)
def _plate_embedding(path: str):
    return _face_embedding(Path(path))


def _face_embedding(path: Path):
    import torch
    from PIL import Image
    from .qc import _clip
    box = face_box(path)
    if not box:
        return None
    x, y, w, h = box
    m = int(0.25 * max(w, h))
    im = Image.open(path).convert("RGB")
    crop = im.crop((max(0, x - m), max(0, y - m), min(im.width, x + w + m), min(im.height, y + h + m)))
    model, proc = _clip()
    with torch.no_grad():
        f = model.get_image_features(**proc(images=crop, return_tensors="pt"))
    if not isinstance(f, torch.Tensor):
        f = getattr(f, "image_embeds", None) or f.pooler_output
    return torch.nn.functional.normalize(f, dim=-1)[0]


def face_similarity(image: Path, plate: Path) -> float | None:
    a, b = _face_embedding(image), _plate_embedding(str(plate))
    if a is None or b is None:
        return None
    return round(float((a * b).sum()), 3)


# ------------------------------------------------------------------ 锐度 / 焦外

def sharpness(path: Path, box=None) -> tuple[float, float]:
    """返回 (主体 Laplacian 方差, 背景 Laplacian 方差)。主体 = 脸框外扩，没有脸用画面中央 40%"""
    import cv2
    g = _gray(path)
    H, Wd = g.shape
    lap = cv2.Laplacian(g, cv2.CV_64F)
    if box:
        x, y, w, h = box
        m = int(0.15 * max(w, h))
        x0, y0, x1, y1 = max(0, x - m), max(0, y - m), min(Wd, x + w + m), min(H, y + h + m)
    else:
        x0, y0, x1, y1 = int(Wd * .3), int(H * .3), int(Wd * .7), int(H * .7)
    subj = lap[y0:y1, x0:x1].var()
    mask = np.ones_like(lap, bool)
    # 背景 = 画面四周（去掉主体框外扩 2 倍的区域）
    cx, cy, hw, hh = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0), (y1 - y0)
    mask[int(max(0, cy - hh)):int(min(H, cy + hh)), int(max(0, cx - hw)):int(min(Wd, cx + hw))] = False
    bg = lap[mask].var() if mask.any() else subj
    return float(subj), float(bg)


def smooth_fraction(path: Path, block: int = 16) -> float:
    """焦外占比：缩小 4 倍（去掉生成噪点）后，细节方差 < 第 90 百分位 5% 的块所占比例（奶油焦外/干净背景）。
    项目方参考图约 0.26。比“背景环”稳：特写里头发会占满背景环"""
    import cv2
    g = _gray(path)
    g = cv2.resize(g, (max(1, g.shape[1] // 4), max(1, g.shape[0] // 4)), interpolation=cv2.INTER_AREA)
    lap = cv2.Laplacian(g, cv2.CV_64F)
    H, Wd = lap.shape
    h, w = max(1, H // block), max(1, Wd // block)
    v = lap[:h * block, :w * block].reshape(h, block, w, block).var(axis=(1, 3))
    return float((v < 0.05 * max(float(np.percentile(v, 90)), 1e-6)).mean())


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


# ------------------------------------------------------------------ 汇总

def score_image(path: Path, *, client=None, facts: list[str] | None = None, plate: Path | None = None,
                dark: bool = False, qc: dict | None = None, colour_ref: Path | None = None,
                vlm_image: Path | None = None, vlm_prompt: str = VLM_PROMPT) -> dict:
    parts, reasons, missing = {}, [], []
    if client is not None and facts:
        try:
            v = vlm_check(client, vlm_image or path, facts, vlm_prompt)
            if v["score"] is not None:
                parts["fidelity"] = v["score"]
                missing = v["missing"]
                if v.get("parsed", 0) < len(facts[:MAX_FACTS]):
                    reasons.append(f"VLM 只解析到 {v.get('parsed')}/{len(facts[:MAX_FACTS])} 条")
        except Exception as e:  # noqa: BLE001 — VLM 不可用时其余分项照算
            reasons.append(f"VLM 失败：{type(e).__name__}: {str(e)[:120]}")
    if client is not None:
        try:
            r = realism_check(client, path)
            if r is not None:
                parts["realism"] = r
        except Exception as e:  # noqa: BLE001
            reasons.append(f"真人感评分失败：{type(e).__name__}")
    box = None
    try:
        box = face_box(path)
    except Exception as e:  # noqa: BLE001
        reasons.append(f"找脸失败：{type(e).__name__}")
    if plate is not None and box:
        sim = face_similarity(path, plate)
        if sim is not None:
            parts["face"] = round(_clip01((sim - 0.55) / 0.35), 3)
            parts["face_cos"] = sim
    d = G.colour_distance(path, colour_ref, dark=dark)
    parts["colour_dist"] = round(d, 2)
    parts["colour"] = round(math.exp(-d / (30.0 if dark else 18.0)), 3)
    subj, bg = sharpness(path, box)
    parts["sharp_var"], parts["bg_var"] = round(subj, 1), round(bg, 1)
    parts["sharp"] = round(_clip01(math.log10(max(subj, 1.0)) / 3.0), 3)          # 方差 1000 → 1.0
    frac = smooth_fraction(path)
    parts["smooth_frac"] = round(frac, 3)
    parts["bokeh"] = round(_clip01(frac / 0.30), 3)     # 30% 画面是柔焦/干净背景 → 1.0（参考图 0.26）
    keys = [k for k in WEIGHTS if k in parts]
    total = sum(WEIGHTS[k] * parts[k] for k in keys) / sum(WEIGHTS[k] for k in keys)
    if "fidelity" in parts and parts["fidelity"] < 0.5:
        reasons.append("剧本细节不到一半")
    return {"total": round(total, 3), "parts": parts, "missing": missing, "reasons": reasons,
            "weights": {k: WEIGHTS[k] for k in keys}}


def side_by_side(first: Path, cand: Path, out: Path, height: int = 960) -> Path:
    """尾帧质检用：左 = 首帧，右 = 候选尾帧"""
    from PIL import Image
    a, b = Image.open(first).convert("RGB"), Image.open(cand).convert("RGB")
    a = a.resize((round(a.width * height / a.height), height))
    b = b.resize((round(b.width * height / b.height), height))
    im = Image.new("RGB", (a.width + b.width + 16, height), (255, 255, 255))
    im.paste(a, (0, 0))
    im.paste(b, (a.width + 16, 0))
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out)
    return out


def dumps(score: dict) -> str:
    return json.dumps(score, ensure_ascii=False)
