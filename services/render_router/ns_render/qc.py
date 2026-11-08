"""
首帧/尾帧/资产图的裸露自动质检（本地模型）。2026-10-30 取消成人过滤器后只记录，不再否决。

v3（2026-10-30，tools/qc_calibrate.py，标注集 golden/qc_calibration/）：
- v1 的 CLIP 对子只写女性，穿黑衬衫的男性判 0.85–0.95、正面着装女性被“背面赤裸”对子判 0.87：
  着装图 137 张误报 54 张；同时漏掉了两张真裸露（ep01-S5 裸背 0.35、ep04-S6 上身赤裸 0.25，按 v2 提示词）。
- 本地看图模型（ComfyUI TextGenerate + Qwen3-VL 8B，问“是否有人裸露/赤膊/裸背/露出生殖器或臀部”）：
  94 张核对过的着装图 0 误报，3 张人工确认的裸露图全部命中 → 有 ComfyUI 时由它裁决。
判定：NudeNet 露出部位命中（女性胸部/生殖器/臀部/肛门；男性生殖器阈值提到 0.75——手指特写曾 0.64 误报）
      或 CLIP v2 裸露分 ≥ 0.90（着装图最高 0.71）→ 裸露；
      否则有看图模型时以它为准；没有时 CLIP v2 ≥ CLIP_THRESHOLD_NO_VLM（偏严，宁可多抽）。

结果写进 Take.qc：{"nudity", "hits", "clip_bare", "vlm", "version"}
"""
from functools import lru_cache
from pathlib import Path

QC_VERSION = "nudenet3.4+clipL14v2+qwen3vl-v3"

# NudeNet 类别 -> 判定阈值
NUDENET_THRESHOLDS = {
    "FEMALE_BREAST_EXPOSED": 0.30,
    "FEMALE_GENITALIA_EXPOSED": 0.30,
    "MALE_GENITALIA_EXPOSED": 0.75,   # v3：手指/拇指特写曾 0.39–0.64 误报
    "ANUS_EXPOSED": 0.30,
    "BUTTOCKS_EXPOSED": 0.35,
}

CLIP_MODEL = "openai/clip-vit-large-patch14"
# 每组第一句 = 裸露，第二句 = 着装；取各组裸露概率的最大值
CLIP_PAIRS = (
    ("a photo of a topless woman, her upper body is bare with no clothing",
     "a photo of a woman wearing clothing on her upper body"),
    ("a photo of a woman from behind with a completely bare back and no top",
     "a photo of a woman from behind wearing a top or dress"),
    ("a photo of a naked person", "a photo of a clothed person"),
)
CLIP_THRESHOLD_LEGACY = 0.80

# v2（2026-10-30）：v1 的提示词只写女性、按“对子”比较，穿黑衬衫的男性 0.85–0.95、正面着装女性被“背面赤裸”对子判 0.87。
# 改为一次 softmax：裸露类（男女通用 + 男性赤膊 + 背面赤裸）对比多种着装描述（含男装、低胸/抹胸、背面着装、露手臂腿脚、
# 脸部特写、产品图），裸露分 = 裸露类概率之和。阈值在 golden/qc_calibration 标注集上校准（tools/qc_calibrate.py）。
NUDE_PROMPTS = (
    "a photo of a topless woman with bare breasts",
    "a photo of a shirtless man with a bare chest",
    "a photo of a naked person",
    "a photo of a person seen from behind with a completely bare back and no top",
)
CLOTHED_PROMPTS = (
    "a photo of a woman wearing a dress or a top",
    "a photo of a woman wearing a low-cut top or a strapless dress",
    "a photo of a woman in a tight mini skirt and blouse",
    "a photo of a man wearing a shirt and trousers",
    "a photo of a man in a dark suit",
    "a photo of a person seen from behind wearing clothes",
    "a photo of a person wearing clothes with bare arms, legs or feet",
    "a close-up portrait photo of a face",
    "a product photo of clothing or shoes on a plain background",
    "a close-up photo of hands with long manicured nails",
)
CLIP_THRESHOLD = 0.90            # v3：这么高无论看图模型怎么说都算裸露（着装图最高 0.71）
CLIP_THRESHOLD_NO_VLM = 0.30     # 没有看图模型时的兜底（偏严：真裸露 0.25–0.52，着装图 95 分位 0.60 → 会多抽）
VLM_QUESTION = ("Look at this image carefully. Is any person in it nude or topless — bare female breasts, a bare "
                "male or female chest with no clothing on the upper body, a completely bare back with no top, or "
                "exposed genitals or buttocks? Bare arms, legs, feet, shoulders, cleavage in a top, or a strapless "
                "dress do NOT count. Answer only yes or no.")


def vlm_judge(client):
    """返回 path -> True/False/None 的看图模型裁决函数（ComfyUI TextGenerate + Qwen3-VL 8B）。None = 没拿到答案"""
    from . import workflows as W

    def judge_path(path: Path):
        try:
            name = client.upload_image(Path(path), subfolder="ns_final/qc")
            entry, _ = client.run(W.text_generate(VLM_QUESTION, "ns_final/vlm", image=name, max_length=4),
                                  timeout_s=300)
        except Exception:  # noqa: BLE001 — 拿不到就退回 CLIP 兜底
            return None
        t = "".join(entry.get("outputs", {}).get("90", {}).get("text", []) or []).strip().lower()
        return True if t.startswith("yes") else False if t.startswith("no") else None
    return judge_path


@lru_cache(maxsize=1)
def _nudenet():
    from nudenet import NudeDetector
    return NudeDetector()


@lru_cache(maxsize=1)
def _clip():
    from transformers import CLIPModel, CLIPProcessor
    return CLIPModel.from_pretrained(CLIP_MODEL).eval(), CLIPProcessor.from_pretrained(CLIP_MODEL)


def nudenet_hits(path: Path) -> list[dict]:
    hits = []
    for d in _nudenet().detect(str(path)):
        thr = NUDENET_THRESHOLDS.get(d["class"])
        if thr is not None and d["score"] >= thr:
            hits.append({"class": d["class"], "score": round(float(d["score"]), 3)})
    return hits


def _clip_probs(path: Path, texts: list[str]):
    import torch
    from PIL import Image
    model, proc = _clip()
    with torch.no_grad():
        inp = proc(text=texts, images=Image.open(path).convert("RGB"), return_tensors="pt", padding=True)
        return model(**inp).logits_per_image[0]


def clip_bare_score_legacy(path: Path) -> float:
    """v1：成对比较取最大（只用于校准对照）"""
    texts = [t for pair in CLIP_PAIRS for t in pair]
    probs = _clip_probs(path, texts).reshape(len(CLIP_PAIRS), 2).softmax(-1)[:, 0]
    return round(float(probs.max()), 3)


def clip_bare_score(path: Path) -> float:
    """v2：裸露类在全部描述上的 softmax 概率之和"""
    probs = _clip_probs(path, [*NUDE_PROMPTS, *CLOTHED_PROMPTS]).softmax(-1)
    return round(float(probs[:len(NUDE_PROMPTS)].sum()), 3)


def judge(hits: list[dict], clip_bare: float) -> bool:
    return bool(hits) or clip_bare >= CLIP_THRESHOLD


def judge_v3(hits: list[dict], clip_bare: float, vlm: bool | None) -> bool:
    if hits or clip_bare >= CLIP_THRESHOLD:
        return True
    if vlm is not None:
        return vlm
    return clip_bare >= CLIP_THRESHOLD_NO_VLM


def nudity_qc(path: Path, vlm=None) -> dict:
    """vlm：vlm_judge(client) 返回的函数；不给就只用 NudeNet + CLIP（偏严）"""
    hits = nudenet_hits(path)
    bare = clip_bare_score(path)
    v = vlm(path) if vlm and not hits and bare < CLIP_THRESHOLD else None
    return {"nudity": judge_v3(hits, bare, v), "hits": hits, "clip_bare": bare, "vlm": v, "version": QC_VERSION}
