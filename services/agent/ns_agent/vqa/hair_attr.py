"""Q1a 逐人头发属性（VQAGATE 11-02 P0，项目方截图 ep06-S2 v6：顾沉长出了林晚的蓝色长直发）。

确定性部分（decider=code）：契约 Q1 已把每张脸认到具体角色（ArcFace，证据 boxes）→ 每张脸裁头肩跑 MediaPipe 多类分割
（models_cache/vision/selfie_multiclass_256x256.tflite，hair 类），只认与这张脸头顶相连的头发连通域（背对镜头、脸没检出的旁人
头发不划过来），量：
  hair_blue  头发里蓝色像素占比（HSV 185–240°，S ≥0.15，V ≥0.12）
  below      下巴以下 0.3 脸高之外的头发面积 / 脸框面积（长发）
按人物卡（config/gen_contract.json Q1a_attr）判：短发角色 below ≥ below_max = 长发；非蓝发角色 hair_blue ≥ blue_max = 蓝发；
蓝发角色 hair_blue < blue_min = 发色不对。标定 docs/vqa/GATE_CALIBRATION.md §18（106 张，6 个错例全拦、正常零误拦）。
VLM 部分（decider=local_model，K-REV-V01 只进本地）：同一裁图问性别 / 发色 / 发长，与人物卡不符 = 不过。
限线程（K-OPS-V13）：分割在 producer 进程内跑，mediapipe 用 XNNPACK 单图推理；cv2.setNumThreads(4)。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[4]
MODEL = ROOT / "models_cache" / "vision" / "selfie_multiclass_256x256.tflite"
_SEG = None
DILATE = 0.0           # 标定见 GATE_CALIBRATION §18：0.15 / 0.25 / 0.35 都带来误拦（018 旁人蓝发、084 黑发），ep06-S1 v8 的缺口要 ≥0.45 才连上 → 不膨胀，这类交看图逐人问答


class Unavailable(RuntimeError):
    """分割模型 / mediapipe 不在 → 调用方记 Q1a untested + 报警（不静默降级，SYSARCH 11-02）"""


def _seg():
    global _SEG
    if _SEG is None:
        if not MODEL.exists():
            raise Unavailable(f"缺模型 {MODEL.name}")
        try:
            from mediapipe.tasks.python import BaseOptions
            from mediapipe.tasks.python import vision as V
        except ImportError as e:
            raise Unavailable(f"没有 mediapipe：{e}") from e
        _SEG = V.ImageSegmenter.create_from_options(
            V.ImageSegmenterOptions(base_options=BaseOptions(model_asset_path=str(MODEL)), output_category_mask=True))
    return _SEG


def _hsv(a: np.ndarray):
    a = a.astype(np.float32) / 255
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx, mn = a.max(-1), a.min(-1)
    d = mx - mn + 1e-6
    h = np.where(mx == r, ((g - b) / d) % 6, np.where(mx == g, (b - r) / d + 2, (r - g) / d + 4)) * 60
    s = np.where(mx > 0, d / (mx + 1e-6), 0)
    return h, s, mx


def hair_mask_feats(rgb: np.ndarray, boxes: list, k: int, mask: np.ndarray, off: tuple[int, int]) -> dict:
    """纯函数（便于测试）：rgb = 裁图，mask = 同尺寸类别图（1 = 头发），off = 裁图左上角在原图的坐标，boxes 为原图坐标"""
    import cv2
    x1, y1, x2, y2 = boxes[k]
    fw, fh = max(1.0, x2 - x1), max(1.0, y2 - y1)
    ox, oy = off
    H, W = mask.shape[:2]
    yy, xx = np.mgrid[oy:oy + H, ox:ox + W]
    fx, fy = (x1 + x2) / 2, (y1 + y2) / 2
    hair = mask.reshape(H, W) == 1
    for j, b in enumerate(boxes):                 # 多人：按归一化距离划给最近的脸
        if j == k:
            continue
        bx, by, bw = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2, max(1.0, b[2] - b[0])
        hair &= np.hypot((xx - fx) / fw, (yy - fy) / fw) <= np.hypot((xx - bx) / bw, (yy - by) / bw)
    # 求连通域前先膨胀（DILATE × 脸宽）：同一个人垂在脑后的长发常被下巴 / 脖子皮肤隔开 0.3–0.4 脸宽（ep06-S1 v8 顾沉），
    # 不膨胀会漏；计数仍只算原始头发像素
    r = max(3, int(DILATE * fw))
    near = cv2.dilate(hair.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)))
    _n, lab = cv2.connectedComponents(near, connectivity=8)
    lab = np.where(hair, lab, 0)
    crown = (yy >= y1 - 0.5 * fh) & (yy <= y1 + 0.25 * fh) & (xx >= x1 + 0.1 * fw) & (xx <= x2 - 0.1 * fw)
    keep = sorted(set(np.unique(lab[crown & hair]).tolist()) - {0})
    hair = np.isin(lab, keep) if keep else np.zeros_like(hair)
    hair &= (xx >= x1 - 0.9 * fw) & (xx <= x2 + 0.9 * fw)
    h, s, v = _hsv(rgb)
    blue = (h >= 185) & (h <= 240) & (s >= 0.15) & (v >= 0.12)
    n = int(hair.sum())
    return {"hair_px": round(n / (fw * fh), 3), "hair_blue": round(float((hair & blue).sum() / max(1, n)), 3),
            "below": round(float((hair & (yy > y2 + 0.3 * fh)).sum() / (fw * fh)), 3)}


def crop_box(W: int, H: int, box) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = box
    fw, fh = x2 - x1, y2 - y1
    return (int(max(0, x1 - 1.2 * fw)), int(max(0, y1 - 1.0 * fh)), int(min(W, x2 + 1.2 * fw)), int(min(H, y2 + 2.5 * fh)))


def measure(image, boxes: list) -> list[dict]:
    """→ 每个框的头发特征；分割不可用抛 Unavailable"""
    import cv2
    import mediapipe as mp
    from PIL import Image
    cv2.setNumThreads(4)
    a = np.asarray(Image.open(image).convert("RGB"))
    H, W = a.shape[:2]
    out = []
    for k, b in enumerate(boxes):
        cx1, cy1, cx2, cy2 = crop_box(W, H, b)
        c = np.ascontiguousarray(a[cy1:cy2, cx1:cx2])
        m = _seg().segment(mp.Image(image_format=mp.ImageFormat.SRGB, data=c)).category_mask.numpy_view()
        out.append(hair_mask_feats(c, boxes, k, np.asarray(m), (cx1, cy1)))
    return out


def judge(cid: str, f: dict, card: dict) -> list[str]:
    """人物卡 card = {"name", "hair_short": bool, "blue_max" | "blue_min", "below_max"} → 不符原因（空 = 过）"""
    bad = []
    nm = card.get("name", cid)
    if card.get("hair_short") and f["below"] >= card.get("below_max", 0.15):
        bad.append(f"{nm} 应为短发，下巴以下有头发 {f['below']:.2f} ≥ {card.get('below_max', 0.15)}（长发）")
    if card.get("blue_max") is not None and f["hair_blue"] >= card["blue_max"]:
        bad.append(f"{nm} 不该是蓝发，头发蓝色占比 {f['hair_blue']:.2f} ≥ {card['blue_max']}")
    if card.get("blue_min") is not None and f["hair_px"] >= card.get("min_hair_px", 0.15) and f["hair_blue"] < card["blue_min"]:
        bad.append(f"{nm} 应为雾霾蓝发，头发蓝色占比 {f['hair_blue']:.2f} < {card['blue_min']}")
    return bad


VLM_Q = ("看【待检】图里红框框住的那张脸是谁的、这个人的样子（红框外的其他人不算），只按画面回答，输出 JSON："
         '{"gender": "男|女|看不清", "hair_color": "黑|棕|蓝|灰银|金|其它|看不清", "hair_length": "短|及肩|长|看不清", '
         '"nails": "长美甲|普通|看不见", "e": "一句依据"}')


def vlm_ask(image, box) -> dict | None:
    """本地看图逐人问答（K-REV-V01）；服务不可用 → None"""
    from PIL import Image

    from . import client
    from .contract import ROOT as VROOT, rubric_loads
    im = Image.open(image).convert("RGB")
    fp = VROOT / "data" / "vqa" / "crops" / f"{Path(str(image)).stem}_q1a_{int(box[0])}_{int(box[1])}.jpg"
    fp.parent.mkdir(parents=True, exist_ok=True)
    # 红框标出要问的那张脸（11-02 正常样本 057：裁图里旁边林晚占了正中，“正中这个人”有歧义，陈宇被答成蓝发女性）
    from PIL import ImageDraw
    cx1, cy1, cx2, cy2 = crop_box(im.width, im.height, box)
    c = im.crop((cx1, cy1, cx2, cy2))
    x1, y1, x2, y2 = box
    ImageDraw.Draw(c).rectangle([x1 - cx1, y1 - cy1, x2 - cx1, y2 - cy1], outline=(255, 0, 0), width=max(3, int((x2 - x1) / 40)))
    c.save(fp, quality=92)
    res = client.generate({"system": "你是画面质检员，只看图、只报事实，只输出 JSON。", "text": VLM_Q,
                           "images": [{"path": str(fp), "label": "【待检】", "max_pixels": 250000}]}, wait_s=600)
    if "error" in res:
        return None
    return rubric_loads(res.get("text", "")) or None


def vlm_judge(cid: str, ans: dict, card: dict) -> list[str]:
    nm, bad = card.get("name", cid), []
    if card.get("gender") and ans.get("gender") in ("男", "女") and ans["gender"] != card["gender"]:
        bad.append(f"{nm} 看图判为{ans['gender']}性")
    if card.get("hair_colors") and ans.get("hair_color") not in (None, "看不清") and ans["hair_color"] not in card["hair_colors"]:
        bad.append(f"{nm} 看图发色 {ans['hair_color']}（应为 {'/'.join(card['hair_colors'])}）")
    if card.get("hair_lengths") and ans.get("hair_length") not in (None, "看不清") and ans["hair_length"] not in card["hair_lengths"]:
        bad.append(f"{nm} 看图发长 {ans['hair_length']}（应为 {'/'.join(card['hair_lengths'])}）")
    if card.get("nails_ok") and ans.get("nails") not in (None, "看不见") and ans["nails"] not in card["nails_ok"]:
        bad.append(f"{nm} 看图指甲 {ans['nails']}（应为 {'/'.join(card['nails_ok'])}）")
    return bad
