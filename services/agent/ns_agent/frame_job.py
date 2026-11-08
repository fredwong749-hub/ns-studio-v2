"""全系统唯一的首帧 / 尾帧出图作业构建（K-REN-134“全系统只有一条出图路径”，总调度 2026-11-01 P0）。

项目方原话（11-01 01:13 截图）：“你现在生成的人物不是林晚真实的形象……肯定有 bug……修复 reference 图片的 bug”。
查实：画布“重出首帧”走 ns_render.pipeline.render_image 的旧换脸式两步（图1 = 旧首帧构图，图2 = 旧母版裁切
data/cache/refs/master_face_b8e7b00b6756 = ar-c2351c9dcf），完全绕开 v9 配方、asset_refs 槽位与生成契约。

现在所有入口（tools/pipeline/image_jobs.py、tools/assetmaster/frames_v2.py、画布 frame-jobs 重出 / 尾帧 / 编辑整图重出）
都用这里的 build_first / build_last 得到作业，再由 ns_render.unified_frame.build_graph 得到 ComfyUI 图：
    提示词 = asset_refs 槽位标注（“图N是…必须完全一致”）+ 编译器输出（角色锁去服装、S★ 后缀、禁用 LoRA 剔除）
    参考   = asset_refs 槽位（项目方挑定的身份脸 > 去头造型图 > 景别单品 / 参考板 > 场景底图）
    闸门   = ns_agent.gen_contract.preflight（不过不提交）/ postflight
"""
from __future__ import annotations

from ns_agent import asset_refs as AR
from ns_agent import compiler as C
from ns_agent import prompt_template_v2 as PT2
import re
from ns_schema import prompt_v2 as PV


def retrigger(shot, g):
    """P4 / 系统剧本里 img / motion 句首的触发词按系统触发词表标出（编译器据此剥离 / 保留）"""
    table = {t.trigger for t in g.lora_triggers}

    def split(text):
        out = []
        for t in [x.strip() for x in (text or "").split("，")]:
            if t in table or t.startswith("pose_"):
                out.append(t)
            else:
                break
        return out
    it, mt = split(shot.img_prompt), split(shot.motion_prompt)
    return shot.model_copy(update={"img_triggers": it or list(shot.img_triggers),
                                   "motion_triggers": mt or list(shot.motion_triggers)})


def freeze_archived_extras(project: str | None, shot):
    """W7（11-03，A2 第 18 轮 P1-①）：已归档剧（runs.json status=archived / frozen，如 series10_v1）编译冻结。
    W2 起导入器把 构图 / 灯光 / 光线 / 分拍 / 镜头语言 v2 七段收进 shot.extras；V1 源稿每镜也写了 光线 / 分拍，
    重导后 first_beat_gaze / 镜头语言段会换内容（ep01-S6 视线「林晚的脸」→「走廊暗处」等）。归档剧只读，提示词不许变：
    这里把 shot.extras 过滤回 W2 之前的旧集合（importer.script_md.LEGACY_SHOT_EXTRA_LABELS，按去括注的 base 比），
    新旧导入稿编出的 prompt_sha 都和 W2 之前一样。非归档剧原样返回（同一个对象）"""
    if not project or not AR._archived(project):
        return shot
    from ns_agent.importer.script_md import LEGACY_SHOT_EXTRA_LABELS, _split_label
    ex = AR.g(shot, "extras") or []
    keep = [e for e in ex if _split_label(str(AR.g(e, "label") or ""))[0] in LEGACY_SHOT_EXTRA_LABELS]
    if len(keep) == len(ex):
        return shot
    if isinstance(shot, dict):
        return {**shot, "extras": keep}
    return shot.model_copy(update={"extras": keep})


def compile_frame(ep, shot, g, project: str | None, *, bible=None, available=None, assets: bool | None = None,
                  model: str = "qwen_image_2.1", legacy_refs: list[str] | None = None, role_lines=None, mode: str = "shot",
                  **plan_kw):
    """→ (提示词, CompiledShot, ShotRefs | None, SlotPlan | None)。assets=False 只给对照实验（旧做法：只挂脸）。
    mode="sheet"（造型设定图：正面全身站立）：不加单人朝向句（四分之三侧脸 / 眼睛看着…）、不补视线句、不加镜头语言段——
    这些是剧情镜的表演 / 摄影要求，和设定图“正面站立、目视前方”冲突（MERGE-IMG-O 11-03 第 6 项）"""
    from ns_agent.bible import load_bible
    from ns_agent.gen_contract import style as ST
    shot = freeze_archived_extras(project, shot)          # W7：归档剧编译冻结
    use_assets = bool(project and AR.project_cfg(project)) if assets is None else assets
    if use_assets:
        g, _removed = AR.locks_without_wardrobe(g)          # K-AST-232
    seg, _src = ST.segment_of(shot)
    c = C.compile_shot(ep, retrigger(shot, g), g, set(available or ()), image_style=ST.image_suffix(project, seg),   # 按剧 / 按段取风格（L-126 / L-130）
                       bible=bible or load_bible(), framing_project=project)
    if not use_assets:
        return (role_lines(legacy_refs or []) if role_lines else "") + c.image_prompt, c, None, None
    # 资产规划用编译器真正保留的触发词判主体（LoRA 不可用被剥掉的鞋触发词不算证据），与编译器同口径（REFLOCK 11-01）
    plan_kw.setdefault("kept_triggers", [t for t in (AR.g(shot, "img_triggers") or []) if t not in (c.dropped_triggers or [])])
    sheet = mode == "sheet"
    sr, plan = AR.plan_for(project, ep, shot, model, turn=not sheet, **plan_kw)
    note = (shot_language_note(project, shot, mode) + plan.prompt_note + outer_items_note(project, ep, shot, sr, c.image_prompt)
            + ("" if sheet else eyeline_note(shot, sr, c.image_prompt))
            + garment_detail_note(project, ep, shot, sr, c.image_prompt) + blocking_note(shot, c.image_prompt))
    # L-130：段落标记在编译器出口统一清掉（compiler._no_segment_marks，ISS-1103-005241-bf53）；这里原来的 model_copy 分支对
    # dataclass 的 CompiledShot 从不生效，有 scene_id 的镜标记就漏进了“本镜场景：”
    return plan.prompt_prefix + with_note(c.image_prompt, note), c, sr, plan


def shot_language_note(project: str | None, shot, mode: str = "shot") -> str:
    """ISS-1103-023528-2869：镜头语言模板 v2 首帧段（② 镜头 → ③ 构图 → ④ 灯光 → L-110 融合句，固定顺序，按剧开关），
    放在正文 + 【场景】之后、其余补句之前；设定图模式不加"""
    if mode == "sheet":
        return ""
    from ns_agent import shot_language as SL
    return SL.image_note(project, shot)


def blocking_note(shot, body: str) -> str:
    """K-REN-952 站位句（cast x 唯一来源，句式 asset_refs.BLOCKING_NOTE_RE）；开关 promptfix.blocking_from_cast"""
    return AR.blocking_note(shot, body) if AR.pf_on("blocking_from_cast") else ""


# K-REN-101（总调度 11-01 07:35，VIDEO10 查出 ep01 首帧丢了造型 A 的短款西装外套：剧本 img 行没写外套，图就照着出了）：
# 本镜景别可见的造型单品（与 asset_refs.resolve_shot 给设定图 / 单品排参考时同一份清单：本集造型 parts ∩ 景别可见区，
# 去掉 marker，备选单品按 part_active）里，正文没写到的，用一句固定说明补进出图正文（风格后缀之前，同 with_note）。
# 剧本原文不改。固定句式改了 → 同一次改动里让 ns_api.unified_stale.match_target 认得出（OUTER_NOTE_RE），否则新版本会被误标过期
OUTER_NOTE = AR.OUTER_ITEMS_NOTE          # 句式与识别式唯一来源：asset_refs（TEXT_NOTE_RES，DATAALIGN 11-01 合并）
OUTER_NOTE_RE = AR.OUTER_ITEMS_RE


# 造型登记名 → 出图时更明确的叫法（模型对“西装裙”常画成西裤；造型 A 的 EN 是 dark grey pencil skirt）
GARMENT_ALIAS = {"深灰西装裙": "深灰铅笔裙 / 深灰西装裙——及膝裙，不是长裤"}


def outer_items(project: str, ep, shot, sr=None, body: str | None = None) -> list[tuple[str, str, list[str]]]:
    """→ [(名字, 造型, [本镜景别可见、正文没写到的单品短名])]；与 asset_refs.resolve_shot 的可见单品同一套规则"""
    img = AR.g(shot, "img_prompt") or ""
    n_ep = int(AR.g(ep, "number") or 0)
    _size, subject, zones = AR.visible_zones(img)
    if subject in ("part", "object") or not (zones - {"face"}):
        return []
    looks = AR.load_looks(project)
    chars = (sr.characters if sr is not None else None) or []
    out = []
    for ch in chars:
        cid, lid, name = ch.get("character"), ch.get("look"), ch.get("name") or ch.get("character")
        if ch.get("wardrobe_scope") is not None or ch.get("back_view"):   # K-REN-951 / 957：局部不露脸、背影的人不补“要穿全”
            continue
        if ch.get("presence") == "局部" and not ch.get("face_described"):   # FIX-PRE 11-06（REVIEW-PRE D）：局部的人只露部位 → partial_items
            continue
        L = (looks.get(cid) or {}).get(lid) if lid else None
        if L is None:
            continue
        # 服装件（outfit：上衣 / 外套 / 下装）不按景别裁：模型常把近景画宽，露出的下装若没被点名就会自己编（总调度 11-01：
        # ep01-S5 近景三次都画成黑长裤，造型 A 是深灰西装裙 / 铅笔裙）；鞋、饰品、指甲仍按景别可见区
        # ⤴ IMGFIX 11-01（总调度 fredw-2b 21:5x）：特写例外——特写只补景别内看得到的单品。ep01-S1 特写被补了“深灰铅笔裙——及膝裙”，
        # 模型为画出裙子把特写拉成中景（data/ab/imgfix 全部 S1 出图都是坐姿中景）；近景及更远照旧补全部服装件
        loose = _size != "特写"
        vis = [x for x in L.parts if (x.zone in zones or (loose and x.slot == "outfit")) and x.slot != "marker"
               and AR.part_active(x, n_ep, img)]
        # K-REN-707（开关 close_no_lower）：特写 / 近景 / 中近景不补看不见的下半身单品 / 描述（露出长腿、裙、鞋）
        if AR.FR.close_no_lower() and _size in AR.FR.CLOSE_NO_LOWER:
            vis = [x for x in vis if AR.FR.visible_garment(AR._short(x.label), _size)]
        if any(x.extra and x.slot == "shoes" for x in vis):
            vis = [x for x in vis if not (x.slot == "shoes" and not x.extra)]
        names = []
        for x in sorted(vis, key=lambda x: AR.ZONE_ORDER.get(x.zone, 9)):
            orig = AR._short(x.label)
            if AR.FR.close_no_lower() and _size in AR.FR.CLOSE_NO_LOWER and not AR.FR.visible(orig, _size):
                orig = AR.FR.visible_items(orig, _size) or orig    # ISS-1103-024205-8856：一体件只补看得见的那部分（近景不补丝袜 / 高跟）
            lab = GARMENT_ALIAS.get(orig, orig)
            text = body if body is not None else img          # 出图正文（编译器会按景别删掉剧本里的服装词）没写到才补
            core = orig.split("：", 1)[-1].strip()           # “日常全身装：黑色漆皮抹胸紧身短裙”：正文写了冒号后的单品名就算写到
            if orig and orig not in text and core not in text and lab not in names:
                names.append(lab)
        if names:
            out.append((name, lid, names))
    return out


def partial_items(project: str, ep, shot, sr=None, body: str | None = None) -> list[tuple[str, str, list[str]]]:
    """FIX-PRE 11-06（REVIEW-PRE D / K-REN-951）：局部（不露脸 / 过肩前景 / 背影）的人 → [(名字, 部位, [部位内看得到、正文没写到的
    鞋 / 饰品 / 指甲短名])]。可见区 = cast 部位（asset_refs.part_scope_zones），服装件不补（不露整套），对不上部位 = 不补"""
    img = AR.g(shot, "img_prompt") or ""
    n_ep = int(AR.g(ep, "number") or 0)
    looks = AR.load_looks(project)
    text = body if body is not None else img
    parts_by_name = {AR.g(c, "name"): str(AR.g(c, "part") or "").strip() for c in AR.g(shot, "cast") or []}
    out = []
    for ch in (sr.characters if sr is not None else None) or []:
        if ch.get("presence") != "局部" or ch.get("face_described"):
            continue
        cid, lid, name = ch.get("character"), ch.get("look"), ch.get("name") or ch.get("character")
        L = (looks.get(cid) or {}).get(lid) if lid else None
        scope = set(ch["wardrobe_scope"]) if ch.get("wardrobe_scope") is not None else AR.part_scope_zones(shot, name)
        if L is None or not scope:
            continue
        names = []
        for x in sorted(L.parts, key=lambda x: AR.ZONE_ORDER.get(x.zone, 9)):
            if x.zone not in scope or x.slot in ("outfit", "marker") or not AR.part_active(x, n_ep, img):
                continue
            lab = AR._short(x.label)
            core = lab.split("：", 1)[-1].strip()
            if lab and lab not in text and core not in text and lab not in names:
                names.append(lab)
        if names:
            out.append((name, parts_by_name.get(name) or "部位", names))
    return out


def outer_items_note(project: str, ep, shot, sr, body: str = "") -> str:
    """整套造型的人：OUTER_NOTE（要穿全）；局部的人：AR.PART_ITEMS_NOTE（只补部位内单品）。顺序 = TEXT_NOTE_RES（整套在前、局部在后）"""
    items = outer_items(project, ep, shot, sr, body=body or None)
    part = partial_items(project, ep, shot, sr, body=body or None)
    return ("".join(OUTER_NOTE.format(name=n, lid=lid, items="、".join(xs)) + "。" for n, lid, xs in items if xs)
            + "".join(AR.PART_ITEMS_NOTE.format(name=n, part=pt, items="、".join(xs)) + "。" for n, pt, xs in part if xs))


# ACTING 11-01（R11-2 / R11-8）：出图正文没写视线（frame_spec.GAZE_WORDS 一个都没有）时，补一句固定视线说明；
# 句式唯一来源 asset_refs.EYELINE_NOTE（进 TEXT_NOTE_RES，ns_api.unified_stale.match_target 自动认）。落点取本镜第一拍 视线=（剧本原文），
# 没写就用“画面里的人或物”。剧本原文不改；局部 / 物件特写不加
GAZE_WORDS = ("看", "望", "瞥", "视线", "眼神", "盯", "目光", "迎上", "垂眼", "闭眼", "抬眼")


def first_beat_gaze(shot) -> str:
    import re as _re
    for e in AR.g(shot, "extras") or []:
        if str(AR.g(e, "label") or "").startswith("分拍"):
            m = _re.search(r"视线\s*[=＝]\s*([^｜|；;]{1,30})", str(AR.g(e, "text") or ""))
            v = (m.group(1).strip() if m else "").strip("。，")
            if v and v not in ("—", "-", "无") and "镜头" not in v:
                return v
    # 导入后的系统剧本不带 分拍：退到 motion 行里第一处“视线 / 目光 + 转向 / 落在 / 停在 / 看向 …”的落点
    m = _re.search(r"(?:视线|目光)(?:转向|落在|停在|移向|看向|投向|盯着|锁在)([^，。；—]{1,20})", str(AR.g(shot, "motion_prompt") or ""))
    if m and "镜头" not in m.group(1):
        return m.group(1).strip()
    return ""


def _first_beat(shot) -> str:
    for e in AR.g(shot, "extras") or []:
        if str(AR.g(e, "label") or "").startswith("分拍"):
            return str(AR.g(e, "text") or "").split("；", 1)[0]
    return ""


def _react_gaze(beat: str, name: str) -> str:
    """第一拍 反应=“<名字>：视线落在…，…” → 落点（剧本原文）"""
    import re as _re
    m = _re.search(r"反应\s*[=＝][^｜|]*?" + _re.escape(name) + r"\s*[:：]?([^｜|；;]{1,40})", beat)
    if not m:
        return ""
    g = _re.search(r"(?:视线|目光)(?:落在|停在|移向|转向|看向|投向|锁在)?([^，。、]{1,20})|(?:看向|望向|看着|盯着|瞥向)([^，。、]{1,20})", m.group(1))
    v = ((g.group(1) or g.group(2)) if g else "").strip()
    return "" if (not v or "镜头" in v) else v


def _covered(body: str, names: list[str]) -> set:
    """正文按小句归人（同 QW-A7）：归给某人的小句里有视线词就算写了；写实套话“眼睛为真实人眼大小”不算"""
    import re as _re
    b = (body or "").replace("眼睛为真实人眼大小", "")
    here = [n for n in names if n in b]
    segs, cur = {n: "" for n in here}, None
    # 按逗号 / 分号切小句、按主语归人（与 tools/qa/acting_lints.person_clauses 同一套切法，改一处要同步另一处）
    for cl in _re.split(r"[，,；;。]", b):
        c = cl.strip()
        if not c:
            continue
        lead = next((n for n in here if c.startswith(n) or c[1:].startswith(n)), None)
        if lead:
            cur = lead
        for n in here:
            if _re.search(_re.escape(n) + r"(?:的)?(?:视线|目光|眼神|垂眼|垂眸|抬眼|闭眼|看|望|盯|瞥)", c):
                segs[n] += c + "，"
        if cur:
            segs[cur] += c + "，"
    return {n for n, seg in segs.items()
            if any(w in seg for w in GAZE_WORDS) or any(w in seg for w in ("背对", "背影", "背身"))}


def eyeline_note(shot, sr, body: str) -> str:
    """SCRIPT-RSI 11-01（D-SCR-07，项目方 18:2x“所有图片不要看镜头”）：⤴ 原先只给第一个人、且正文有任何视线词就整句不加——
    双人镜第二个人没写视线时照样默认看镜头。现在逐人判断：在画的人里谁那一段没写视线，就给谁补一句固定的正向落点句。
    落点：第一拍的主角取 视线=，其他人取第一拍 反应= 里自己的视线；都没有时，多人镜看另一个在画的人，单人镜写“画面里的人或物”。"""
    img = AR.g(shot, "img_prompt") or ""
    _size, subject, _zones = AR.visible_zones(img)
    if subject in ("part", "object"):
        return ""
    chars = (sr.characters if sr is not None else None) or []
    names = [n for n in (c.get("name") or c.get("character") for c in chars) if n] or [
        AR.g(c, "name") for c in AR.g(shot, "cast") or [] if AR.g(c, "presence") == "在画" and AR.g(c, "name")]
    names = list(dict.fromkeys(names))
    # K-REN-903（IMGLAB 11-02，ep05-S3/S6/S7 局部人物被补“视线落在对方的脸上”→ 画出整张脸）：视线句只给露脸的人补，
    # 落点也不写不露脸的人的脸
    if AR.FR.partial_only_enabled() and AR.g(shot, "cast") is not None:
        faced = set(AR.FR.faced_people(shot, names)[0])
        names = [n for n in names if n in faced]
    if not names:
        return ""
    if len(names) == 1:
        if any(w in (body or "").replace("眼睛为真实人眼大小", "") for w in GAZE_WORDS):
            return ""
        return AR.EYELINE_NOTE.format(name=names[0], target=(first_beat_gaze(shot) or "画面里的人或物")[:30]) + "。"
    done = _covered(body, names)
    beat = _first_beat(shot)
    act = beat.split("｜")[1] if beat.count("｜") >= 1 else ""
    hits = sorted((act.find(n), n) for n in names if n in act)
    lead = hits[0][1] if hits else names[0]
    out = []
    for n in names:
        if n in done:
            continue
        t = (first_beat_gaze(shot) if n == lead else _react_gaze(beat, n)) or next(
            (f"{o}的脸" for o in names if o != n), "画面里的人或物")
        t = t.strip()[:30]
        out.append(AR.EYELINE_NOTE.format(name=n, target=t[:-1] if t.endswith("上") and len(t) > 1 else t) + "。")
    return "".join(out)


def garment_detail_note(project: str, ep, shot, sr, body: str = "") -> str:
    """IMGFIX r5（总调度 fredw-2b 11-02）：在画（或局部背影）且上身入画的人，本造型外层单品的衣橱登记描述补一句
    （例：“林晚的短款西装外套：两只袖子都是深灰西装袖，露米白衬衫袖口”）。局部 / 物件特写、上身不入画不加；正文已有这句不重复"""
    img = AR.g(shot, "img_prompt") or ""
    _size, subject, zones = AR.visible_zones(img)
    if subject in ("part", "object"):
        return ""
    out = []
    for ch in (sr.characters if sr is not None else None) or []:
        name, cid, lid, pr = ch.get("name"), ch.get("character"), ch.get("look"), ch.get("presence", "在画")
        back = pr == "局部" and AR.back_part(shot, name)
        if not (pr == "在画" or ch.get("face_described") or back):
            continue
        if not back and not (zones & {"upper", "body"}):
            continue
        for label, desc in AR.garment_details(project, cid, lid):
            s = AR.GARMENT_DETAIL_NOTE.format(name=name, label=label, desc=desc)
            if s not in (body or "") and desc not in (body or ""):
                out.append(s + "。")
    return "".join(out)


def with_note(body: str, note: str) -> str:
    """槽位不足的文字说明放在正文末尾、系统风格后缀之前：景别句永远是参考标注之后的第一句（K-REN-101）"""
    if not note:
        return body
    from ns_agent.gen_contract import style as ST
    i = -1
    for s_ in [ST.image_suffix()] if not ST._manifest_mode(ST._run()) else [f"{x}，{ST.NO_TEXT}" for x in ST.run_suffixes()]:
        i = max(i, body.rfind(s_) if s_ else -1)            # 分段剧：哪一段的风格后缀都认（L-130）
    if i > 0:
        head = body[:i].rstrip("，, ")
        return f"{head}，{note.rstrip('。')}，{body[i:]}"
    return f"{body.rstrip('。')}。{note}"


def cast_of(shot) -> list[dict]:
    """作业 cast（人数闸 tools/vqa/accept.people_range 读）。face_hidden：在画但不露脸（过肩前景只露肩 / 后脑、背影、被遮，
    prompt_template_v2.hidden_people）→ 计上限不计下限（PS-MASTER 11-05）"""
    hidden = PT2.hidden_people(shot) if AR.g(shot, "cast") else {}
    return [{"name": AR.g(c, "name"), "presence": AR.g(c, "presence"), "x": AR.g(c, "x"), "depth": AR.g(c, "depth"),
             **({"face_hidden": True, "hidden_part": hidden[AR.g(c, "name")]} if AR.g(c, "name") in hidden else {})}
            for c in (AR.g(shot, "cast") or [])]


def build_first(project: str, ep, shot, g, *, bible=None, model: str = "qwen_image_2.1", prompt_override: str | None = None,
                mode: str = "shot", continuity_ref: tuple[str, str] | None = None, frame_stage: str | None = None, **plan_kw) -> dict:
    """首帧作业（= 生成契约请求的超集）。prompt_override：画布一次性整段替换的正文（槽位标注照加在前面）。
    continuity_ref=(文件, 标注句模板)（L-203 尾帧 compose 用）：在槽位参考之后再挂一张“连续性参考”（通过契约的首帧），标注句
    放在槽位标注之后、正文之前，模板里的 {n} = 它的图号；参考槽逻辑（脸 / 造型 / 场景底图）与首帧完全同一套"""
    from ns_agent.gen_contract import style as ST
    n = int(AR.g(ep, "number"))
    shot = freeze_archived_extras(project, shot)          # W7：归档剧编译冻结（eyeline / 镜头语言段读同一份）
    sid = f"ep{n:02d}-S{AR.g(shot, 'index')}"
    full, c, sr, plan = compile_frame(ep, shot, g, project, bible=bible, model=model, mode=mode, **plan_kw)
    body = full[len(plan.prompt_prefix):]
    tmpl, tmpl_trace, neg_extra = None, None, []
    if not prompt_override and mode == "shot" and PT2.image_on(project):
        # 阶段 2.1 提示词模板 v2（CODER-PT 11-05，PLAN_EP01_FINISH §8 ①–⑤）：正文按 Qwen-Image 模板重编（机位首句置前、去【角色】块、
        # 人物一行、场景 + 一句布光、否定进负面词、按露脸人数写同框）；参考槽标注（plan.prompt_prefix）与契约补句（with_note）不变。
        # 按剧开关 run_style.json runs.<剧>.prompt_template.image，默认 v1 = 本分支不进
        seg_t, _ = ST.segment_of(shot)
        body, neg_extra, tmpl_trace = PT2.image_body(project, ep, shot, g, c, sr, plan, style_suffix=ST.image_suffix(project, seg_t))
        body = with_note(body, plan.prompt_note + outer_items_note(project, ep, shot, sr, body) + eyeline_note(shot, sr, body)
                         + garment_detail_note(project, ep, shot, sr, body) + blocking_note(shot, body))
        tmpl = PT2.TEMPLATE_ID
    if prompt_override:
        seg_in, seg_src = ST.segment_of(shot)       # P1-1（REVIEW-G4）：改稿路径按本剧本段取后缀（与 :323 同一函数），不再取当前剧缺省段
        body = prompt_override if ST.preset_suffix(project, seg_in) in prompt_override else f"{prompt_override}，{ST.image_suffix(project, seg_in)}"
        body = with_note(body, shot_language_note(project, shot, mode) + plan.prompt_note + outer_items_note(project, ep, shot, sr, body)
                         + ("" if mode == "sheet" else eyeline_note(shot, sr, body))
                         + garment_detail_note(project, ep, shot, sr, body) + blocking_note(shot, body))
    cont, cont_line, extra_refs = None, "", []
    if continuity_ref:                                                         # L-203：首帧作最后一张参考（弱指令，只保一致性）
        cont = continuity_slot(plan, model, continuity_ref[0], continuity_ref[1])
        cont_line, extra_refs = cont["line"], [continuity_ref[0]]
    job = {"id": sid, "shot": sid, "ep": n, "index": AR.g(shot, "index"), "stage": "image", "model": model,
            "prompt": plan.prompt_prefix + cont_line + ST.strip_segment_marks(body), "refs": plan.files + extra_refs, "asset_refs": AR.record(sr, plan), "cast": cast_of(shot),
            "loras": [["krea2_film_texture_qwen21.safetensors", 0.5]],
            "compiled_by": "ns_agent.frame_job.build_first → ns_agent.compiler.compile_shot", "framing_project": project,
            "recipe": "frame_v9（K-REN-134 唯一出图路径）", "last_prompt": c.last_prompt,
            "face_files": [s.file for s in plan.slots if s.kind == "face"], "compiler_inputs_sha": c.inputs_sha,
            "blocking": AR.blocking_of(shot), "last_faces": last_face_plan(project, shot, c.last_prompt, plan)}
    if cont:
        job["continuity_ref"] = cont
    job["route"], job["route_reasons"] = AR.plan_route(project, sr, plan)      # K-REN-958：本地专用资产 → 只许自有机器的道（L-128）
    seg_in, seg_src = ST.segment_of(shot)                                      # L-130：幻想 / 现实段 → 风格预设
    pid, seg, dflt = ST.segment_preset(project, seg_in)
    if pid:
        job["preset"], job["style_segment"] = pid, seg
        job["style_warning"] = (f"本镜没有幻想 / 现实段标记，按缺省 {seg} 段出（L-130，写作侧应在节拍里标【幻想段】/【现实段】）"
                                if dflt else None)
    # CARRYOVER 第 11 项（MERGE-IMG-O 11-03）：负面词 = 编译器负面词（剧本 base_negative + bible style / safe 负面 + 未成年人外观硬负面）
    # ∪ W-40 品牌负面词。原来直接覆盖成 brand_negative（V2 多为空），硬负面词进不了作业。硬负面永远在（job_negative 兜底）
    tier, tier_ov = first_frame_tier(project, episode=n)                       # L-191 / L-206 ②：按剧 + 按集取首帧档（默认 preview，不加字段）
    if tier == "turbo_previz":                                                 # L-206 ②：8 步 turbo + 摆位图（previz 深度控制）
        stage_pv = frame_stage or ("last" if continuity_ref else "first")      # 尾帧 compose：摆位图按落幅改写后的字段算（config last_frame.previz）；
        # W-DETECT 11-06（复审）：阶段显式传（frame_stage），不再从“有没有首帧参考”推——fl_escalate 去掉首帧参考时尾帧摆位图照算
        pv, why = previz_or_none(sid, shot, g, stage=stage_pv)
        if pv:
            job["previz"] = pv
        else:                                                                  # 摆位图算不出 / 没开 → 裸 turbo（同采样参数、不挂控制图），永不拦本集
            tier, tier_ov = first_frame_tier(project, force=previz_fallback_tier())
            job["previz_missing"] = why
    if tier_ov:
        job["tier"], job["tier_overrides"] = tier, tier_ov
    job["negative"] = job_negative(c.image_negative, AR.brand_negative(project, plan), "、".join(neg_extra))
    if tmpl:                                                                   # 阶段 2.1：模板进作业（签名 / take.params / 重出判定都认它）
        job["prompt_template"], job["prompt_template_trace"] = tmpl, tmpl_trace
        job["compiled_by"] += " + ns_agent.prompt_template_v2"
    job["adult_reasons"] = AR.plan_adult(project, sr, plan)                    # L-128 不变项：成人画面作业只由项目方触发
    job["adult"] = bool(job["adult_reasons"])
    job["compile_trace"] = trace_of(job)
    return job


_REF_NO_RE = re.compile(r"^图\d+是")
LAST_OUTFIT_BY_FIRST = "；{name}的服装、鞋、饰品以本镜头首帧为准"


def continuity_slot(plan, model: str, file: str, line_tmpl: str) -> dict:
    """L-203：给“连续性参考”（通过契约的首帧）腾出最后一个图位。参考槽按首帧同一套逻辑排好后，模型还有空位 → 直接追加；
    槽已满（TextEncodeQwenImage21 只有 image_1..3）→ 最后一个非身份脸槽（造型板 / 单品 / 场景底图）让位给首帧（首帧本身就带着
    这一镜的服装与陈设），身份脸 / 脸板一张不丢（Q1 硬项）；让位的槽记进 plan.dropped、标注句末尾补“X 的服装以首帧为准”。
    原地改 plan（slots / prompt_prefix）→ {file, n, line, replaced}"""
    cap = AR.model_slots(model)
    replaced = None
    if len(plan.slots) >= cap:
        # L-204 ②（11-06）：让位的是造型设定图 / 参考板 / 单品（首帧本身带着服装），不是场景底板；只剩底板可让时才让底板
        victim = (next((s_ for s_ in reversed(plan.slots) if s_.kind in ("sheet", "board", "item")), None)
                  or next((s_ for s_ in reversed(plan.slots) if s_.kind not in ("face", "faceboard")), None))
        if victim is not None:
            joined = "".join(s_.line for s_ in plan.slots)
            head = plan.prompt_prefix[:-len(joined)] if joined and plan.prompt_prefix.endswith(joined) else plan.prompt_prefix
            plan.slots.remove(victim)
            for k, s_ in enumerate(plan.slots, 1):
                if s_.index != k:
                    s_.line = _REF_NO_RE.sub(f"图{k}是", s_.line, count=1)
                    s_.index = k
            plan.prompt_prefix = head + "".join(s_.line for s_ in plan.slots)
            plan.dropped.append({"file": victim.file, "label": "、".join(victim.labels), "character": victim.character, "role": victim.kind,
                                 "why": "L-203 尾帧：参考槽已满，让位给本镜头首帧（服装 / 陈设以首帧为准）"})
            replaced = {"file": victim.file, "kind": victim.kind, "character": victim.character, "labels": list(victim.labels)}
    n_ref = len(plan.slots) + 1
    line = line_tmpl.format(n=n_ref)
    if replaced and replaced.get("character"):
        line = line.rstrip("。") + LAST_OUTFIT_BY_FIRST.format(name=AR.ID_NAME.get(replaced["character"], replaced["character"])) + "。"
    return {"file": file, "n": n_ref, "line": line, "replaced": replaced}


# ---------------------------------------------------------------- PROMPTFIX 11-02（ALIGNMENT 第 2 / 5 / 6 步）
def _sha16(s: str) -> str:
    import hashlib
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:16]


def trace_of(job: dict) -> dict:
    """每个出图作业的 compile_trace（随作业走、随 take 落账 params.compile_trace）：提示词 sha、参考图列表（文件 + 内容 sha）、
    站位契约、送脸决定、矛盾告警。两台同一 sv 版本编同一镜 → prompt_sha / refs_sha 必须相同（tools/promptfix/compile_sha.py 比对）"""
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    refs = []
    for f in job.get("refs") or []:
        p = Path(f) if Path(f).is_absolute() else root / f
        refs.append({"file": f, "sha": AR.sha_file(p) if p.is_file() else None})
    ar = job.get("asset_refs") or {}
    return {"schema": "nsfs.compile_trace/0.5", "shot": job.get("shot"), "stage": job.get("stage"),
            "prompt_sha": _sha16(job.get("prompt") or ""), "refs": refs,
            "refs_sha": _sha16(json.dumps([r["sha"] or r["file"] for r in refs])),
            "compiler_inputs_sha": job.get("compiler_inputs_sha"), "blocking": job.get("blocking"),
            "prompt_template": job.get("prompt_template"),            # 阶段 2.1：v2 模板随 take 落账（None = 现行 v1）
            "faces": face_ids(ar.get("slots") or []),
            "last_faces": job.get("last_faces"), "style_preset": job.get("preset"), "style_segment": job.get("style_segment"),
            "style_warning": job.get("style_warning"), "route": job.get("route"), "route_reasons": job.get("route_reasons"), "adult": job.get("adult"),
            "conflicts": [w for w in ar.get("warnings") or [] if w.get("kind") == "cast_face_conflict"],
            "wardrobe_scope": {c.get("name"): c.get("wardrobe_scope") for c in ar.get("characters") or []
                               if c.get("wardrobe_scope") is not None},
            "rules": ["K-REN-950", "K-REN-951", "K-REN-952", "K-REN-953", "K-REN-954", "K-REN-955", "K-REN-957"]}


TIER_SAMPLING_KEYS = ("unet", "steps", "cfg", "sampler", "scheduler")


def _tier_cfg() -> dict:
    try:
        return _json.loads((_ROOT / "config" / "render_quality.json").read_text(encoding="utf-8")).get("first_frame_tier") or {}
    except (OSError, ValueError):  # arch-ok: 读不到 render_quality.json = 没有首帧档（preview 默认路径，不放行任何闸门）
        return {}


def tier_name(runs_value, episode: int | None = None) -> str:
    """L-206 ②：runs[剧] 的两种写法 → 档名。字符串 = 全剧一档（L-191 旧写法，原样兼容）；
    {"default": "base35", "from_episode": {"2": "turbo_previz"}} = 从第 N 集起改档（取 ≤ 本集的最大集号；没给集号 / 都不够 → default）。
    非法键（非整数）忽略；其它类型 → preview"""
    if runs_value is None:
        return "preview"
    if isinstance(runs_value, str):
        return runs_value or "preview"
    if not isinstance(runs_value, dict):
        return "preview"
    tier = str(runs_value.get("default") or "preview")
    if episode is None:
        return tier
    best = None
    for k, v in (runs_value.get("from_episode") or {}).items():
        try:
            kn = int(k)
        except (TypeError, ValueError):
            continue
        if kn <= int(episode) and (best is None or kn > best[0]) and v:
            best = (kn, str(v))
    return best[1] if best else tier


def first_frame_tier(project: str | None, force: str | None = None, episode: int | None = None) -> tuple[str, dict]:
    """L-191：按剧取首帧出图档（config/render_quality.json first_frame_tier）。→ ("preview", {}) 默认；("base35", 采样覆盖)；
    L-206 ②：("turbo_previz", {unet: turbo, steps: 8, cfg: 1.0})——8 步 turbo + 摆位图深度控制（控制图见 previz_inputs）；
    ("turbo", 同采样参数) = turbo_previz 的退路（摆位图算不出）。episode：按集切档（tier_name）。force 仅供回退 / 实验"""
    cfg = _tier_cfg()
    if not cfg:
        return "preview", {}
    tier = force or tier_name((cfg.get("runs") or {}).get(project or ""), episode)
    if tier == "base35" and cfg.get("base35"):
        return "base35", {k: v for k, v in cfg["base35"].items() if not k.startswith("_")}
    if tier == "turbo_previz" and cfg.get("turbo_previz"):
        return "turbo_previz", {k: v for k, v in cfg["turbo_previz"].items() if k in TIER_SAMPLING_KEYS}
    if tier == "turbo" and (cfg.get("turbo") or cfg.get("turbo_previz")):
        src = cfg.get("turbo") or cfg.get("turbo_previz")
        return "turbo", {k: v for k, v in src.items() if k in TIER_SAMPLING_KEYS}
    return "preview", {}


def previz_fallback_tier() -> str:
    """turbo_previz 摆位图算不出时的档（config turbo_previz.fallback，默认 turbo = 同 8 步不挂控制图；L-206 ②“永不拦本集”）"""
    return str((_tier_cfg().get("turbo_previz") or {}).get("fallback") or "turbo")


def previz_or_none(sid: str, shot, g, stage: str = "first") -> tuple[dict | None, str]:
    """previz_inputs 的失败关闭包装：→ (previz, "") 或 (None, 原因)。摆位图任何异常都只是“本镜不挂控制图”，不抛、不拦"""
    cfg = _tier_cfg().get("turbo_previz") or {}
    if stage == "last" and not (cfg.get("last_frame") or {}).get("previz", True):
        return None, "config turbo_previz.last_frame.previz=off（尾帧裸 turbo）"
    try:
        pv = previz_inputs(sid, shot=shot, g=g, stage=stage)
    except Exception as e:  # noqa: BLE001  # arch-ok: L-206 ②：摆位图算不出 → 退回裸 turbo，不拦本集；原因记进作业 previz_missing
        return None, f"摆位图失败 {type(e).__name__}: {str(e)[:160]}"
    return (pv, "") if pv else (None, "没有摆位图（auto 关 / 没有手工深度图）")


def _auto_previz(sid: str, shot, g, cfg: dict, stage: str) -> dict:
    """tools/previz/auto_blocking.ensure_previz（CPU 光线投射，按输入哈希缓存）。sys.path 懒加载同 sheet_intake 的做法"""
    import sys as _sys
    for p in (str(_ROOT / "tools" / "previz"), str(_ROOT / "packages" / "schema")):
        if p not in _sys.path:
            _sys.path.insert(0, p)
    import auto_blocking as AB
    auto = cfg.get("auto") or {}
    fields = AB.fields_from_shot(shot)
    cast = AB.cast_from_locks(AR.g(g, "character_locks") if g is not None else None)
    return AB.ensure_previz(sid, fields, cast, root=_ROOT, dir_=cfg.get("dir") or "data/previz",
                            scale=float(auto.get("render_scale") or AB.DEFAULT_SCALE), stage=stage)


def previz_inputs(sid: str, shot=None, g=None, stage: str = "first") -> dict | None:
    """L-206 ②：本镜摆位图（深度控制图）+ 控制参数（config turbo_previz）。
    给了 shot（导入稿镜对象）且 auto.enabled → 自动摆位（auto_blocking.ensure_previz：分镜字段 → 摆位 JSON → 深度图，按输入哈希缓存
    data/previz/<ep>-S<n>/auto[_last]/<sha12>/）；没给 shot / auto 关 → 旧的手工深度图 data/previz/<ep>-S<n>/out/S<n>_depth.png。
    → {control, sha, file_sha, shot_spec, strength, start_percent, end_percent, auto{…}}；没有 → None；自动摆位出错原样抛（previz_or_none 接）。
    sha = 输入哈希（两台机器各自算同输入必相同；作业签名 / 重出判定认它，不认 PNG 字节）"""
    cfg = _tier_cfg().get("turbo_previz") or {}
    if not cfg:
        return None
    m = re.match(r"^(ep\d+)-S(\d+)$", sid or "")
    if not m:
        return None
    auto = cfg.get("auto") or {}
    if shot is not None and auto.get("enabled", True):
        rec = _auto_previz(sid, shot, g, cfg, stage)
        dp, spec = rec["depth"], rec["spec"]
        pv = {"control": dp.relative_to(_ROOT).as_posix(), "sha": rec["inputs_sha"][:16], "file_sha": AR.sha_file(dp),
              "shot_spec": AR.sha_file(spec) if spec.is_file() else None,
              "auto": {"stage": stage, "cached": rec["cached"], "seconds": rec["seconds"], "scale": rec["scale"], "dir": rec["dir"].relative_to(_ROOT).as_posix()}}
    else:
        base = _ROOT / (cfg.get("dir") or "data/previz") / f"{m.group(1)}-S{m.group(2)}"
        dp = base / "out" / f"S{m.group(2)}_depth.png"
        if not dp.is_file():
            return None
        spec = base / f"S{m.group(2)}.json"
        pv = {"control": dp.relative_to(_ROOT).as_posix(), "sha": AR.sha_file(dp), "shot_spec": AR.sha_file(spec) if spec.is_file() else None}
    for k, d in (("strength", 0.55), ("start_percent", 0.0), ("end_percent", 0.7)):
        pv[k] = float((cfg.get("control") or {}).get(k, d))
    return pv


def job_negative(*parts: str) -> str:
    """作业负面词：各段去重拼接（、分隔），硬负面（compiler.MINOR_GUARD）保证在。Turbo（cfg 1）采样时不送（workflows.negative_for），
    但作业 / take 记录里始终带着，定稿档（cfg>1）与改用 Base 的预设照样生效"""
    seen: list[str] = []
    for p in parts:
        for w in re.split(r"[、，,]", p or ""):
            w = w.strip()
            if w and w not in seen:
                seen.append(w)
    if C.MINOR_GUARD not in seen:
        seen.append(C.MINOR_GUARD)
    return "、".join(seen)


def face_ids(slots: list[dict]) -> list[str]:
    """送了身份脸的角色（按在画人物逐人）：face 槽的角色 + 脸板每一格的角色（ISS-1103-023528-1509：原来脸板只记第一格 → 三人镜只认一张脸）"""
    out: list[str] = []
    for s in slots:
        if s.get("kind") == "face":
            ids = [s.get("character")]
        elif s.get("kind") == "faceboard":
            ids = [m.get("character") for m in s.get("members") or [] if m.get("role") == "face"] or [s.get("character")]
        else:
            continue
        out += [i for i in ids if i and i not in out]
    return out


TURN_WORDS = ("转身", "回头", "转过脸", "抬起头", "抬头", "正面")


def last_face_plan(project: str, shot, last_prompt: str | None, plan) -> list[dict]:
    """K-REN-953（ep07-S3 尾帧 v14–v19 陌生女人）：首帧没送脸（cast 局部 / 局部特写），但尾帧的结束状态写到了脸（嘴唇 / 视线 /
    表情 / 转身）→ 尾帧必须送这个人的身份脸（asset_refs.face_of）。返回 [{character, file, why}]"""
    if not AR.pf_on("last_face_guard") or not last_prompt:
        return []
    have = set(face_ids([{"kind": s.kind, "character": s.character, "members": s.members} for s in plan.slots]))
    on = AR.cast_on_screen(shot)
    names = [n for n, _ in on]
    txt = last_prompt.replace("眼睛为真实人眼大小", "")
    out = []
    for name, _pr in on:
        cid = AR.NAME_ID.get(name, name)
        if cid in have:
            continue
        solo_face = len(names) == 1 and any(w in txt for w in AR.FACE_VIS_WORDS)
        if AR.face_described(name, txt, names) or solo_face or any(w in txt for w in TURN_WORDS):
            f = AR.face_of(project, cid)
            if f:
                out.append({"character": cid, "file": f, "why": "尾帧结束状态写到脸 / 转身（K-REN-953）"})
    return out


# ---------------------------------------------------------------- L-203 尾帧按落幅单独构图（compose）
LAST_CONTINUITY_LINE = "图{n}是本镜头首帧，只用来保持人物服装、发型、场景陈设与光线一致；构图、机位、景别按下面的结束画面重新构图。"
_CAM_WORDS = ("机位", "仰", "俯", "过肩", "越肩", "正面", "侧", "背", "平视", "眼平", "航拍", "鸟瞰", "跟拍", "锁定")
_LAST_DROP_EXTRAS = ("分拍", "表演节拍")        # 第一拍的视线 / 反应是起幅的，尾帧不用（eyeline_note 退到结束画面里的视线落点）
_END_RE = re.compile(r"落幅\s*[=＝]\s*([^｜|]+)")
_HEIGHT_RE = re.compile(r"平视[^、，（）]*|略低微仰|略高微俯|微仰|微俯|略仰|略俯|仰拍|俯拍|低机位[^、，（）]*|高机位[^、，（）]*|眼平|仰|俯")
_LABEL_PAREN_RE = re.compile(r"（[^）]*）|\([^)]*\)")


def end_framing(shot) -> str:
    """L-203：本镜落幅原文 = 分镜 **motion** 最后一个“——”之后的文字（比喻段跳过，同 compiler.end_state 的切法）；
    motion 没写“——”时退到 **运镜** 行的 落幅=…；都没有 → ""（调用方退回旧尾帧法）"""
    motion = str(AR.g(shot, "motion_prompt") or "")
    segs = C.split_dash(C.split_camera(motion)[1]) if motion else []
    while len(segs) > 1 and segs[-1].strip().startswith(C.SIMILE):
        segs.pop()
    if len(segs) > 1 and segs[-1].strip("，,。；; "):
        return segs[-1].strip("，,。；; ")
    m = _END_RE.search(PT2.script_field(shot, "运镜"))
    return m.group(1).strip("，,。；; ") if m else ""


def _cast_names(shot) -> list[str]:
    return [n for n in (AR.g(c, "name") for c in (AR.g(shot, "cast") or [])) if n]


def last_angle_field(shot, end_text: str) -> str:
    """尾帧的 **机位与景别**：景别 = 落幅第一个景别词；机位 = 落幅里写了机位 / 仰俯 / 过肩 / 正面侧面就用它，没写沿用首帧机位段
    （机位不变是默认，变了要分镜写明）；第三段 = **运镜** 的 落幅=… 文字（焦点 / 同框说明），没有沿用首帧第三段"""
    first = PT2.script_field(shot, "机位与景别")
    af = PV.split_field(first) if first else []
    size = AR.FR.size_of(end_text) or (af[0] if af else "")
    clauses = [c.strip() for c in re.split(r"[，,；;]", end_text) if c.strip()]
    names = _cast_names(shot)
    cam = [c for c in clauses[:3] if any(w in c for w in _CAM_WORDS) and not any(n in c for n in names) and c != size]
    camera = "、".join(cam) if cam else (af[1] if len(af) >= 2 else "")
    if cam and len(af) >= 2 and not _HEIGHT_RE.search(camera):       # 落幅只写了方位没写高度 → 高度沿用首帧（机位高度默认不变）
        hm = _HEIGHT_RE.search(_LABEL_PAREN_RE.sub("", af[1]))
        if hm:
            camera += "、" + hm.group(0)
    # FIX-PRE 11-06（REVIEW-PRE B / L-204 ①）：过肩镜的“越谁拍谁”写进尾帧机位段——落幅原文写明了就按落幅，没写明沿用首帧
    # （原来机位段去掉了带人名的小句，parse_angle 退到 cast 顺序猜，S4 尾帧猜成“越过祁念的肩拍卓琳”、被拍的主体被写成不露脸）
    if names and PV.parse_angle(camera, names)["azimuth"] == "ots" and PV.parse_angle(camera, names).get("ots_by") != "text":
        a_end = PV.parse_angle(end_text, names)
        a_first = PV.parse_angle("｜".join(af[1:3]), names) if len(af) >= 2 else {}
        pair = next(((a["ots_from"], a["ots_to"]) for a in (a_end, a_first) if a.get("ots_by") == "text" and a["ots_from"] and a["ots_to"]), None)
        if pair:
            camera += f"、越过{pair[0]}的肩拍{pair[1]}"
    m = _END_RE.search(PT2.script_field(shot, "运镜"))
    # W-PASS 11-07：沿用首帧第三段时只取其中的落幅（“起幅：…；落幅：…”写法；首帧那边只取起幅，PV.start_end）
    tail = (m.group(1).strip() if m else "") or ((PV.start_end(af[2])[1] or af[2]) if len(af) >= 3 else "")
    return "｜".join(x for x in (size, camera, tail) if x)


def compose_last_shot(shot, end_text: str):
    """L-203：把本镜改写成“落幅镜”——img_prompt = 落幅原文（触发词沿用首帧），**机位与景别** 按落幅改，**构图** 换成 运镜 的 落幅=…
    （没有就去掉），分拍 / 表演节拍去掉；人物表、场景、灯光、镜头、光学、造型沿用（身份 / 服装 / 场景 / 光线一致由参考图与这些字段保证）"""
    from ns_schema.script import Extra
    trig = list(AR.g(shot, "img_triggers") or [])
    img = ("，".join(trig) + "，" if trig else "") + end_text
    m = _END_RE.search(PT2.script_field(shot, "运镜"))
    comp = ("落幅=" + m.group(1).strip()) if m else ""
    angle = Extra(label="机位与景别", text=last_angle_field(shot, end_text))
    new_extras, seen_angle = [], False
    for e in AR.g(shot, "extras") or []:
        lab = _LABEL_PAREN_RE.sub("", str(AR.g(e, "label") or "")).strip("*：: ")
        if lab in _LAST_DROP_EXTRAS:
            continue
        if lab == "机位与景别":
            new_extras.append(angle)
            seen_angle = True
        elif lab == "构图":
            if comp:
                new_extras.append(Extra(label="构图", text=comp))
        else:
            new_extras.append(Extra(**e) if isinstance(e, dict) else e)
    if not seen_angle:
        new_extras.append(angle)
    upd = {"img_prompt": img, "motion_prompt": end_text, "motion_triggers": [], "extras": new_extras}
    if isinstance(shot, dict):
        return {**shot, **upd, "extras": [x.model_dump() for x in new_extras]}
    return shot.model_copy(update=upd)


TIGHTER_NOTE = "（比首帧明显更紧：人物在画面里更大、画面边缘裁掉首帧里的环境，构图与首帧不同）"


def tighten_size(size: str | None, level: int) -> str | None:
    """景别词紧 level 档（framing.SIZES 从紧到松排：极近特写 … 大远景）；没写景别 → None"""
    if not size or size not in AR.FR.SIZES or level <= 0:
        return size
    i = AR.FR.SIZES.index(size)
    return AR.FR.SIZES[max(0, i - level)]


def tighten_last_shot(shot2, end_text: str, level: int):
    """W-DETECT 11-06：落幅镜再紧 level 档——**机位与景别** 第一段换成更紧的景别词、img_prompt 里的景别词同步替换并加 TIGHTER_NOTE。
    → (shot2', end_text')；落幅没写景别词时只加说明句"""
    from ns_schema.script import Extra
    size = AR.FR.size_of(end_text)
    new = tighten_size(size, level)
    txt = end_text
    if size and new and new != size:
        txt = end_text.replace(size, new, 1)
    txt = txt.rstrip("，,。；; ") + TIGHTER_NOTE
    extras = []
    for e in AR.g(shot2, "extras") or []:
        lab = _LABEL_PAREN_RE.sub("", str(AR.g(e, "label") or "")).strip("*：: ")
        if lab == "机位与景别":
            parts = PV.split_field(str(AR.g(e, "text") or ""))
            if parts and size and new and parts[0].strip() == size:
                parts[0] = new
            elif parts and new and new != parts[0].strip():
                parts[0] = tighten_size(parts[0].strip(), level) or parts[0]
            extras.append(Extra(label="机位与景别", text="｜".join(parts)))
        else:
            extras.append(Extra(**e) if isinstance(e, dict) else e)
    img = str(AR.g(shot2, "img_prompt") or "")
    if size and new and new != size:
        img = img.replace(size, new, 1)
    img = img.rstrip("，,。；; ") + TIGHTER_NOTE
    upd = {"img_prompt": img, "motion_prompt": txt, "extras": extras}
    if isinstance(shot2, dict):
        return {**shot2, **upd, "extras": [x.model_dump() for x in extras]}, txt
    return shot2.model_copy(update=upd), txt


def build_last_compose(project: str, ep, shot, g, first_job: dict, first_file: str, *, bible=None,
                       prompt_override: str | None = None, plan_kw: dict | None = None) -> dict:
    """L-203 尾帧（compose）：落幅当一张新图编——提示词 = 首帧同一套模板（景别 / 机位 / 构图 / 人物 / 道具 / 光线 + 风格 / 镜头 /
    纠正器 / 材质词），参考 = 身份脸 / 造型 / 场景底图照首帧槽位逻辑（留一槽），通过契约的首帧挂在最后一张、只保持服装 / 发型 /
    陈设 / 光线一致（槽满时顶掉最后一个非脸槽，见 continuity_slot）。放最后 + 弱指令的理由：Qwen-Image 多图编辑里 图1 是“被改的底图”，
    首帧放图1 + “保持不变”正是 L-203 点名的复制问题（11-06 校准：旧法 111 对里 51% 构图相关 ≥ 0.80）；身份脸 / 造型放前面保住 Q1。
    落幅写不出（motion 没有“——”、运镜没有 落幅=）→ 退回旧法（job.last_frame_mode = edit_fallback）"""
    end_text = end_framing(shot)
    if not end_text:
        job = _build_last_edit(first_job, first_file, prompt_override=prompt_override)
        job["last_frame_mode"], job["last_frame_mode_why"] = "edit_fallback", "分镜没写落幅（motion 无“——”、运镜无 落幅=）"
        return job
    kw = dict(plan_kw or {})
    fl_level = int(kw.pop("fl_escalate", 0) or 0)
    model = first_job.get("model") or "qwen_image_2.1"
    shot2 = compose_last_shot(shot, end_text)                          # 参考槽与首帧同一套逻辑；槽满时首帧顶掉最后一个非脸槽（continuity_slot）
    cont = (first_file, LAST_CONTINUITY_LINE)
    if fl_level:
        # W-DETECT 11-06（L-203 换构图阶梯）：上一版尾帧与首帧太像（FL）→ 落幅景别紧 fl_level 档、不再把首帧挂进参考（首帧一进参考，
        # 编辑模型就照抄构图：ep01 S9 25 版 / S4 12 版 / S7 5 版 全部 0.82–0.99），服装 / 场景一致改靠设定图 + 场景底图 + 文字
        shot2, end_text = tighten_last_shot(shot2, end_text, fl_level)
        cont = None
    job = build_first(project, ep, shot2, g, bible=bible, model=model, prompt_override=prompt_override,
                      continuity_ref=cont, frame_stage="last", **kw)
    job.update({"id": first_job["id"] + ":last", "stage": "last", "last_prompt": first_job.get("last_prompt"),
                "last_frame_mode": "compose", "end_framing": end_text, "first_frame_file": first_file,
                "last_faces": first_job.get("last_faces") or [], "first_prompt_sha": _sha16(first_job.get("prompt") or "")})
    if fl_level:
        job["fl_escalate"] = {"level": fl_level, "continuity_ref": False, "why": "上一版尾帧与首帧太像（FL），落幅紧档、去首帧参考"}
    # L-206 ②：尾帧档跟首帧同一张按集表（first_frame_tier(episode)）；turbo_previz 时 build_first 已按落幅改写后的字段（shot2：景别 / 机位 /
    # 构图）算了尾帧自己的摆位图（stage=last，与首帧的起幅摆位图不同一张），算不出 → 裸 turbo（previz_missing）。首帧的起幅深度图从不给尾帧用
    if job.get("previz") and job["previz"].get("auto", {}).get("stage") != "last":
        job.pop("previz")
        tier, ov = first_frame_tier(project, force=previz_fallback_tier())
        job["tier"], job["tier_overrides"], job["previz_missing"] = tier, ov, "L-203：尾帧按落幅重新构图，不用首帧（起幅）摆位图"
    job["compile_trace"] = trace_of(job)
    return job


def build_last(first_job: dict, first_file: str, *, prompt_override: str | None = None, project: str | None = None,
               ep=None, shot=None, g=None, bible=None, mode: str | None = None, plan_kw: dict | None = None) -> dict:
    """尾帧作业。方式按 run_style.json runs.<剧>.last_frame_mode（ns_agent.video_mode.last_frame_mode）：
    edit（默认）= 旧法：通过契约的首帧作图1、项目方挑定的身份脸作图2；提示词 = bible last_frame_keep + 编译器 last_prompt；
    compose（L-203）= 落幅单独构图（build_last_compose），需要 project / ep / shot / g；缺了退回旧法并记 edit_fallback。
    两种都在作业里记 first_frame_file（尾帧 postflight 的 FL 首尾帧比对读它）"""
    from ns_agent import video_mode as VM
    proj = project or first_job.get("framing_project")
    m = mode or VM.last_frame_mode(proj)
    if m == "compose" and ep is not None and shot is not None and g is not None:
        return build_last_compose(proj or "", ep, shot, g, first_job, first_file, bible=bible,
                                  prompt_override=prompt_override, plan_kw=plan_kw)
    job = _build_last_edit(first_job, first_file, prompt_override=prompt_override)
    if m == "compose":
        job["last_frame_mode"], job["last_frame_mode_why"] = "edit_fallback", "调用方没给 ep / shot / g（旧入口）"
    return job


def _build_last_edit(first_job: dict, first_file: str, *, prompt_override: str | None = None) -> dict:
    """尾帧（旧法 edit）：通过契约的首帧作图1、项目方挑定的身份脸作图2；提示词 = 编译器 last_prompt（与 ASSETMASTER frames_v2 同法）。
    K-REN-953：首帧没送脸、尾帧结束状态写到脸 → 补送该人身份脸（first_job.last_faces）"""
    faces = list(first_job.get("face_files") or [])[:1]
    lines = ""
    if faces:
        lead = next((s.get("character") for s in (first_job["asset_refs"].get("slots") or []) if s.get("kind") == "face"), "主角")
        lines = f"图2是{AR.ID_NAME.get(lead, lead)}的脸部参考（只取脸和发型，必须一致）。"
    elif first_job.get("last_faces"):
        lf = first_job["last_faces"][0]
        faces = [lf["file"]]
        lines = f"图2是{AR.ID_NAME.get(lf['character'], lf['character'])}的脸部参考（只取脸和发型，必须一致）。"
    job = {**first_job, "id": first_job["id"] + ":last", "stage": "last",
           "prompt": lines + (prompt_override or first_job.get("last_prompt") or ""), "refs": [first_file] + faces,
           "first_frame_file": first_file, "last_frame_mode": "edit"}
    job["compile_trace"] = trace_of(job)
    return job


# =====================================================================================================
# IMGMASTER 分段编译（L-118，docs/imgmaster/DESIGN.md；开关 config/compile/imgmaster.yaml imgmaster.enabled，默认关）
# 固定五段：[景别·镜头] [场景连续性块] [人物块] [本镜动作] [约束句]，前面是“参考标注头”（槽位分配函数产出，不是一段）。
# 其它规则只能登记成插件（config/compile/plugins.yaml）改本段；插件签名 (segment_text, shot, continuity) -> segment_text。
# P1 只提供只读试编（compile_frame_v2），build_first / build_last 在开关关闭时一字不改。
# =====================================================================================================
import hashlib as _hl
import json as _json
import re as _re
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parents[3]
SEGMENTS = ("framing", "continuity", "characters", "action", "constraints")
_ENV_WORDS = _re.compile(r"光|灯|窗|墙|背景|虚化|霓虹|光斑|夜色|天色|地板|地面|走廊|楼道|街")
_SIDE_OBJ = ("台灯", "走廊", "窗", "门", "电梯", "屏幕")


class CompileError(RuntimeError):
    """分段编译失败关闭：未登记插件 / 段名不对 / 规则不存在或没有 check / continuity 缺失"""


def _sha(s) -> str:
    return _hl.sha256((s if isinstance(s, bytes) else str(s).encode("utf-8"))).hexdigest()[:12]


def imgmaster_cfg() -> dict:
    import yaml
    p = _ROOT / "config/compile/imgmaster.yaml"
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("imgmaster", {}) if p.is_file() else {}


def imgmaster_enabled() -> bool:
    return bool(imgmaster_cfg().get("enabled", False))


def _rules_with_check() -> dict:
    import yaml
    out = {}
    for f in sorted((_ROOT / "knowledge/rules").glob("*.yaml")):
        d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        for r in (d.get("rules") if isinstance(d, dict) else d) or []:
            if isinstance(r, dict) and r.get("id"):
                out[r["id"]] = {"check": r.get("check"), "file": f.relative_to(_ROOT).as_posix()}
    return out


PLUGIN_IMPL: dict = {}


def plugin(pid: str):
    """登记插件实现；必须在 plugins.yaml 有 enabled 条目（load_plugins 校验）"""
    def deco(fn):
        PLUGIN_IMPL[pid] = fn
        return fn
    return deco


def load_plugins(path=None, rules: dict | None = None, impl: dict | None = None) -> list[dict]:
    """读登记表并校验（失败关闭）；→ 按 (段顺序, order) 排好的条目（含 enabled=false 的，调用方自己筛）"""
    import yaml
    p = _Path(path) if path else _ROOT / "config/compile/plugins.yaml"
    rows = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("plugins") or []
    rules = _rules_with_check() if rules is None else rules
    impl = PLUGIN_IMPL if impl is None else impl
    seen = set()
    for r in rows:
        pid = r.get("id")
        if not pid or pid in seen:
            raise CompileError(f"插件 id 缺失或重复：{pid}")
        seen.add(pid)
        if r.get("segment") not in SEGMENTS:
            raise CompileError(f"插件 {pid} 的 segment={r.get('segment')} 不是五段之一 {SEGMENTS}")
        if not r.get("enabled"):
            continue
        rid = r.get("rule_id")
        if not rid or rid not in rules:
            raise CompileError(f"插件 {pid} 的 rule_id={rid} 在 knowledge/rules 里不存在")
        if not rules[rid]["check"]:
            raise CompileError(f"插件 {pid} 的规则 {rid} 没有 check（G1：每个判断有 decider 和 check）")
        if pid not in impl:
            raise CompileError(f"插件 {pid} 已登记但代码里没有 @plugin 实现")
    on = {r["id"] for r in rows if r.get("enabled")}
    for pid in impl:
        if pid not in on:
            raise CompileError(f"代码里有插件 {pid} 但 plugins.yaml 没登记为 enabled（未登记插件不许跑）")
    return sorted(rows, key=lambda r: (SEGMENTS.index(r["segment"]), r.get("order", 0)))


# ---- continuity（HOLLYWOOD 导出，schema nsfs.scene_locks/1）
def load_continuity(path) -> dict:
    p = _Path(path)
    if not p.is_file():
        raise CompileError(f"continuity 文件不存在：{path}（失败关闭，不按“没有连续性块”出图）")
    raw = p.read_bytes()
    d = _json.loads(raw.decode("utf-8"))
    if d.get("schema") != "nsfs.scene_locks/1":
        raise CompileError(f"continuity schema 不对：{d.get('schema')}")
    d["_sha"] = _hl.sha256(raw).hexdigest()
    d["_file"] = str(path)
    return d


def scene_lock(cont: dict, scene_id: str) -> dict:
    sc = (cont.get("scenes") or {}).get(scene_id)
    if not sc:
        raise CompileError(f"continuity 里没有场景 {scene_id}")
    miss = [k for k in ("fixtures", "key_light", "blocking", "screen_direction") if not sc.get(k)]
    if miss:
        raise CompileError(f"场景 {scene_id} 定稿缺 {miss}")
    return sc


def continuity_sentence(sc: dict) -> str:
    """整场固定句式（同一场所有镜逐字相同）：主光 / 站位 / 屏幕方向"""
    return f"主光：{sc['key_light']}；站位与屏幕方向：{sc['screen_direction']}"


def _add_note(text: str, note: str | None) -> str:
    return text if not note else (text.rstrip("，。 ") + "，" if text else "") + note.rstrip("。")


_ACTION_WORDS = _re.compile(r"视线|目光|眼神|看|望|盯|朝|转|落在|张开|搭|扣|放|抖|握|拿|走向|走去|走到|走进|背对|坐在|站在|伸|按|敲|停|靠|抬|低头|垂")
_LIGHT_RE = _re.compile(r"台灯|屏幕.{0,3}光|霓虹|逆光|侧光|光斑")      # continuity 主光句涵盖的光源词；其它光源（例：安全出口绿光）不算重复


def _bigrams(t: str) -> set:
    t = _re.sub(r"[\s，。；、：|｜（）()“”\"*]", "", t or "")
    return {t[i:i + 2] for i in range(len(t) - 1)}


@plugin("cont_scene_block")
def _p_cont_scene_block(text, shot, cont):
    """场景表字段（去掉“光线：”，光由 continuity 主光统一给）+ 固定句 → 整场相同的【场景】块"""
    items = [x for x in text.replace("【场景】", "", 1).split("；") if x and not x.startswith(("光线：", "本镜场景："))]
    return "【场景】" + "；".join(items + [continuity_sentence(cont["lock"])])


@plugin("env_dedupe")
def _p_env_dedupe(text, shot, cont):
    """K-WRT-H25 的编译侧：正文里不带人名、不是人物动作 / 视线、且和 continuity（主光 + 站位 + 场景表）重复的环境 / 灯光分句删掉，
    环境只由 continuity 说一次；continuity 没说到的环境细节（例：安全出口绿光）保留"""
    names = cont.get("names") or []
    ref = _bigrams(continuity_sentence(cont["lock"]) + cont.get("scene_base", ""))
    keep = []
    for cl in text.split("，"):
        bc = _bigrams(cl)
        if (cl and _ENV_WORDS.search(cl) and not any(n in cl for n in names) and not _ACTION_WORDS.search(cl)
                and bc and (len(bc & ref) / len(bc) >= 0.5 or _LIGHT_RE.search(cl))):
            cont["dropped"].append({"plugin": "env_dedupe", "clause": cl})
            continue
        keep.append(cl)
    return "，".join(keep)


def _sides(txt: str) -> dict:
    """{物件: {轴: 值}}，轴 = lateral（左 / 右）| depth（后方 / 前方）；只在同一物件同一轴上比较"""
    out = {}
    for o in _SIDE_OBJ:
        for m in _re.finditer(r"(?:画面|在)(左|右)侧?的?" + _re.escape(o), txt or ""):          # “画面左侧走廊方向…”
            out.setdefault(o, {})["lateral"] = m.group(1)
        for m in _re.finditer(_re.escape(o) + r"(?:方向|暗处|尽头)?(?:在|位于|从|朝)?(?:画面)?([^，；。]{0,6})", txt or ""):
            seg = m.group(1)
            lat = _re.search(r"(左|右)", seg)
            dep = _re.search(r"(后方|身后|前方|后)", seg)
            if lat:
                out.setdefault(o, {})["lateral"] = lat.group(1)
            if dep:
                out.setdefault(o, {})["depth"] = "后" if dep.group(1) in ("后方", "身后", "后") else "前"
    return out


def _side_conflicts(detail: str, ref: dict) -> dict:
    got = _sides(detail)
    return {o: {ax: (v, ref[o][ax]) for ax, v in axes.items() if ax in ref.get(o, {}) and ref[o][ax] != v}
            for o, axes in got.items() if o in ref and any(ax in ref[o] and ref[o][ax] != v for ax, v in axes.items())}


@plugin("scene_detail_guard")
def _p_scene_detail_guard(text, shot, cont):
    """“本镜场景：…”里某物件的左右 / 前后和 continuity（主光 + 屏幕方向）不一致 → 删这个分句并记 trace"""
    ref = _sides(cont["lock"]["key_light"] + "。" + cont["lock"]["screen_direction"])
    keep = []
    for cl in _re.split(r"(?<=[，；])", text):
        c = cl.strip("，；")
        bad = _side_conflicts(c, ref)
        if bad and not _ACTION_WORDS.search(c):
            cont["dropped"].append({"plugin": "scene_detail_guard", "clause": c, "conflict": bad})
            continue
        if bad:
            cont.setdefault("warn", []).append({"plugin": "scene_detail_guard", "clause": c, "conflict": bad, "why": "含动作 / 视线，不自动删，需写作侧改"})
        keep.append(cl)
    return "".join(keep)


@plugin("outer_items")
def _p_outer_items(text, shot, cont):
    return _add_note(text, cont.get("outer_note"))


@plugin("eyeline")
def _p_eyeline(text, shot, cont):
    return _add_note(text, cont.get("eyeline_note"))


@plugin("garment_detail")
def _p_garment_detail(text, shot, cont):
    return _add_note(text, cont.get("garment_note"))


@plugin("text_fallback")
def _p_text_fallback(text, shot, cont):
    return _add_note(text, cont.get("fallback_note"))


def _split_old(c, shot, g) -> dict:
    """把现行编译输出（c.image_prompt，不含参考标注头和补句）按锚点拆回五段的原料"""
    from ns_agent.gen_contract import style as ST
    full = c.image_prompt
    scene = C.scene_segment(shot, g)
    i = full.find(scene) if scene else -1
    pre, post = (full[:i], full[i + len(scene):]) if i >= 0 else (full, "")
    j = pre.find("【")
    framing, rest = (pre[:j], pre[j:]) if j >= 0 else (pre, "")
    body_set = {x.strip() for x in (c.image_body or "").split("，") if x.strip()}
    frag_set = {x.strip() for f in (c.look_fragments or []) for x in f.split("，")}
    clauses = rest.split("，")
    # 人物块 / 动作段的分界 = 最后一个角色锁（“【名】”开头的分句）之后、剧本 img 原文第一个分句（景别词）出现处
    first_body = ((c.image_body or "").split("，")[0]).strip()
    start = max([n for n, x in enumerate(clauses) if x.strip().startswith("【")] + [0])
    k = next((n for n in range(start, len(clauses)) if first_body and clauses[n].strip() == first_body), None)
    if k is None:
        k = next((n for n in range(start, len(clauses)) if clauses[n].strip() in body_set and clauses[n].strip() not in frag_set), len(clauses))
    base, detail = scene.rsplit("；本镜场景：", 1) if "；本镜场景：" in scene else (scene, "")
    return {"framing": framing.strip("，"), "scene_base": base, "scene_detail": detail,
            "characters": "，".join(clauses[:k]).strip("，"), "action": "，".join(clauses[k:]).strip("，"),
            "constraints": post.strip("，"), "suffix": ST.image_suffix()}


def sid_of(ep, shot) -> str:
    return f"ep{int(AR.g(ep, 'number')):02d}-S{AR.g(shot, 'index')}"


def compile_frame_v2(ep, shot, g, project: str, cont: dict, *, bible=None, available=None, model: str = "qwen_image_2.1",
                     scene_looks=None, plugins_path=None, **plan_kw) -> dict:
    """分段编译（只读，不写任何文件）。→ {prompt, preamble, segments, trace, plan, sr, ctx}
    """
    from ns_agent.bible import load_bible
    from ns_agent.gen_contract import style as ST
    shot = freeze_archived_extras(project, shot)          # W7：归档剧编译冻结
    rows = load_plugins(plugins_path)
    g2, _removed = AR.locks_without_wardrobe(g)
    c = C.compile_shot(ep, retrigger(shot, g2), g2, set(available or ()), image_style=ST.image_suffix(),
                       bible=bible or load_bible(), framing_project=project)
    kept = plan_kw.get("kept_triggers") or [t for t in (AR.g(shot, "img_triggers") or []) if t not in (c.dropped_triggers or [])]
    sr = AR.resolve_shot(project, ep, shot, kept_triggers=kept, two_person=plan_kw.get("two_person"))
    plan = AR.plan_slots_v2(sr, model, plan_kw.get("capacity"), scene_looks)
    AR._apply_single_turn(plan, AR.single_turn_line(sr, shot))
    raw = _split_old(c, shot, g2)
    lock = scene_lock(cont, AR.g(shot, "scene_id"))
    names = [x.get("name") for x in sr.characters or [] if x.get("name")] + [AR.g(x, "name") for x in AR.g(shot, "cast") or []]
    ctx = {"lock": lock, "scene_base": raw["scene_base"], "names": [n for n in dict.fromkeys(names) if n], "dropped": [],
           "outer_note": outer_items_note(project, ep, shot, sr, c.image_prompt), "fallback_note": plan.prompt_note,
           "eyeline_note": eyeline_note(shot, sr, c.image_prompt), "garment_note": garment_detail_note(project, ep, shot, sr, c.image_prompt)}
    seg = {"framing": raw["framing"], "continuity": raw["scene_base"], "characters": raw["characters"],
           "action": raw["action"] + (("，本镜场景：" + raw["scene_detail"]) if raw["scene_detail"] else ""),
           "constraints": raw["constraints"]}
    suf = raw["suffix"]                       # S★ 风格后缀固定放全文最后（L-087 不动），不进任何插件的视野
    if suf and seg["constraints"].endswith(suf):
        seg["constraints"] = seg["constraints"][:-len(suf)].rstrip("，, ")
    sources = {k: [{"rule": "compiler.compile_shot", "file": "services/agent/ns_agent/compiler.py", "kind": "base"}] for k in SEGMENTS}
    sources["framing"][0]["rule_id"] = "K-REN-101"          # 景别首句原样透传（含 K-REN-123/902/903/705）
    applied = []
    for r in rows:
        if not r.get("enabled"):
            continue
        before = seg[r["segment"]]
        seg[r["segment"]] = PLUGIN_IMPL[r["id"]](before, shot, ctx)
        if seg[r["segment"]] != before:
            applied.append(r["id"])
            sources[r["segment"]].append({"plugin": r["id"], "rule_id": r["rule_id"], "order": r.get("order")})
    seg["constraints"] = "，".join(x for x in (seg["constraints"], suf) if x)
    prompt = plan.prompt_prefix + "，".join(seg[k].strip("，") for k in SEGMENTS if seg[k].strip("，"))
    pf = _Path(plugins_path) if plugins_path else _ROOT / "config/compile/plugins.yaml"
    trace = {"schema": "nsfs.compile_trace/1", "shot": sid_of(ep, shot), "model": model,
             "continuity": {"file": cont.get("_file"), "sha256": cont.get("_sha"), "scene": AR.g(shot, "scene_id")},
             "plugins_sha": _sha(pf.read_bytes()), "compiler_inputs_sha": c.inputs_sha, "bible_version": c.bible_version,
             "segments": [{"name": k, "sha": _sha(seg[k]), "chars": len(seg[k]), "sources": sources[k]} for k in SEGMENTS],
             "preamble": {"sha": _sha(plan.prompt_prefix),
                          "slots": [{"index": s.index, "kind": s.kind, "file": s.file, "character": s.character} for s in plan.slots],
                          "dropped": plan.dropped, "rule": "asset_refs.plan_slots_v2（身份脸 > 场景 > 本场造型 > 其它）"},
             "look_refs_via_text": [{"character": d.get("character"), "file": d["file"], "why": "造型参考因槽满走文字（总调度 11-02：双人镜不合成脸板，L-086 保真优先）"}
                                    for d in plan.dropped if d.get("role") != "scene"],
             "dropped_clauses": ctx["dropped"], "warnings": ctx.get("warn", []), "applied_plugins": applied, "prompt_sha": _sha(prompt)}
    return {"prompt": prompt, "preamble": plan.prompt_prefix, "segments": seg, "trace": trace, "plan": plan, "sr": sr, "ctx": ctx}


def write_compile_trace(trace: dict, out_dir) -> dict:
    """随 take 落账：trace 写成 <out_dir>/<shot>_<prompt_sha>.json，返回 {file, sha}（take 记录的 compile_trace 字段）。
    P1 不被任何出图路径调用；开关打开后由 build_first 返回作业时附带"""
    raw = _json.dumps(trace, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")
    d = _Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{trace['shot']}_{trace['prompt_sha']}.json"
    f.write_bytes(raw)
    return {"file": str(f), "sha": _hl.sha256(raw).hexdigest()}
