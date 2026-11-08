"""视频提示词模板 v2（官方写法）的配方引擎接线（阶段 2.1，CODER-PT 11-05；纯文本规则在 ns_schema.prompt_v2，与 ns_agent.compiler 同一份）。

生产路径 tools/series10_video/build_jobs → ns_recipes.prompt.build_base：开关 run_style.json runs.<剧>.prompt_template.video = v2 时，
H3 正文换成 ns_schema.prompt_v2.video_body()（短锚点句 + 运镜独立成句 + 写作方分拍），运镜 enabled 仍按 camera_norm.is_enabled（L-129 不动）。
ns_recipes 不依赖 ns_agent 包：camera_norm 按文件路径载入（prompt._camera_norm 同一处）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ns_schema import prompt_v2 as PV

ROOT = Path(__file__).resolve().parents[3]
RUN_STYLE = ROOT / "config" / "compile" / "run_style.json"
TEMPLATE_ID = "v2"
_FORCE: dict = {}


def _run_style() -> dict:
    try:
        return json.loads(RUN_STYLE.read_text(encoding="utf-8"))
    except (OSError, ValueError):   # arch-ok: 读不到 = 模板按 v1（现行），不是失败开放
        return {}


def options() -> dict:
    return dict(_run_style().get("prompt_template_v2") or {})


def on(run: str | None) -> bool:
    if "video" in _FORCE:
        return str(_FORCE["video"]) == TEMPLATE_ID
    import os
    env = os.environ.get("NS_PROMPT_TEMPLATE")        # 审计 / 对照用（与 ns_agent.prompt_template_v2 同一变量）；生产进程不设
    if env in ("v1", "v2"):
        return env == TEMPLATE_ID
    ent = ((_run_style().get("runs") or {}).get(run or "") or {}).get("prompt_template")
    if isinstance(ent, str):
        return ent == TEMPLATE_ID
    return bool(isinstance(ent, dict) and str(ent.get("video") or "v1") == TEMPLATE_ID)


def segment_of(spec) -> str:
    """幻想 / 现实段。REVIEW-B 11-05：与 ns_agent 同一个判法（gen_contract.style.segment_of：字段 段落= / 剧本段落标记【幻想段】…），
    两条视频路出同一句布光；原先这里按“幻想”二字出现与否判，会与 ns_agent 不一致。ns_agent 不可用时退回旧判法"""
    ex = dict(getattr(spec, "extras", None) or {})
    try:
        from ns_agent.gen_contract import style as ST
    except Exception:  # arch-ok: ns_recipes 可独立运行（不依赖 ns_agent 包）→ 退回旧判法
        txt = "；".join([str(getattr(getattr(spec, "scene", None), "detail", "") or "")] + [str(v) for v in ex.values()])
        return "fantasy" if "幻想" in txt else "reality"
    shot = {"extras": ex, "scene_detail": getattr(getattr(spec, "scene", None), "detail", "") or "",
            "img_prompt": getattr(getattr(spec, "first_frame", None), "text", "") or "",
            "motion_prompt": getattr(getattr(spec, "motion", None), "text", "") or "",
            "h3_check": getattr(getattr(spec, "h3", None), "text", "") or ""}
    return ST.segment_of(shot)[0] or "reality"


def video_dict(spec, run: str | None) -> dict:
    from .prompt import _camera_norm
    CN = _camera_norm()
    opt = options()
    ex = getattr(spec, "extras", None) or {}
    chars = []
    for c in spec.characters_on_screen:
        chars.append({"name": c.name, "en": c.en_phrase or "", "look_en": c.look or "", "x": c.screen_x, "depth": c.depth,
                      "presence": c.presence, "part": c.part, "faced": c.face_visible is not False})
    names = [c["name"] for c in chars]
    angle_field = ex.get("机位与景别", "")
    af = PV.split_field(angle_field)
    if len(af) >= 3 and ("过肩" in af[1] or "越" in af[2]):      # 过肩：第三段写“从 X 越肩拍 Y”（与 ns_agent 适配同口径）
        af = [af[0], af[1] + "｜" + af[2]] + af[3:]
    cam_field = ex.get("运镜", "")
    mv = PV.split_field(cam_field)
    move_id = CN.match_zh(mv[0]) if mv else None
    amp, speed = PV.amp_speed_of(cam_field)        # 只读镜级 **运镜** 字段（与 ns_agent 适配同源；spec.camera 的 镜头运动 幅度 / 速度不取）
    motion = getattr(getattr(spec, "motion", None), "text", "") or ""
    why = None
    if not CN.is_enabled(move_id, run, motion):
        move_id, why = CN.fallback(move_id, bool(CN.BIG_ACTION.search(motion)))
    move_id, why2 = CN.multi_person_fallback(move_id, len(spec.visible), run)   # L-192：双人及以上只允许 static / push_in / pull_out / handheld_follow
    why = why2 or why
    move = CN.move(move_id) or CN.move("static")
    sharp, _soft = PV.focus_names(angle_field, ex.get("构图", ""), names)
    seg = segment_of(spec)
    body = ""
    if spec.h3 and spec.h3.text:
        m = re.search(r"integrated_multimodal_description:\s*(.*?)(?=\n\s*\n[A-Za-z_]+:|\Z)", spec.h3.text, re.S)
        body = (m.group(1) if m else "").strip()
    return {"size": getattr(spec.camera, "size", None), "angle_field": af[1] if len(af) >= 2 else angle_field, "comp_field": ex.get("构图", ""),
            "chars": chars, "h3_body": body,
            # AUDIT-RULES 11-05：L1 布光句按本镜 **灯光** 字段（动机光源 + 方向）生成，取不到才用配置通用句（不写死环形灯 + 正面）
            "lighting_en": PV.lighting_en_from(PV.kv_field(ex.get("灯光", "")), seg, ((opt.get("lighting_en") or {}).get("generic") or {}).get(seg, "")),
            "constraint_en": opt.get("constraint_en", ""), "move": move, "move_id": move_id, "amp": amp, "speed": speed,
            "direction": opt.get("video_direction_default", "right"), "focus": sharp[0] if sharp else None, "fallback_why": why, "segment": seg,
            "templates": [m.get("template") for m in (CN.vocab().get("moves") or []) if m.get("template")]}


def video_body_for_spec(spec, run: str | None) -> tuple[str, list[str]]:
    """→ (FL2VA 形态正文, notes)"""
    d = video_dict(spec, run)
    body, rec = PV.video_body(d)
    notes = [f"prompt_template v2（官方写法）：运镜 {rec.get('move_id')} → “{rec.get('camera')}”；去掉分拍里嵌的运镜分句 {len(rec.get('removed') or [])} 处"]
    if d.get("fallback_why"):
        notes.append(f"K-REN-801 运镜退回：{d['fallback_why']}")
    return body, notes
