"""生成契约的像素度量（REFLOCK 11-01）：postflight（笔记本）与 gpu33 服务器打分（tools/gen_contract/clip_srv.py）共用同一份代码，
保证同一模型、同一滑窗、同一阈值口径。只依赖 numpy / cv2 / PIL；CLIP 用 transformers，模型目录可由 NS_CLIP_PATH 指定（gpu33 上是
ModelScope 下的同一份权重，sha256 a2bf730a0c7d… = HF openai/clip-vit-large-patch14@32bd642）。"""
from __future__ import annotations

import os
from functools import lru_cache

import numpy as np

ZONE_REGION = {"feet": (0.50, 1.0), "lower": (0.30, 0.95), "upper": (0.0, 0.75), "body": (0.0, 1.0), "neck": (0.0, 0.6),
               "hand": (0.0, 1.0), "waist": (0.25, 0.8), None: (0.0, 1.0)}


def _shares_gpu_with_local_lane() -> bool:
    """LOCALGPU（11-01）：本机有启用的出图道（config/render_endpoints.json lanes 里 image_only 且没 disabled，如 laptop:0）
    = 这张卡归出图用 → CLIP 不上卡。11-01 19:22 实测：契约 CLIP 在同卡上，ComfyUI 被挤进共享显存，每步 5 s → 40 s。
    gpu33 上（remote_score 下发本文件）没有这份配置 → False，行为不变"""
    try:
        import json
        from pathlib import Path
        cfg = Path(__file__).resolve().parents[4] / "config" / "render_endpoints.json"
        lanes = json.loads(cfg.read_text(encoding="utf-8")).get("lanes") or {}
        return any(isinstance(v, dict) and v.get("image_only") and not v.get("disabled") for v in lanes.values())
    except Exception:  # noqa: BLE001
        return False


@lru_cache(maxsize=1)
def _clip():
    import torch
    from transformers import CLIPModel, CLIPProcessor
    name = os.environ.get("NS_CLIP_PATH") or "openai/clip-vit-large-patch14"
    dev = os.environ.get("NS_CLIP_DEVICE") or ("cuda" if torch.cuda.is_available() and not _shares_gpu_with_local_lane() else "cpu")
    m = CLIPModel.from_pretrained(name, local_files_only=True).to(dev).eval()
    p = CLIPProcessor.from_pretrained(name, local_files_only=True)
    return m, p, dev


def embed(images) -> np.ndarray:
    import torch
    m, p, dev = _clip()
    out = []
    for i in range(0, len(images), 32):
        with torch.no_grad():
            x = p(images=images[i:i + 32], return_tensors="pt").to(dev)
            e = m.visual_projection(m.vision_model(pixel_values=x["pixel_values"]).pooler_output)
            e = e / e.norm(dim=-1, keepdim=True)
        out.append(e.float().cpu().numpy())
    return np.concatenate(out) if out else np.zeros((0, 768), np.float32)


def embed_text(texts) -> np.ndarray:
    import torch
    m, p, dev = _clip()
    with torch.no_grad():
        x = p(text=list(texts), return_tensors="pt", padding=True).to(dev)
        e = m.text_projection(m.text_model(input_ids=x["input_ids"], attention_mask=x["attention_mask"]).pooler_output)
        e = e / e.norm(dim=-1, keepdim=True)
    return e.float().cpu().numpy()


def probe(img, texts) -> list[float]:
    """CLIP 零样本：图与各句的 softmax 概率（logit_scale 同模型）"""
    import torch
    m, _p, _d = _clip()
    ie = embed([img])[0]
    te = embed_text(texts)
    logits = float(m.logit_scale.exp().item()) * (te @ ie)
    ex = np.exp(logits - logits.max())
    return [round(float(v), 4) for v in ex / ex.sum()]


FACE_PROBE = ("a close-up photo of a plastic doll-like airbrushed CGI face with smooth waxy skin",
              "a close-up photograph of a real human face with natural skin texture and pores")
SHOE_PROBE = ("a plain clear transparent high heel shoe with no decoration",
              "a high heel shoe decorated with crystals, rhinestones, jewels or an ankle strap")


def face_texture(img, face_bbox) -> dict | None:
    """脸部区域质感（总调度 11-01：整图 Laplacian 留出复核 AUC 0.41 作废，改量脸）：脸框中间 60% 缩到 256 高（按脸大小归一）→
    高频能量 |g − GaussianBlur σ2| 均值、Laplacian 方差、脸部镜面高光占比（油）、CLIP“塑料娃娃脸 vs 真人照片”探针"""
    import cv2
    if not face_bbox:
        return None
    a = np.asarray(img.convert("RGB"))
    H, W = a.shape[:2]
    x1, y1, x2, y2 = [int(v) for v in face_bbox]
    w, h = x2 - x1, y2 - y1
    if w < 16 or h < 16:
        return None
    cx0, cy0, cx1, cy1 = max(0, x1 + w // 5), max(0, y1 + h // 5), min(W, x2 - w // 5), min(H, y2 - h // 5)
    crop = a[cy0:cy1, cx0:cx1]
    if crop.size == 0 or crop.shape[0] < 16 or crop.shape[1] < 16:
        # FACTORY 11-02 15:xx（sv-11 后 gpu33 score_infra 3/36）：脸框落在图外 / post size 换算后越界 → 裁出空数组，
        # cvtColor 抛 -215 !_src.empty()，整单打分失败。越界当“量不了”，和脸框太小同口径
        return None
    g = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY).astype(np.float32)
    g = cv2.resize(g, (max(8, int(256 * g.shape[1] / g.shape[0])), 256), interpolation=cv2.INTER_AREA)
    hf = float(np.abs(g - cv2.GaussianBlur(g, (0, 0), 2)).mean())
    lap = float(cv2.Laplacian(g, cv2.CV_32F).var())
    hsv = cv2.cvtColor(crop, cv2.COLOR_RGB2HSV)
    oil = float(((hsv[..., 2] > 235) & (hsv[..., 1] < 60)).mean())
    from PIL import Image as _I
    full = _I.fromarray(a[max(0, y1 - h // 4):min(H, y2 + h // 4), max(0, x1 - w // 4):min(W, x2 + w // 4)])
    pp = probe(full, FACE_PROBE)
    return {"face_hf": round(hf, 3), "face_lap": round(lap, 1), "face_oil": round(oil, 4), "p_plastic": pp[0], "face_px": int(h)}


def _ref_crop(im):
    """单品产品图：去掉四周 8% 的布景边"""
    w, h = im.size
    return im.crop((int(w * .08), int(h * .08), int(w * .92), int(h * .92)))


def windows(im, region=(0.0, 1.0), fracs=(0.30, 0.45, 0.65)):
    W, H = im.size
    y0, y1 = int(region[0] * H), int(region[1] * H)
    boxes = []
    for f in fracs:
        s = int(f * W)
        if s > (y1 - y0):
            s = y1 - y0
        st = max(16, s // 2)
        for y in range(y0, max(y0, y1 - s) + 1, st):
            for x in range(0, max(0, W - s) + 1, st):
                boxes.append((x, y, x + s, y + s))
    return boxes


def hs_hist(im) -> np.ndarray:
    import cv2
    a = cv2.cvtColor(np.asarray(im.convert("RGB")), cv2.COLOR_RGB2HSV)
    h = cv2.calcHist([a], [0, 1], None, [16, 8], [0, 180, 0, 256])
    return cv2.normalize(h, h).flatten()


def hist_corr(a, b) -> float:
    import cv2
    return float(cv2.compareHist(hs_hist(a).astype("float32"), hs_hist(b).astype("float32"), cv2.HISTCMP_CORREL))


def region_match(img, ref, zone=None) -> dict:
    """生成图里与参考（单品 / 设定图）最像的区域：CLIP 余弦最高的窗 + 该窗与参考的颜色直方图相关"""
    boxes = windows(img, ZONE_REGION.get(zone, (0, 1)))
    crops = [img.crop(b) for b in boxes]
    e = embed(crops + [ref])
    sims = e[:-1] @ e[-1]
    k = int(np.argmax(sims))
    return {"clip": round(float(sims[k]), 4), "box": boxes[k], "hist": round(hist_corr(crops[k], ref), 4)}


# ---------------------------------------------------------------- Q4 质感
def texture(img, face_box=None) -> dict:
    import cv2
    a = np.asarray(img.convert("RGB"))
    H, W = a.shape[:2]
    if face_box:
        x1, y1, x2, y2 = [int(v) for v in face_box]
        pad = int(0.1 * (y2 - y1))
        crop = a[max(0, y1 + pad):min(H, y2 - pad), max(0, x1 + pad):min(W, x2 - pad)]
        src = "face"
    else:
        crop = a[int(H * .1):int(H * .45), int(W * .25):int(W * .75)]
        src = "center"
    if crop.size == 0 or crop.shape[0] < 16 or crop.shape[1] < 16:   # 越界脸框裁出空数组：先判，不进 cvtColor（同上）
        return {"src": src, "error": "区域太小"}
    g = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    g = cv2.resize(g, (int(256 * g.shape[1] / g.shape[0]), 256))
    lap = float(cv2.Laplacian(g, cv2.CV_64F).var())
    f = np.abs(np.fft.fftshift(np.fft.fft2(g.astype(np.float32) - g.mean()))) ** 2
    cy, cx = np.array(f.shape) // 2
    yy, xx = np.ogrid[:f.shape[0], :f.shape[1]]
    r = np.sqrt(((yy - cy) / (f.shape[0] / 2)) ** 2 + ((xx - cx) / (f.shape[1] / 2)) ** 2)
    hf = float(f[r > 0.25].sum() / max(f.sum(), 1e-9))
    hsv = cv2.cvtColor(a, cv2.COLOR_RGB2HSV)
    sat95 = float(np.percentile(hsv[..., 1], 95) / 255.0)
    ch = cv2.cvtColor(crop, cv2.COLOR_RGB2HSV)
    spec = float(((ch[..., 2] > 235) & (ch[..., 1] < 40)).mean())
    # 判据（K-REV-232 第二轮，总调度认可集标定）：整图缩到宽 768 的 Laplacian 方差 + 整图平均饱和度；脸框 / 中心区域的
    # 指标只记录（区域大小随景别变，绝对值不可比：同一张图中心 265、脸框 38）
    gf = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY)
    if W != 768:
        gf = cv2.resize(gf, (768, int(round(H * 768 / W))), interpolation=cv2.INTER_AREA)
    lap_full = float(cv2.Laplacian(gf.astype(np.float32), cv2.CV_32F).var())
    sat_mean = float(hsv[..., 1].mean() / 255.0)
    return {"src": src, "lap_full": round(lap_full, 1), "sat_mean": round(sat_mean, 3), "specular": round(spec, 4),
            "region_lap_var": round(lap, 1), "region_hf_ratio": round(hf, 4), "sat_p95": round(sat95, 3)}




def post_like(im, size=None, usm=None):
    """与 ns_render.image_params.post_file 相同的后处理（PIL lanczos 缩回 → UnsharpMask），服务器上用原始输出复现交付图"""
    from PIL import ImageFilter, Image
    if size and tuple(im.size) != tuple(size):
        im = im.resize(tuple(size), Image.LANCZOS)
    if usm:
        im = im.filter(ImageFilter.UnsharpMask(**usm))
    return im


def outer_presence(img, face_bbox, en: str, texts=None, region: str = "torso") -> float | None:
    """外层单品（西装外套 / 风衣 / 大衣…）穿没穿（总调度 11-01：ep01 S1–S3 没穿外套却过了 Q2）：
    按这个人的脸框取躯干（下巴往下约 3 个脸高、左右各 1.8 个脸宽），CLIP 零样本“穿着 <外层> / 只穿衬衫没有 <外层>”，
    返回“穿着”的概率。不看颜色直方图。没有脸框 → None（未测）"""
    if not face_bbox:
        return None
    x1, y1, x2, y2 = [float(v) for v in face_bbox]
    fw, fh = x2 - x1, y2 - y1
    cx = (x1 + x2) / 2
    W, H = img.size
    if region == "head":                         # 发型：头部 + 两侧到肩（长发会垂过下巴）
        box = (int(max(0, cx - 1.6 * fw)), int(max(0, y1 - 0.8 * fh)), int(min(W, cx + 1.6 * fw)), int(min(H, y2 + 1.8 * fh)))
    else:
        box = (int(max(0, cx - 1.8 * fw)), int(min(H - 2, y2)), int(min(W, cx + 1.8 * fw)), int(min(H, y2 + 3.0 * fh)))
    if box[3] - box[1] < 16 or box[2] - box[0] < 16:
        return None
    crop = img.crop(box)
    texts = tuple(texts) if texts else (f"a photo of a woman wearing a {en} over her top, the {en} is clearly visible",
                                        f"a photo of a woman wearing only a blouse or shirt, with no {en}")
    return probe(crop, texts)[0]


def outer_rows_with_faces(outer, faces, floor: float = 0.3) -> list[dict]:
    """笔记本路径（measure_local）：按 remote faces 给每个外层行配上该角色自己的脸框（与 remote_score._FULL 同口径）"""
    out = []
    for o in outer or []:
        cand = [f for f in faces or [] if ((f.get("sims") or {}).get(o.get("character")) or -1) >= floor]
        best = max(cand, key=lambda f: f["sims"][o["character"]], default=None)
        out.append({**o, "face_bbox": list(best["bbox"]) if best else None})
    return out


def measure(img, rows: list[dict], refs: dict, face_bbox=None, scene_ref=None, outer=None) -> dict:
    """rows: [{label, role, zone, source, file, ref: refs 的键}]；refs: 键 → PIL 图（已按 source 处理过 _ref_crop 前的原图）"""
    out = {"Q2": [], "Q4": None, "Q7": None}
    for m in rows:
        ref = refs[m["ref"]]
        if m.get("source") == "wardrobe":
            ref = _ref_crop(ref)
        r = region_match(img, ref, m.get("zone") if m.get("role") == "item" else None)
        if m.get("zone") == "feet":                       # 鞋：额外的“加装饰 vs 素款”探针（总调度 11-01）
            r["p_embellished"] = probe(img.crop(tuple(r["box"])), SHOE_PROBE)[1]
        out["Q2"].append({"label": m.get("label"), "role": m["role"], "zone": m.get("zone"), "source": m.get("source"),
                          "file": m.get("file"), **r})
    out["Q4"] = texture(img, face_bbox)
    ft = face_texture(img, face_bbox)
    if ft:
        out["Q4"] = {**(out["Q4"] or {}), **ft}
    out["Q8"] = hair(img, face_bbox) if face_bbox else None
    def _fb(o):                                   # 行里带了这个角色自己的脸框就用它；没带时林晚用主脸框，其他角色未测
        if "face_bbox" in o:
            return o["face_bbox"]
        from ..cast_table import lead_id                         # OVERSIGHT-F P2：主脸框归女主（按剧取，不写死 lin_wan）
        lead = lead_id() or "lin_wan"
        return face_bbox if o.get("character", lead) == lead else None
    out["outer"] = [{k: v for k, v in {**o, "p": (lambda v: None if v is None else round(float(v), 4))(
        outer_presence(img, _fb(o), o.get("en") or "jacket", o.get("probe"), o.get("region") or "torso"))}.items() if k != "face_bbox"} for o in outer or []]
    if scene_ref is not None:
        e = embed([img, scene_ref])
        out["Q7"] = {"file": None, "clip": round(float(e[0] @ e[1]), 4)}
    return out


# ---------------------------------------------------------------- Q8 发色（总调度 11-01：ep01-S5 发色偏暗灰）
def _hair_px(rgb, x, y, w, h):
    """头顶带 + 脸两侧各半个脸宽里的蓝色发丝像素（OpenCV H 85–130、S ≥30、V ≥30）——同 VIDEO10 score_clip._hair_px"""
    import cv2
    H, W = rgb.shape[:2]
    boxes = [(max(0, x - w // 2), max(0, y - int(0.45 * h)), min(W, x + w + w // 2), max(1, y + int(0.05 * h))),
             (max(0, x - w // 2), max(0, y), max(0, x), min(H, y + h)),
             (min(W, x + w), max(0, y), min(W, x + w + w // 2), min(H, y + h))]
    px = []
    for x0, y0, x1, y1 in boxes:
        sub = rgb[y0:y1, x0:x1] if x1 > x0 and y1 > y0 else rgb[:0, :0]
        if sub.size:                       # 越界脸框（落在图外）切出空数组：跳过，不进 cvtColor（FACTORY 11-02 15:xx）
            hsv = cv2.cvtColor(np.ascontiguousarray(sub), cv2.COLOR_RGB2HSV).reshape(-1, 3)
            px.append(hsv[(hsv[:, 0] >= 85) & (hsv[:, 0] <= 130) & (hsv[:, 1] >= 30) & (hsv[:, 2] >= 30)])
    return np.concatenate(px) if px else np.zeros((0, 3), np.uint8)


def hair(img, face_bbox) -> dict | None:
    """蓝色发丝 HSV 中位数 + 同帧脸部肤色亮度（脸框中间 50%，HSV V 中位）→ 相对亮度 rel_v = 发 V / 肤 V（暗场不误判）。
    face_bbox = [x1, y1, x2, y2]；发丝像素 < 200 → None（未测）"""
    import cv2
    if not face_bbox:
        return None
    rgb = np.asarray(img.convert("RGB"))
    x1, y1, x2, y2 = [int(v) for v in face_bbox]
    w, h = max(1, x2 - x1), max(1, y2 - y1)
    px = _hair_px(rgb, x1, y1, w, h)
    if len(px) < 200:
        return {"n": int(len(px)), "note": "没检出足够的蓝色发丝"}
    med = np.median(px.astype(np.float32), axis=0)
    cx0, cy0 = max(0, x1 + w // 4), max(0, y1 + h // 4)     # 负下标会从另一头切：先夹到 0
    sub = rgb[cy0:cy0 + h // 2, cx0:cx0 + w // 2]
    if not sub.size:                       # 脸框越界：肤色量不了，同“发丝不够”一样记未测
        return {"n": int(len(px)), "note": "脸框越界，量不了同帧肤色"}
    skin = cv2.cvtColor(np.ascontiguousarray(sub), cv2.COLOR_RGB2HSV).reshape(-1, 3)
    sv = float(np.median(skin[:, 2])) if len(skin) else 0.0
    return {"n": int(len(px)), "hsv": [round(float(v), 1) for v in med], "skin_v": round(sv, 1),
            "rel_v": round(float(med[2]) / sv, 3) if sv > 0 else None}
