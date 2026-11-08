"""提示词模板 v2 的编译接线（阶段 2.1，CODER-PT 11-05；纯文本规则在 ns_schema.prompt_v2）。

按剧开关：config/compile/run_style.json runs.<剧>.prompt_template = {"image": "v1|v2", "video": "v1|v2"}；没写 / v1 = 现行编译逐字不变。
共用选项：run_style.json 顶层 prompt_template_v2（否定改写表、负面词追加、L1 英文布光句、约束句）。
接入点：
  出图  frame_job.build_first（K-REN-134 唯一出图路径）：开关 v2 → 正文换成 image_body()，参考槽标注（plan.prompt_prefix）不变，
        作业加 prompt_template="v2"（producer.job_sig / shot_inputs_sig / redo_inputs_changed 据此判“模板变了”→ 重出）。
  视频  compiler.compile_shot（画布 / 单镜路径）与 ns_recipes.prompt.build_base（生产 vidgen 路径）：开关 v2 → H3 正文换成
        ns_schema.prompt_v2.video_body()，运镜句由词表 h3_official 生成、enabled 仍按 camera_norm.is_enabled（L-129 不动）。
测试 / 对照页可用 force(...) 临时覆盖开关（不写配置）。
"""
from __future__ import annotations

import json
import re
import sys
from contextlib import contextmanager
from pathlib import Path

from ns_schema import prompt_v2 as PV

ROOT = Path(__file__).resolve().parents[3]
RUN_STYLE = ROOT / "config" / "compile" / "run_style.json"
_FORCE: dict = {}          # {"image": "v2", "video": "v2"}：force() 覆盖（测试 / 对照页）
TEMPLATE_ID = "v2"


def _run_style() -> dict:
    try:
        return json.loads(RUN_STYLE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"[prompt_template_v2] {RUN_STYLE} 读不出：{e} → 模板按 v1", file=sys.stderr)
        return {}


def options() -> dict:
    return dict(_run_style().get("prompt_template_v2") or {})


def template_of(project: str | None, kind: str) -> str:
    """kind = image | video → "v1" | "v2"（force 覆盖 > 环境变量 NS_PROMPT_TEMPLATE（审计 / 对照用，生产进程不设）> 配置 > v1）"""
    if kind in _FORCE:
        return str(_FORCE[kind])
    import os
    env = os.environ.get("NS_PROMPT_TEMPLATE")
    if env in ("v1", "v2"):
        return env
    ent = ((_run_style().get("runs") or {}).get(project or "") or {}).get("prompt_template")
    if isinstance(ent, str):
        return ent if ent in ("v1", "v2") else "v1"
    if isinstance(ent, dict):
        return str(ent.get(kind) or "v1")
    return "v1"


def image_on(project: str | None) -> bool:
    return template_of(project, "image") == TEMPLATE_ID


def video_on(project: str | None) -> bool:
    return template_of(project, "video") == TEMPLATE_ID


@contextmanager
def force(image: str | None = None, video: str | None = None):
    """临时覆盖开关（不写配置）：with force(image="v2"): job = build_first(...)"""
    old = dict(_FORCE)
    try:
        if image:
            _FORCE["image"] = image
        if video:
            _FORCE["video"] = video
        yield
    finally:
        _FORCE.clear()
        _FORCE.update(old)


# ---------------------------------------------------------------- 读剧本字段
def _g(o, k, d=None):
    return o.get(k, d) if isinstance(o, dict) else getattr(o, k, d)


def script_field(shot, label: str) -> str:
    """导入稿 shot.extras 里 label 的原文（带括注的 label 按去括注的 base 比）"""
    for e in _g(shot, "extras") or []:
        lab = re.sub(r"（[^）]*）|\([^)]*\)", "", str(_g(e, "label") or "")).strip("*：: ")
        if lab == label:
            return str(_g(e, "text") or "").strip()
    return ""


def looks_of(ep, shot) -> dict[str, dict]:
    """本集 **造型｜X** 行（镜 N 起换装按镜号取）→ {人: {服装, 发, 妆, …, EN}}"""
    out: dict[str, dict] = {}
    idx = int(_g(shot, "index") or 0)
    for lk in _g(ep, "looks") or []:
        fs = _g(lk, "from_shot")
        if fs is not None and int(fs) > idx:
            continue
        name = _g(lk, "character")
        cur = dict(out.get(name) or {})
        cur.update({k: v for k, v in (_g(lk, "slots") or {}).items() if v})
        if _g(lk, "en"):
            cur["EN"] = _g(lk, "en")
        out[name] = cur
    return out


def registry_en(g) -> dict[str, str]:
    return {_g(c, "name"): (_g(c, "en") or "") for c in (_g(g, "character_registry") or []) if _g(c, "name")}


def cast_rows(shot) -> list[dict]:
    return [{"name": _g(c, "name"), "presence": _g(c, "presence") or "", "x": _g(c, "x"), "depth": _g(c, "depth"), "part": _g(c, "part")}
            for c in (_g(shot, "cast") or []) if _g(c, "presence") in ("在画", "局部")]


def segment_of(shot) -> str:
    from ns_agent.gen_contract import style as ST
    seg, _src = ST.segment_of(shot)
    return seg or "reality"


# ---------------------------------------------------------------- 出图（§8 ①–⑤）
def lens_kind(size: str | None, n_faced: int, ots: bool, subject: str | None) -> str:
    """L-192 焦段表的镜头类型：insert（物件 / 局部特写）/ closeup（特写）/ ots_or_single_mcu（过肩、单人近景 / 中近景）/ two_shot（≥2 露脸或单人中景）/ establishing"""
    from ns_agent import framing as FR
    if subject in ("part", "object"):
        return "insert"
    if size in FR.CLOSE:
        return "closeup"
    if ots or (size in ("近景", "中近景") and n_faced <= 1):
        return "ots_or_single_mcu"
    if size in ("全景", "中全景", "远景", "大远景", "全身"):
        return "establishing"
    return "two_shot"


def lens_of(shot, *, n_faced: int = 1, ots: bool = False, subject: str | None = None, size: str | None = None) -> dict:
    """**镜头** 字段（lenses.yaml id / 中文名）照写 > 没写时按 L-192 焦段表（config/director/lenses.yaml by_kind，按镜头类型；
    与 by_size 同一张表、单一来源）> 再没有就按景别默认（lenses.yaml by_size）。→ {focal, aperture（f/2.8 或 T2.8 原样）, dof_zh, dof_en, macro, source}"""
    from ns_agent import shot_language as SL
    sv = SL._shot_v2()
    import yaml
    LN = yaml.safe_load((ROOT / "config/director/lenses.yaml").read_text(encoding="utf-8")) or {}
    by = {x["id"]: x for x in LN.get("lenses") or []}
    # K-WRT-816（PS-MASTER 11-05）：下游先读分镜 **光学** 行（焦段｜T｜高度=…，复用 ns_render.lens_sim_v2.parse_optics_line）
    optics = script_field(shot, "光学")
    if optics:
        try:
            from ns_render.lens_sim_v2 import parse_optics_line
            o = parse_optics_line(optics)
        except ImportError as e:   # arch-ok: render_router 不在路径（极少）→ 退到 **镜头** / by_kind，并打印
            print(f"[prompt_template_v2] parse_optics_line 不可用：{e}", file=sys.stderr)
            o = {}
        if o.get("focal_mm"):
            focal = int(o["focal_mm"]) if float(o["focal_mm"]).is_integer() else o["focal_mm"]
            ap = f"T{o['t_stop']:g}" if o.get("t_stop") else ""
            e = next((x for x in by.values() if float(x.get("focal_mm") or 0) == float(o["focal_mm"])
                      and (not ap or str(x.get("aperture") or "").upper() == ap.upper())), None)
            t = float(o.get("t_stop") or 2.8)
            dof_zh, dof_en = ((e.get("dof_zh"), e.get("dof_en")) if e else
                              (("很浅的景深，焦外柔和成光斑", "a very shallow depth of field with soft bokeh") if t <= 1.4 else
                               ("浅景深，背景柔化", "a shallow depth of field with the background softened") if t <= 2.0 else
                               ("中等景深，人物和环境一起清楚", "a moderate depth of field holding both figure and setting")))
            return {"focal": str(focal), "aperture": ap or str((e or {}).get("aperture") or ""), "dof_zh": dof_zh or "", "dof_en": dof_en or "",
                    "macro": bool(e and ("macro" in str(e.get("id")) or "微距" in str(e.get("zh") or ""))), "source": "script 光学"}
    over = script_field(shot, "镜头") or None
    if not over:
        k = lens_kind(size, n_faced, ots, subject)
        e = by.get((LN.get("by_kind") or {}).get(k) or "")
        if e:
            return {"focal": str(e.get("focal_mm") or ""), "aperture": str(e.get("aperture") or ""), "dof_zh": e.get("dof_zh") or "",
                    "dof_en": e.get("dof_en") or "", "macro": "macro" in str(e.get("id")) or "微距" in str(e.get("zh") or ""),
                    "source": f"L-192 焦段表 by_kind {k} = {e['id']}"}
    L = sv.pick_lens(SL._shot_dict(shot), over)
    return {"focal": str(L.get("focal_mm") or ""), "aperture": str(L.get("aperture") or ""), "dof_zh": L.get("dof_zh") or "",
            "dof_en": L.get("dof_en") or "", "macro": "macro" in str(L.get("id") or "") or "微距" in str(L.get("zh") or ""),
            "source": "script" if over else "lenses.yaml by_size"}


def hidden_people(shot, angle: dict | None = None) -> dict[str, str]:
    """本镜 cast 在画但不露脸的人 → {名: 露出的部位}：背影（asset_refs.back_view）、站位 / img 里“以…入画 / 只露 / 被遮”（ns_schema.prompt_v2.hidden_face）、
    过肩被越的人（机位与景别 ots_from）。出图同框句（⑤）、作业 cast.face_hidden、人数闸下限（tools/vqa/accept.people_range）共用这一判定"""
    from ns_agent import asset_refs as AR
    from ns_agent import compiler as C
    from ns_agent import framing as FR
    cast = cast_rows(shot)
    names = [r["name"] for r in cast]
    if not names:
        return {}
    img = C._no_segment_marks(C._strip_triggers(_g(shot, "img_prompt") or "", list(_g(shot, "img_triggers") or []))) or ""
    faced_all, _partial = FR.faced_people(shot, names)
    _lead, per, _tail = PV.person_segments(img, names)
    # **站位**：人与人用“；”分，每人内部“名：朝向｜描述”用“｜”分 → 按“；”切才能拿到整段
    stand = {n: p.strip() for p in re.split(r"[；;]", script_field(shot, "站位")) for n in names if p.strip().startswith(n)}
    if angle is None:
        af = PV.split_field(script_field(shot, "机位与景别"))
        seg = af[1] if len(af) >= 2 else ""
        if len(af) >= 3 and ("过肩" in seg or "越" in af[2]):
            seg += "｜" + af[2]
        angle = PV.parse_angle(seg, names)
    out: dict[str, str] = {}
    a = angle or {}
    by_text = a.get("ots_by") == "text"
    ots_from = a.get("ots_from") if by_text else None             # FIX-PRE 11-06：按 cast 顺序猜出来的“被越的人”不作数
    for r in cast:
        n = r["name"]
        if r["presence"] != "在画":
            continue
        if by_text and n == a.get("ots_to") and not AR.back_view(shot, n):
            continue                                            # 过肩被拍的人 = 焦点主体，永远不记成不露脸（REVIEW-PRE B）
        hid, part = PV.hidden_face(stand.get(n, "") + "｜" + "，".join(per.get(n) or []))
        if hid and part not in ("", "肩与背", "背影"):          # 写明了露出的部位（“只以右肩与后脑…入画”）最具体，优先
            out[n] = part
        elif AR.back_view(shot, n):
            out[n] = "背影"
        elif hid or ots_from == n or n not in faced_all:
            out[n] = part if hid and part else "肩与背"
    return out


def lens_text(L: dict) -> tuple[str, str]:
    """→ (中文镜头句, 英文)：“85mm 镜头 T1.4” / “85 mm lens at T1.4”；f/ 光圈照写 f/2.8"""
    if not L.get("focal"):
        return "", ""
    ap = L["aperture"] if L["aperture"].upper().startswith("T") else "f/" + L["aperture"].lstrip("f/")
    macro_zh, macro_en = ("微距", " macro") if L.get("macro") else ("", "")
    return f"{L['focal']}mm {macro_zh}镜头 {ap}", f"{L['focal']} mm{macro_en} lens at {ap}"


def lighting_zh(shot, g, segment: str) -> str:
    """③ 一句布光：**灯光** 布光 + 色温 > **光线** 主光 / 补光 / 修正 > 场景表光线（按段取句）"""
    kv = PV.kv_field(script_field(shot, "灯光"))
    if kv.get("布光"):
        return "，".join(x for x in (kv.get("布光"), kv.get("色温")) if x)
    kv2 = PV.kv_field(script_field(shot, "光线"))
    if kv2:
        return "，".join(f"{k}{v}" for k, v in kv2.items() if k in ("主光", "补光", "轮廓光", "修正") and v)
    sid = _g(shot, "scene_id")
    ent = next((e for e in (_g(g, "scene_library") or []) if _g(e, "scene_id") == sid), None)
    light = (_g(ent, "light") or "") if ent else ""
    if "；" in light and ("现实段" in light or "幻想段" in light):
        for part in light.split("；"):
            if ("幻想" in part) == (segment == "fantasy"):
                return re.sub(r"^(?:现实段|幻想段)[:：]", "", part.strip())
    return light


def image_body(project: str, ep, shot, g, c, sr, plan, *, style_suffix: str) -> tuple[str, list[str], dict]:
    """→ (正文（不含参考槽标注；末尾带风格后缀）, 追加负面词, trace)。c = CompiledShot（取 image_body / 材质条目），sr = ShotRefs"""
    from ns_agent import framing as FR
    from ns_agent import asset_refs as AR
    opt = options()
    neg_tb = opt.get("negation_rewrites") or {}
    from ns_agent import compiler as C
    cast = cast_rows(shot)
    names = [r["name"] for r in cast]
    # 剧本 img 原文（去触发词 / 段落标记；静帧视线落点按 C-07 改向画面一侧），不用编译器按景别删句后的 image_body（小句边界已变）
    img = FR.off_axis_gaze(C._no_segment_marks(C._strip_triggers(_g(shot, "img_prompt") or "", list(_g(shot, "img_triggers") or []))) or "")
    size = FR.size_of(_g(shot, "img_prompt") or img) or sr.size
    angle_field = script_field(shot, "机位与景别")
    comp_field = script_field(shot, "构图")
    af = PV.split_field(angle_field)
    angle_seg = af[1] if len(af) >= 2 else angle_field
    focus_seg = af[2] if len(af) >= 3 else ""
    a = PV.parse_angle(angle_seg + ("｜" + focus_seg if "过肩" in angle_seg or "越" in focus_seg else ""), names)
    # W-PASS 11-07：第三段“起幅：…；落幅：…”只取起幅（首帧 = 起幅；尾帧走 frame_job.compose_last_shot 已换成落幅段）。
    # 原来整段进首句 → 一张图里写了两个时刻，ep02-S8 4 版里 2 版画成上下两格拼图
    focus_seg = PV.start_end(focus_seg)[0]
    reg = registry_en(g)
    en_of = lambda n: PV.short_en(reg.get(n, "")) if reg.get(n) else "the figure"   # noqa: E731
    sharp, soft = PV.focus_names(angle_field, comp_field, names)
    if not sharp:
        prim = next((x.get("name") for x in (sr.characters or []) if x.get("character") == sr.primary and x.get("name") in names), None)
        sharp = [prim] if prim else names[:1]
    # 露脸人数（⑤）：cast 在画且露脸（faced_people）且不是背影；站位 / img 里写“以…入画 / 只露 / 背影 / 被遮”或过肩被越的人不算露脸
    faced_all, partial = FR.faced_people(shot, names)
    _lead_cl, per, _tail = PV.person_segments(img, names)
    hidden = hidden_people(shot, angle=a)
    faced = [n for n in faced_all if n not in hidden and any(r["name"] == n and r["presence"] == "在画" for r in cast)]
    n_faced = len(faced)
    L = lens_of(shot, n_faced=n_faced, ots=a["azimuth"] == "ots", subject=sr.subject, size=size)
    lens_zh, lens_en = lens_text(L)
    # ① 首句（中 + 英）
    size_zh = f"{size}" + (f"（{PV.SIZE_NOTE_ZH[size]}）" if size in PV.SIZE_NOTE_ZH else "") if size else "中景"
    focus_zh = focus_seg or ("焦点在" + "、".join(sharp) + ("，" + "、".join(soft) + "略在焦外" if soft else "") if sharp else "")
    zh = "，".join(x for x in (size_zh, PV.angle_zh(a), lens_zh, L.get("dof_zh"), focus_zh) if x)
    focus_en = ""
    if sharp:
        if len(sharp) >= 2 and len(sharp) == len(names):
            focus_en = f"all {len(sharp)} figures in focus"
        else:
            focus_en = "focus on " + " and ".join(en_of(n) for n in sharp)
        if soft:
            focus_en += ", " + " and ".join(en_of(n) for n in soft) + " slightly soft"
    en = ", ".join(x for x in (f"{PV.SIZE_EN.get(size or '', 'a medium shot')} {PV.angle_en(a, en_of)}".strip(), lens_en, L.get("dof_en"), focus_en) if x)
    lead = f"{zh}（{en}）"
    # ② 人物行 + ⑤ 同框句（按露脸人数）
    looks = looks_of(ep, shot)
    lines, negs = [], []
    for r in cast:
        n = r["name"]
        if r["presence"] == "局部":
            lines.append(FR.PARTIAL_CLAUSE.format(name=n, part=r["part"]) if r.get("part") else f"{n}只在画面边缘露出局部")
            continue
        clauses = per.get(n) or []
        if not clauses:                                    # img 没按人写 → 用 **站位** 兜底
            st = next((p for p in PV.split_field(script_field(shot, "站位")) if p.startswith(n)), "")
            clauses = [x for x in re.split(r"[，、]", st.split("｜")[-1]) if x]
        line, ng = PV.person_line(n, clauses, looks.get(n), r.get("x"), r.get("depth"), neg_tb)
        if AR.back_view(shot, n):
            line = line.replace(n + "（", n + "背对镜头的背影（", 1) if "（" in line else n + "背对镜头的背影" + line[len(n):]
        lines.append(line)
        negs += [x for x in ng if x not in negs]
    count = PV.frame_sentence(faced, hidden)                  # 按露脸人数；不露脸的人写“只以虚化的 X 在前景”
    # L-211 ①（项目方 11-07 01:2x）：过肩前景 / 背影的不露脸者（hidden_people = 作业 cast.face_hidden；原文写到他的脸的除外）
    # → 明写“背对镜头，只见后脑与肩”，他的脸 / 侧脸进本镜负面词（ep02-S5/S7/S13 前景男人露出侧脸、没送脸 = 陌生脸，19 张里 5 张）。
    # 只对本镜这些人加；露出部位是手 / 腰侧等（被遮、局部）的不加
    described_n = {x.get("name") for x in (sr.characters or []) if x.get("face_described")}
    turned = [n for n, part in hidden.items() if n not in described_n and (part == "背影" or "肩" in part or "后脑" in part)]
    if turned:
        count = "，".join(x for x in (count, "，".join(f"{n}只见后脑与肩" if hidden[n] == "背影" else f"{n}背对镜头，只见后脑与肩" for n in turned)) if x)
    turned_negs = [x for n in turned for x in (f"{n}的脸", f"{n}的侧脸", f"{n}的侧面轮廓")]
    # ③ 场景 + 一句布光
    from ns_agent.gen_contract import style as ST
    seg = segment_of(shot)
    scene_detail = ST.strip_segment_marks(_g(shot, "scene_detail") or "") or ""
    scene_zh, ng = PV.rewrite_negations(scene_detail.strip("，。 "), neg_tb)
    negs += [x for x in ng if x not in negs]
    light_zh, ng = PV.rewrite_negations(lighting_zh(shot, g, seg), neg_tb)
    negs += [x for x in ng if x not in negs]
    # 材质 / 近景技术句沿用编译器条目（短）；安全句的否定部分已在负面词里
    extras = [m for m in (getattr(c, "material_terms", None) or []) if m]
    if not extras:
        from ns_agent import compiler as C
        extras = [m for m in C.material_terms(img + "，" + "，".join(v.get("服装", "") for v in looks.values()), present=set(names), bible=C.load_bible())
                  if FR.visible(m, size)]
        if size in FR.CLOSE or size in ("近景", "中近景"):
            extras.append(C.CLOSEUP_TERM)
    ref_note = (opt.get("ref_note") or "{names}的脸、发型、服装以参考图为准").format(names="、".join(n for n in names if n in faced) or "人物")
    parts = [lead + "。", count + "。" if count else "", "；".join(lines) + "。",
             ("场景：" + scene_zh + "。") if scene_zh else "", ("光线：" + light_zh + "。") if light_zh else "",
             "，".join(extras) + "。" if extras else "", ref_note + "。"]
    body = "".join(p for p in parts if p)
    body = body.rstrip("。") + "，" + style_suffix
    neg_extra = list(opt.get("negative_extra") or []) + negs + [x for x in turned_negs if x not in negs]
    n_vis = len([r for r in cast if r["presence"] == "在画"])
    if opt.get("extra_person_negative", True) and n_vis:
        neg_extra.append(f"第{({2: '二'}.get(n_vis + 1) or PV.ZH_NUM.get(n_vis + 1, n_vis + 1))}个人")  # 序数：第二个人（不是“第两个人”）
    trace = {"template": TEMPLATE_ID, "size": size, "angle": a, "lens": L, "focus": sharp, "soft": soft, "n_faced": n_faced,
             "people": len(cast), "negations_dropped": negs}
    return body, neg_extra, trace


# ---------------------------------------------------------------- 视频（§9 官方写法）
def video_dict(ep, shot, g, project: str | None, h3_body: str) -> dict:
    """compile_shot 路径：Shot / Episode / Globals → ns_schema.prompt_v2.video_body 的输入"""
    from ns_agent import camera_norm as CN
    opt = options()
    cast = cast_rows(shot)
    names = [r["name"] for r in cast]
    reg = registry_en(g)
    looks = looks_of(ep, shot)
    from ns_agent import framing as FR
    faced, _p = FR.faced_people(shot, names)
    chars = [{"name": r["name"], "en": reg.get(r["name"], ""), "look_en": (looks.get(r["name"]) or {}).get("EN", ""), "x": r["x"],
              "depth": r["depth"], "presence": r["presence"], "part": r["part"], "faced": r["name"] in faced} for r in cast]
    from ns_agent import framing as _FR
    size = _FR.size_of(_g(shot, "img_prompt") or "")
    angle_field = script_field(shot, "机位与景别")
    comp_field = script_field(shot, "构图")
    cam_field = script_field(shot, "运镜")
    mv = PV.split_field(cam_field)
    move_id = CN.match_zh(mv[0]) if mv else None
    amp, speed = PV.amp_speed_of(cam_field)        # 只读镜级 **运镜** 字段（两条路同源；**镜头运动** 落在集级 extras，不取）
    motion = _g(shot, "motion_prompt") or ""
    why = None
    if not CN.is_enabled(move_id, project, motion):
        big = bool(CN.BIG_ACTION.search(motion))
        move_id, why = CN.fallback(move_id, big)
    n_vis = len([r for r in cast if r["presence"] == "在画"])
    move_id, why2 = CN.multi_person_fallback(move_id, n_vis, project)      # L-192：双人及以上只允许 static / push_in / pull_out / handheld_follow
    why = why2 or why
    move = CN.move(move_id) or CN.move("static")
    sharp, _soft = PV.focus_names(angle_field, comp_field, names)
    seg = segment_of(shot)
    af = PV.split_field(angle_field)
    angle_seg = af[1] if len(af) >= 2 else angle_field
    if len(af) >= 3 and ("过肩" in angle_seg or "越" in af[2]):      # 过肩：第三段写“从 X 越肩拍 Y”
        angle_seg += "｜" + af[2]
    return {"size": size, "angle_field": angle_seg,
            "comp_field": comp_field, "chars": chars, "h3_body": h3_body,
            # AUDIT-RULES 11-05：L1 布光句按本镜 **灯光** 字段（动机光源 + 方向）生成，取不到才用配置通用句（不写死环形灯 + 正面）
            "lighting_en": PV.lighting_en_from(PV.kv_field(script_field(shot, "灯光")), seg, ((opt.get("lighting_en") or {}).get("generic") or {}).get(seg, "")),
            "constraint_en": opt.get("constraint_en", ""),
            "move": move, "move_id": move_id, "amp": amp, "speed": speed, "direction": opt.get("video_direction_default", "right"),
            "focus": sharp[0] if sharp else None, "fallback_why": why, "segment": seg,
            "templates": [m.get("template") for m in (CN.vocab().get("moves") or []) if m.get("template")]}


def video_h3_text(ep, shot, g, project: str | None, h3_text: str) -> tuple[str, dict]:
    """H3 全文（四块）→ 正文按 v2 重写，其余字段原样。→ (新全文, 记录)"""
    from ns_schema.h3_lint import FIELDS, parse_h3
    from . import h3_prompt as H3
    f = parse_h3(h3_text or "")["fields"]
    body = f.get(FIELDS[0], "")
    if not body:
        return h3_text, {"skipped": "no_body"}
    d = video_dict(ep, shot, g, project, body)
    new_body, rec = PV.video_body(d)
    rec.update(fallback_why=d.get("fallback_why"), segment=d.get("segment"), from_body=body, to_body=new_body)
    return H3.replace_field(h3_text, FIELDS[0], new_body), rec
