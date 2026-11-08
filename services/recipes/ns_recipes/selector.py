"""选配方：Shot Spec 字段 → 配方 id + overlay + 回退链（纯函数）。

规则（与 recipes/*.yaml 的 match / priority 对应）：
1. 写作方在 **镜型** 行显式指定（recipe_hint = 配方 id 或中文名）且该配方可选 → 用它。
2. 否则按 priority 从小到大，取第一个 match 全部成立的可选配方（abstract / selectable:false / 待许可登记 不参与）。
3. overlay：夜景（场景时段或光线基线含“夜”）、泛红风险（提示词有脸红类描述词——渲染参数触发，不是内容检测）。
4. 回退链 = 配方 fallback，去掉被 overlay 禁用的（flush_risk 禁 Pika）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ns_schema.shot_spec import ShotSpec

from .registry import Registry

LOCOMOTION_RE = re.compile(
    r"(走[向进出到过开回近来去]|走路|迈步|迈开|快步|踱步|奔跑|跑[向进出到过开]|小跑|冲[向进出]|离开|转身走|穿过|跨过|起身走|"
    r"\bwalk(?:s|ing)?\b|\brun(?:s|ning)?\b|\bstrides?\b|\bstriding\b|\bjog(?:s|ging)?\b|\bsteps? (?:forward|toward|into|out)\b|"
    r"\bcrosses\b|\bheads? (?:toward|to|into)\b|\bleaves?\b)", re.I)


@dataclass
class Features:
    any_visible: bool
    faces: int
    speaking_on_screen: bool
    size: str | None
    locomotion: bool
    frames: int
    voiceover_dominant: bool
    night: bool
    flush_words: bool
    exposure: str

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Selection:
    recipe_id: str
    overlays: list[str]
    reasons: list[str]
    fallback: list[str]
    features: dict = field(default_factory=dict)


def _flush_hit(spec: ShotSpec, words) -> bool:
    text = " ".join(x or "" for x in (spec.h3.text, spec.first_frame.text, spec.motion.text, spec.end_frame.text))
    low = text.lower()
    return any((w.lower() in low) for w in words)


def features(spec: ShotSpec, registry: Registry | None = None) -> Features:
    words = []
    if registry is not None and "flush_risk" in registry.overlays:
        words = registry.overlays["flush_risk"].get("words") or []
    beats_txt = " ".join(b.action for b in spec.motion.beats)
    loco_src = " ".join([spec.motion.text or "", beats_txt])
    vo = spec.audio.voiceover
    vo_len = (vo.t1 - (vo.t0 or 0)) if (vo and vo.t1 is not None) else 0.0
    return Features(
        any_visible=bool(spec.visible),
        faces=len(spec.faces),
        speaking_on_screen=bool(spec.on_screen_speakers),
        size=spec.camera.size,
        locomotion=bool(LOCOMOTION_RE.search(loco_src)),
        frames=spec.frames,
        voiceover_dominant=bool(vo) and not spec.audio.dialogue and vo_len >= 0.5 * spec.duration_s,
        night=spec.scene.is_night,
        flush_words=_flush_hit(spec, words) if words else False,
        exposure=spec.end_frame.exposure_intent,
    )


def _cond(key: str, want, f: Features) -> bool:
    if key == "any_visible":
        return f.any_visible == bool(want)
    if key == "min_faces":
        return f.faces >= int(want)
    if key == "speaking_on_screen":
        return f.speaking_on_screen == bool(want)
    if key == "size_in":
        return f.size in want
    if key == "locomotion":
        return f.locomotion == bool(want)
    if key == "frames_gt":
        return f.frames > int(want)
    if key == "voiceover_dominant":
        return f.voiceover_dominant == bool(want)
    if key == "night":
        return f.night == bool(want)
    if key == "flush_words":
        return f.flush_words == bool(want)
    if key == "any_of":
        return any(matches(m, f) for m in want)
    raise KeyError(f"未知的 match 条件 {key}")


def matches(match: dict | None, f: Features) -> bool:
    return all(_cond(k, v, f) for k, v in (match or {}).items())


def selectable(r: dict) -> bool:
    return not r.get("abstract") and r.get("selectable", True) and r.get("status") != "待许可登记"


def select(spec: ShotSpec, registry: Registry, allow: set[str] | None = None) -> Selection:
    f = features(spec, registry)
    reasons: list[str] = []
    chosen = None
    hint = (spec.recipe_hint or "").strip()
    if hint:
        for rid, r in registry.raw.items():
            if hint in (rid, r.get("label")) and (selectable(r) or (allow and rid in allow)):
                chosen = rid
                reasons.append(f"镜型行指定 {hint}")
                break
        else:
            reasons.append(f"镜型行 {hint!r} 不是可选配方，按规则选")
    if chosen is None:
        cands = sorted((r.get("priority", 999), rid) for rid, r in registry.raw.items()
                       if selectable(r) or (allow and rid in allow))
        for _p, rid in cands:
            if matches(registry.raw[rid].get("match"), f):
                chosen = rid
                reasons.append(f"match {registry.raw[rid].get('match')}")
                break
    if chosen is None:
        chosen = "static_emotion" if f.any_visible else "establishing"
        reasons.append("没有配方匹配，按有无人物兜底")
    overlays = [oid for oid, ov in sorted(registry.overlays.items()) if matches(ov.get("match"), f)]
    blocked = {b for oid in overlays for b in (registry.overlays[oid].get("block_recipes") or [])}
    resolved = registry._resolve_raw(chosen)
    fb = [x for x in resolved.get("fallback") or [] if x not in blocked]
    # 回退链递归展开（每级的 fallback 也接上），去重
    seen, chain = {chosen}, []
    queue = list(fb)
    while queue:
        x = queue.pop(0)
        if x in seen or x in blocked:
            continue
        seen.add(x)
        chain.append(x)
        queue += registry._resolve_raw(x).get("fallback") or []
    if blocked:
        reasons.append(f"overlay 禁用回退 {sorted(blocked)}")
    return Selection(recipe_id=chosen, overlays=overlays, reasons=reasons, fallback=chain, features=f.as_dict())
