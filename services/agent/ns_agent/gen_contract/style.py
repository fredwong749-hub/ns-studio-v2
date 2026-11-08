"""I7：系统后缀 = 当前默认出图预设（config/render_quality.json default_preset，项目方 2026-10-31 定“徕卡 S★”）。"""
from __future__ import annotations

import json
import re

from . import ROOT, cfg

NO_TEXT = "无字幕、无文字、无水印"
# 空镜 / 单品设定候选的系统后缀句尾（AUTOFIX-O 11-03，ISS-1103-004550-8787）：人像风格后缀（直闪打亮人物 / 皮肤质感）不许进空镜；
# 时间 / 光线由 tools/pipeline/asset_jobs.scene_suffix 从 P1 / P4 场景表拼在前面；preflight I7 在 mode=setting_candidate + asset_stage∈{scene,item} 时认这一句
EMPTY_SUFFIX = "写实空镜摄影，画面中没有任何人物，真实材质与自然光影，轻微颗粒"
SKIN =("皮肤", "肌肤", "脸", "面部", "skin", "face")


def preset() -> dict:
    try:
        rq = json.loads((ROOT / "config" / "render_quality.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    pid = cfg().get("suffix_preset") or rq.get("default_preset")
    return next((p for p in rq.get("image_presets") or [] if p.get("id") == pid), {})


class StyleMissing(RuntimeError):
    """资产包模式（V2）的剧没有登记自己的风格后缀——不退回 V1 的 S★（L-126）"""


def run_style_cfg() -> dict:
    p = ROOT / "config" / "compile" / "run_style.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def _run() -> str | None:
    from ns_agent import run_paths as RP
    return RP.current_run(ROOT)


def _manifest_mode(run: str | None) -> bool:
    from ns_agent import asset_refs as AR
    return bool(run and AR.project_cfg(run) and AR.manifest_rel(run))


SEGMENT_MARKS = {"fantasy": ("【幻想段】", "【幻想】", "幻想段"), "reality": ("【现实段】", "【现实】", "现实段")}


def segment_of(shot) -> tuple[str | None, str | None]:
    """L-130：镜头属于幻想段还是现实段（剧本节拍 / 字段里的段落标记：【幻想段】/【现实段】，或 extras 段落= / segment=）。
    → (fantasy | reality | None, 来源)；None = 没标记（调用方按剧的 default_segment 取并告警）"""
    g = (lambda o, k: o.get(k) if isinstance(o, dict) else getattr(o, k, None))
    ex = g(shot, "extras") or {}
    for k in ("段落", "segment"):
        v = str((ex.get(k) if isinstance(ex, dict) else "") or g(shot, k) or "")
        if "幻想" in v or v == "fantasy":
            return "fantasy", f"字段 {k}"
        if "现实" in v or v == "reality":
            return "reality", f"字段 {k}"
    text = "；".join(str(g(shot, k) or "") for k in ("beats", "img_prompt", "motion_prompt", "scene_detail", "h3_check"))
    if isinstance(ex, dict):
        text += "；" + "；".join(str(v) for v in ex.values())
    hits = {seg: min([text.find(m) for m in marks if m in text] or [10 ** 9]) for seg, marks in SEGMENT_MARKS.items()}
    seg = min(hits, key=hits.get)
    return (seg, "剧本段落标记") if hits[seg] < 10 ** 9 else (None, None)


_MARK_RE = re.compile(r"【(?:幻想|现实)段?】")


def strip_segment_marks(text: str | None) -> str | None:
    """段落标记只给编译器判段，不进出图提示词"""
    return _MARK_RE.sub("", text) if text else text


def _preset_suffix_by_id(pid: str) -> str:
    rq = json.loads((ROOT / "config" / "render_quality.json").read_text(encoding="utf-8"))
    p = next((x for x in rq.get("image_presets") or [] if x.get("id") == pid), None)
    if not p or not (p.get("params") or {}).get("style_suffix"):
        raise StyleMissing(f"render_quality.json 没有预设 {pid} 或它没有 style_suffix")
    return p["params"]["style_suffix"].strip()


def segment_preset(run: str | None, segment: str | None) -> tuple[str | None, str | None, bool]:
    """→ (预设 id, 实际段落, 是否按缺省段落取)；本剧没有分段风格 → (None, None, False)"""
    ent = ((run_style_cfg().get("runs") or {}).get(run or "") or {})
    segs = ent.get("segments") or {}
    if not segs:
        return None, None, False
    seg = segment if segment in segs else (ent.get("default_segment") or "reality")
    return segs[seg]["preset"], seg, segment not in segs


def run_suffixes(run: str | None = None) -> list[str]:
    """本剧所有可能的风格后缀（分段剧 = 各段预设的 style_suffix）——with_note / preflight I7 认哪一句都算"""
    r = run or _run()
    ent = ((run_style_cfg().get("runs") or {}).get(r or "") or {})
    if ent.get("segments"):
        return [_preset_suffix_by_id(v["preset"]) for v in ent["segments"].values()]
    try:
        return [preset_suffix(r)]
    except StyleMissing:
        return []


def preset_suffix(run: str | None = None, segment: str | None = None) -> str:
    """按剧取风格后缀（config/compile/run_style.json）：分段剧（V2，L-130）按幻想 / 现实段取 render_quality 对应预设的 style_suffix；
    资产包模式的剧没登记 → StyleMissing；旧剧 → render_quality 默认预设"""
    r = run or _run()
    ent = ((run_style_cfg().get("runs") or {}).get(r or "") or {})
    pid, _seg, _dflt = segment_preset(r, segment)
    if pid:
        return _preset_suffix_by_id(pid)
    if ent.get("suffix"):
        return ent["suffix"].strip()
    if _manifest_mode(r):
        raise StyleMissing(f"{r} 是资产包模式（V2），config/compile/run_style.json 里没有它的风格后缀；不退回 V1 的 S★（L-126）")
    return ((preset().get("params") or {}).get("style_suffix") or "").strip()


def v1_style_hits(prompt: str, run: str | None = None) -> list[str]:
    """V2（资产包模式）提示词里的 V1 风格短语（run_style.json v1_style_phrases）；旧剧不判"""
    r = run or _run()
    if not _manifest_mode(r):
        return []
    return [w for w in run_style_cfg().get("v1_style_phrases") or [] if w and w in (prompt or "")]


def image_suffix(run: str | None = None, segment: str | None = None) -> str:
    """编译器 image_style 参数用：本剧（本段）风格后缀 + 无字幕句（Q6）"""
    s = preset_suffix(run, segment)
    return f"{s}，{NO_TEXT}" if s else NO_TEXT


def banned_hits(prompt: str) -> list[str]:
    """提示词里的禁用后缀词。“油光 / 油亮 / oily / glossy skin”只在落到皮肤上时算（漆皮的光泽不算）"""
    hits = []
    low = (prompt or "").lower()
    for w in cfg().get("suffix_banned") or []:
        wl = w.lower()
        if wl not in low:
            continue
        if w in ("油光", "油亮", "oily"):
            ok = False
            for m in re.finditer(re.escape(wl), low):
                win = low[max(0, m.start() - 8): m.end() + 8]
                ok = ok or any(s in win for s in SKIN)
            if not ok:
                continue
        hits.append(w)
    return hits
