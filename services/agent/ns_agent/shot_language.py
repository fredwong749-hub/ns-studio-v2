"""镜头语言模板 v2 → 首帧固定顺序句（ISS-1103-023528-2869，MERGE-IMG-O 11-03，L-137 / L-107 / L-110 / L-111）。

唯一编译入口 frame_job.build_first → compile_frame 调这里，得到首帧要补的“镜头语言”句，按固定顺序放在正文 + 【场景】段之后、
造型 / 视线 / 站位补句与风格后缀之前（with_note 同一位置）。顺序与句式单一来源 = config/director/shot_spec_v2.yaml image_compile：
  ② lens        镜头参数（剧本【镜头】→ lenses.yaml；auto = 按景别 lenses.yaml by_size，tools/director/shot_v2.pick_lens 同一份）
  ③ composition 构图（剧本【构图】原文：前景 / 中景 / 背景 / 视线方向）
  ④ lighting    灯光（剧本【灯光】原文：动机光源 / 布光 / 色温 / 人物适配；auto = lighting.yaml 方案，shot_v2.image_clauses_zh 同一份）
  ⑤ fusion      L-110 融合契约句（shot_spec_v2.yaml image_compile.fusion_zh）
（① 机位与景别 = 编译器景别首句，已在正文最前；⑤–⑦ 运镜 / 表演 / 声音只进视频）

每段按剧开关（config/compile/run_style.json runs.<剧>.shot_language）：
  {"lens": "off|script|auto", "composition": "off|script", "lighting": "off|script|auto", "fusion": false|true}
没有条目的剧（V1 / 第二季）= 全关：旧剧提示词逐字不变（L-087）。资产包模式的剧，镜头句里的品牌词（徕卡 / Leica，run_style v1_style_phrases）
一律去掉、只留焦段光圈景深（I7 会挡 V2 提示词里的 V1 风格短语）。剧本字段取自导入稿 shot.extras（label = 构图 / 灯光 / 镜头）。
"""
from __future__ import annotations

import json
import re
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SPEC = ROOT / "config" / "director" / "shot_spec_v2.yaml"
RUN_STYLE = ROOT / "config" / "compile" / "run_style.json"
DEFAULT_ORDER = ("lens", "composition", "lighting", "fusion")
LABELS = {"lens": ("镜头",), "composition": ("构图",), "lighting": ("灯光",)}


@lru_cache(maxsize=1)
def _spec() -> dict:
    import yaml
    try:
        return yaml.safe_load(SPEC.read_text(encoding="utf-8")) or {}
    except OSError as e:
        print(f"[shot_language] {SPEC} 读不了：{e} → 镜头语言模板按空（不补句）", file=sys.stderr)
        return {}


def image_compile_cfg() -> dict:
    return _spec().get("image_compile") or {}


def run_cfg(project: str | None) -> dict:
    try:
        d = json.loads(RUN_STYLE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"[shot_language] {RUN_STYLE} 读不出：{e} → 本剧镜头语言全关", file=sys.stderr)
        return {}
    return (((d.get("runs") or {}).get(project or "") or {}).get("shot_language")) or {}


def _brand_words() -> list[str]:
    try:
        return list(json.loads(RUN_STYLE.read_text(encoding="utf-8")).get("v1_style_phrases") or [])
    except (OSError, ValueError) as e:
        print(f"[shot_language] {RUN_STYLE} 读不出：{e} → 品牌词表按空", file=sys.stderr)
        return []


def _g(o, k, d=None):
    return o.get(k, d) if isinstance(o, dict) else getattr(o, k, d)


def script_field(shot, key: str) -> str:
    """导入稿 shot.extras 里 label = 构图 / 灯光 / 镜头 的原文（去掉 ** 与冒号）"""
    for e in _g(shot, "extras") or []:
        # A2 第 18 轮 F4：带括注的 label（“灯光（动机）”）按去括注的 base 比，和导入器 _split_label / first_beat_gaze 同口径
        lab = re.sub(r"（[^）]*）|\([^)]*\)", "", str(_g(e, "label") or "")).strip("*：: ")
        if lab in LABELS.get(key, ()):
            return str(_g(e, "text") or "").strip()
    return ""


def _fields(text: str) -> str:
    """“前景=A｜中景=B｜背景=C” → “前景A，中景B，背景C”（只换分隔符，不改写作方原文）"""
    parts = [p.strip() for p in re.split(r"[｜|]", text or "") if p.strip()]
    return "，".join(re.sub(r"\s*[=＝]\s*", "：", p, count=1) for p in parts)


@lru_cache(maxsize=1)
def _shot_v2():
    import importlib.util as U
    sp = U.spec_from_file_location("ns_shot_v2", ROOT / "tools" / "director" / "shot_v2.py")
    m = U.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


def _shot_dict(shot) -> dict:
    if isinstance(shot, dict):
        return shot
    d = shot.model_dump() if hasattr(shot, "model_dump") else dict(vars(shot))
    d["cast"] = [c if isinstance(c, dict) else (c.model_dump() if hasattr(c, "model_dump") else vars(c)) for c in d.get("cast") or []]
    return d


def strip_brand(text: str) -> str:
    """V2：镜头句去品牌（徕卡 / Leica / Summilux-M …），只留焦段、光圈、景深"""
    t = re.sub(r"(?:徕卡|Leica|leica)\s*(?:M\s*)?(?:APO-)?(?:Summilux|Summicron|Noctilux|Elmarit)?-?M?\s*", "", text or "")
    for w in _brand_words():
        if w and w in t:
            t = t.replace(w, "")
    return re.sub(r"\s{2,}", " ", t).strip(" ，")


def lens_clause(shot, mode: str, manifest_mode: bool) -> str:
    if mode not in ("script", "auto"):
        return ""
    sv = _shot_v2()
    over = script_field(shot, "lens") or None
    if mode == "script" and not over:
        return ""
    L = sv.pick_lens(_shot_dict(shot), over)
    zh = f"{L['zh']}，光圈 {L['aperture']}，{L['dof_zh']}"
    if manifest_mode:      # V2：不写镜头品牌 / 型号，只写焦段光圈景深（I7 v1_style_phrases：徕卡 / Leica / 50mm f/2 …）
        zh = strip_brand(f"{L.get('focal_mm')}mm 镜头，光圈 {L['aperture']}，{L['dof_zh']}" if L.get("focal_mm") else zh)
    return f"镜头：{zh}" if zh else ""


def composition_clause(shot, mode: str) -> str:
    if mode != "script":
        return ""
    t = script_field(shot, "composition")
    return f"构图：{_fields(t)}" if t else ""


def lighting_clause(shot, mode: str) -> str:
    if mode == "script":
        t = script_field(shot, "lighting")
        return f"灯光：{_fields(t)}" if t else ""
    if mode == "auto":
        t = script_field(shot, "lighting")
        if t:
            return f"灯光：{_fields(t)}"
        return "灯光：" + _shot_v2().image_clauses_zh(_shot_dict(shot))["lighting_zh"].rstrip("。")
    return ""


def fusion_clause(on) -> str:
    return (image_compile_cfg().get("fusion_zh") or "").rstrip("。") if on else ""


def image_clauses(project: str | None, shot) -> list[tuple[str, str]]:
    """→ [(段 id, 句子)]，按 shot_spec_v2.yaml image_compile.order；全关 / 没写 → []"""
    rc = run_cfg(project)
    if not rc:
        return []
    from ns_agent import asset_refs as AR
    mm = bool(project and AR.manifest_rel(project))
    make = {"lens": lambda: lens_clause(shot, rc.get("lens", "off"), mm),
            "composition": lambda: composition_clause(shot, rc.get("composition", "off")),
            "lighting": lambda: lighting_clause(shot, rc.get("lighting", "off")),
            "fusion": lambda: fusion_clause(rc.get("fusion"))}
    out = []
    for k in image_compile_cfg().get("order") or DEFAULT_ORDER:
        s = make[k]() if k in make else ""
        if s:
            out.append((k, s))
    return out


def image_note(project: str | None, shot) -> str:
    """frame_job 用：固定顺序拼好的一段（每句以“。”结尾，同 with_note 的补句口径）"""
    return "".join(s + "。" for _k, s in image_clauses(project, shot))
