"""Preflight（提交前，确定性代码）：docs/GEN_CONTRACT.md §1 I1–I10。任何一项 fail → 拒绝提交。

请求格式（各提交入口统一组装；image_jobs.contract_request(job) 已实现首帧作业的组装）：
    {
      "kind": "image" | "video",
      "model": "qwen_image_2.1" | "qwen_image_edit_2511" | "flux2_klein" | <视频配方名>,
      "project": "series10_v1", "shot_id": "ep01-S3",
      "cast": [{"name", "presence": 在画|局部|画外, "x", "depth", "pose"}],      # I1
      "prompt": "<送进模型的完整提示词>",
      "refs": ["<相对项目根>", ...],                                           # 实际进模型的参考图，按槽位顺序
      "asset_refs": ns_agent.asset_refs.record(...)，                          # I2–I5、I9
      "loras": [[file, strength], ...],
      "compiled_by": "...ns_agent.compiler...",                               # I6
      "first_frame_postflight": {...}                                         # 视频：首帧 postflight 结果（ok）
    }
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from . import ROOT, ContractResult, ContractViolation, Item, cfg
from . import style as ST

TITLES = {
    "I1": "在画角色清单 + 朝向 / 站位", "I2": "身份脸部参考", "I3": "本镜造型 id + 造型设定图",
    "I4": "景别可见的单品资产图", "I5": "场景参考底图", "I6": "提示词 = 系统编译器输出（含景别首句、参考图逐一标注）",
    "I7": "系统后缀 = 默认出图预设，无旧柔焦 / 奶油 / 浓郁饱和 / 油光", "I8": "LoRA 白名单（禁已证伪）",
    "I9": "模型槽位规划（优先级、参考板、双人脸不同槽）", "I10": "成年人与锁定外貌（HC-07）", "I11": "资产包清单（V2：参考只认 assets_v2/manifest.json，清单外 / sha 不符就挡）",
    "I12": "本地专用资产（V2：route=local_only 的造型 / 单品只许项目方自有机器的道，L-128 / K-REN-958）",
    "I13": "品牌字样（V2：带 logo 的参考只取款式，提示词不许出现品牌字样，K-REN-959）",
}
REF_LINE = re.compile(r"图(\d+)是")
IMG_TAG = re.compile(r"<image(\d+)>是")


def _hc():
    p = str(ROOT / "tools" / "pipeline")
    if p not in sys.path:
        sys.path.insert(0, p)
    import hard_constraints as HC
    return HC


def _relpath(p) -> str:
    from pathlib import Path
    q = Path(str(p))
    try:
        return (q if q.is_absolute() else ROOT / q).resolve().relative_to(ROOT.resolve()).as_posix()
    except (ValueError, OSError):
        return str(p).replace("\\", "/")


def registered_sheet_files(project: str | None) -> set[str]:
    """已选定的造型设定图：项目 sheets_file（sheets_chosen.json）里登记的 file"""
    import json
    from ns_agent import asset_refs as AR
    out: set[str] = set()
    projs = [project] if project else list((AR._cfg().get("projects") or {}))
    for pj in projs:
        try:
            d = json.loads((ROOT / AR.project_cfg(pj).get("sheets_file", "")).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        out.update(_relpath(e["file"]) for e in d.values() if isinstance(e, dict) and e.get("file"))
    return out


def look_file_registered(slot: dict, chosen: set[str]) -> bool:
    """造型槽文件 = 已选定设定图本身，或它在 crop_dir 下的系统裁切（asset_refs.sheet_crop 命名：<设定图名>_<栏>[_区域].png）"""
    from ns_agent import asset_refs as AR
    p = _relpath(slot.get("path") or "")
    if p in chosen:
        return True
    sheet = _relpath(slot.get("sheet") or "")
    if sheet not in chosen:
        return False
    crop_dirs = {_relpath(AR.project_cfg(pj).get("crop_dir", "data/refcrops")) for pj in (AR._cfg().get("projects") or {})}
    pp = Path(p)
    return pp.parent.as_posix() in crop_dirs and pp.stem.startswith(Path(sheet).stem + "_") and (ROOT / p).is_file()


def registered_identity_files() -> set[str]:
    """已登记身份图：data/assets/identity_refs/manifest.json 里的文件 + 资产参考配置的身份脸 + 资产中心身份脸覆盖"""
    import json
    out: set[str] = set()
    try:
        m = json.loads((ROOT / "data/assets/identity_refs/manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        m = {}

    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, str) and x.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            out.add(_relpath(x if "/" in x or "\\" in x else f"data/assets/identity_refs/{x}"))
    walk(m)
    from ns_agent import asset_refs as AR
    for pc in (AR._cfg().get("projects") or {}).values():
        for fs in (pc.get("faces") or {}).values():
            out.update(_relpath(f) for f in fs)
    try:
        ov = json.loads(AR.FACE_OVERRIDES.read_text(encoding="utf-8"))
        walk(ov)
    except (OSError, ValueError):
        pass
    return out


def _proj_or_primary(proj: str | None) -> str:
    """本作业的项目；不给才按当前剧（统一调 runs.primary_run，FACTORY 19:1x：不直接读 runs.json primary——V1 作业不能按 V2 判）"""
    if proj:
        return proj
    from ns_agent import run_paths as _RPA     # OVERSIGHT-F P2：runs 加载器只此一处
    return _RPA.runs_module(ROOT).current_run(ROOT)


def stale_face_slots(slots: list[dict], proj: str | None = None) -> list[str]:
    """AUDIT-M A-01 / K-OPS-E29（总调度 11-02 17:1x）：身份脸只认 asset_refs.face_of 那一张——face 槽和脸板里标了 face 的格，
    文件不等于该角色当前身份脸的列出来（旧脸、旧 plates、配方里写死的旧裁切都算）。项目 id 只从运行登记表读（hard_run），读不到抛异常"""
    from ns_agent import asset_refs as AR
    proj = _proj_or_primary(proj)
    cur: dict[str, str | None] = {}
    bad = []
    for s in slots:
        items = [(s.get("character"), s.get("file") or s.get("path"))] if s.get("kind") == "face" else \
            [(m.get("character"), m.get("file") or m.get("path")) for m in s.get("members") or [] if m.get("role") == "face"] \
            if s.get("kind") == "faceboard" else []
        for cid, f in items:
            if not cid or not f:
                continue
            if cid not in cur:
                cur[cid] = AR.face_of(proj, cid)
            if cur[cid] and _relpath(f) != _relpath(cur[cid]):
                bad.append(f"{cid} 用的是 {_relpath(f)}，当前身份脸是 {_relpath(cur[cid])}")
            elif not cur[cid] and AR.manifest_rel(proj):
                # L-126：资产包模式（V2）清单里没有这个人的身份脸 → 槽里不管放什么都不许用（多半是 V1 的脸），失败关闭
                bad.append(f"{cid} 在本剧资产清单里没有身份脸，不许用 {_relpath(f)}（L-126，不退回旧脸）")
    return bad


def manifest_missing_faces(faced: list, proj: str | None = None) -> list[str]:
    """L-126（总调度 11-02 18:5x）：资产包模式的剧，每个露脸的人都必须在清单里有身份脸；没有 → 列出来（preflight I2 挡住出图）。
    proj = 本作业的项目（framing_project）；不给才按当前剧（runs.primary_run，FACTORY 19:1x：不直接读 runs.json primary，否则 V1 作业被按 V2 判）"""
    from ns_agent import asset_refs as AR
    if not proj:
        proj = _proj_or_primary(None)
    if not AR.manifest_rel(proj):
        return []
    return [c for c in faced if c and not AR.face_of(proj, c)]


def sheet_gaps(subject, zones, characters, warnings) -> list[dict]:
    """I3 设定图口径的唯一实现（preflight I3 与 tools/master/episode_ready 共用，ISS-1103-030746-664a）：
    露脸（在画、不是背影、不是局部只送部位单品）且景别里看得到服装的人，造型只有清单单品图（asset_refs 告警“造型参考用清单单品图”）
    = 缺设定图。→ [{character, look, kind, detail}]，detail 统一写“<名> 造型<id> 没有设定图”（asset_jobs 按它排 sheet 作业）。
    config/gen_contract.json I3_require_sheet=false 时不判（回退旧口径）"""
    from . import cfg as _cfg
    if not _cfg().get("I3_require_sheet", True):
        return []
    if subject in ("part", "object") or not (set(zones or []) - {"face"}):
        return []
    out = []
    for w in warnings or []:
        if w.get("kind") != "造型参考用清单单品图":
            continue
        ch = next((c for c in characters or [] if c.get("name") == w.get("character")), None)
        if not ch or ch.get("presence", "在画") != "在画" or ch.get("back_view") or ch.get("wardrobe_scope") is not None:
            continue
        out.append({"character": ch.get("name"), "cid": ch.get("character"), "look": ch.get("look"), "kind": "资产缺图",
                    "detail": f"{ch.get('name')} 造型{ch.get('look')} 没有设定图"})
    return out


def _faceboard_ok(s: dict) -> bool:
    """脸板合规：每格都是 role=face 且标了角色 id，同一角色不重复（ISS-1103-023528-1509）"""
    ms = s.get("members") or []
    ids = [m.get("character") for m in ms]
    return bool(ms) and all(m.get("role") == "face" and m.get("character") for m in ms) and len(set(ids)) == len(ids)


def _kinds(rec: dict, *kinds) -> list[dict]:
    return [m for m in (rec.get("missing") or []) if m.get("kind") in kinds]


CONTINUITY_LINE_RE = re.compile(r"图(\d+)是本镜头首帧")      # frame_job.LAST_CONTINUITY_LINE 的图号（尾帧 compose 的连续性参考）


def _continuity_ok(req: dict, refs: list, files_in_slots: list, prompt: str) -> bool:
    """I9（L-203 / FIX-PRE 11-06）：尾帧 compose 在槽位参考之后多挂一张“本镜头首帧”作连续性参考——refs = 槽位文件 + 这一张，
    且作业 continuity_ref.file 就是它、或提示词里“图N是本镜头首帧”的 N 正好是它的位置 → 算合法槽，不是“参考与规划不一致”"""
    k = len(files_in_slots)
    if len(refs) != k + 1 or refs[:k] != files_in_slots:
        return False
    cont = req.get("continuity_ref") or {}
    if isinstance(cont, dict) and cont.get("file") and cont.get("file") == refs[k]:
        return True
    m = CONTINUITY_LINE_RE.search(prompt or "")
    return bool(m and int(m.group(1)) == k + 1)


def check(req: dict) -> ContractResult:
    c = cfg()
    kind = req.get("kind", "image")
    rec = req.get("asset_refs") or {}
    prompt = req.get("prompt") or ""
    refs = list(req.get("refs") or [])
    items: list[Item] = []

    def add(i, status, detail="", **ev):
        items.append(Item(i, TITLES[i], status, detail, ev))

    # I1 在画角色 + 站位
    cast = req.get("cast")
    if cast is None:
        add("I1", "fail", "作业里没有在画角色清单（Shot Spec 人物）")
    else:
        on = [x for x in cast if x.get("presence") in ("在画", "局部")]
        nopos = [x.get("name") for x in on if x.get("presence") == "在画" and not (x.get("x") or x.get("pose"))]
        add("I1", "fail" if nopos else ("pass" if on else "na"),
            f"缺站位：{nopos}" if nopos else f"{len(on)} 人在画", on_screen=[x.get("name") for x in on])

    if kind == "image":
        if not rec:
            for i in ("I2", "I3", "I4", "I5", "I9"):
                add(i, "fail", "作业没有资产参考解析结果（ns_agent.asset_refs）——服装 / 鞋 / 饰品只靠文字，禁止提交")
        else:
            slots = rec.get("slots") or []
            files_in_slots = [s.get("file") for s in slots]
            # I2
            m = _kinds(rec, "身份脸缺图")
            faces = [s for s in slots if s.get("kind") == "face"]
            need_face = rec.get("subject") not in ("part", "object") and bool(rec.get("primary")) and any(
                c.get("character") == rec.get("primary") and (c.get("presence", "在画") == "在画" or c.get("face_described"))
                for c in rec.get("characters") or [{"character": rec.get("primary")}])
            # K-AST-263（IMGFIX 11-02，项目方 00:05“很多人物face都出了问题”）：每个露脸的人（在画，或局部但原文写到脸）都要有自己的
            # 身份脸（单独的 face 槽，或脸板里标了名字的一格）；提示词写了“两张脸都清晰入画”，身份脸至少两张
            face_ids = {s.get("character") for s in faces} | {mm.get("character") for s in slots if s.get("kind") == "faceboard"
                                                             for mm in s.get("members") or [] if mm.get("role") == "face"}
            faced = [c.get("character") for c in rec.get("characters") or []
                     if (c.get("presence") == "在画" and not c.get("back_view")) or c.get("face_described")]                 if rec.get("subject") not in ("part", "object") else []      # K-REN-957：在画但写背影 = 不露脸
            lack = [c for c in faced if c and c not in face_ids]
            two = "两张脸都清晰入画" in str(req.get("prompt") or "")
            # K-REN-708 矛盾 lint（阻断）：同一个人既写“只露出…，不露脸”，又是露脸人物 / 提示词有脸部占比（特写脸部构图）
            import re as _re
            hidden = set(_re.findall(r"([^，。、；（）]{1,12}?)(?:只露出[^，。]{1,30}，)?不露脸", str(req.get("prompt") or "")))
            names = {c.get("name"): c for c in rec.get("characters") or []}
            clash = [n for n in hidden if n in names and ((names[n].get("presence") == "在画" and not names[n].get("back_view"))
                                                          or names[n].get("face_described")
                                                          or ("脸部占画面" in str(req.get("prompt") or "") and len(names) == 1))]
            if m:
                add("I2", "fail", "；".join(x["detail"] for x in m))
            elif clash:
                add("I2", "fail", f"提示词矛盾：{'、'.join(clash)} 写了“不露脸”，但本镜他是露脸人物 / 脸部特写构图（K-REN-708，写作 / 导入格式问题）")
            elif manifest_missing_faces(faced, req.get("framing_project") or req.get("project")):
                add("I2", "fail", f"露脸的人在本剧资产清单里没有身份脸：{'、'.join(manifest_missing_faces(faced, req.get('framing_project') or req.get('project')))}（L-126 失败关闭，不退回旧脸）")
            elif lack:
                add("I2", "fail", f"露脸的人没有身份脸参考槽：{'、'.join(lack)}（K-AST-263）")
            elif stale_face_slots(slots, req.get('framing_project') or req.get('project')):
                # AUDIT-M A-01（总调度 11-02 17:1x）：原来只看“有没有身份脸槽”，登记过的旧脸也能过；身份脸只认 asset_refs.face_of 那一张（K-OPS-E29）
                add("I2", "fail", "身份脸参考不是当前身份脸（只认 asset_refs.face_of）：" + "；".join(stale_face_slots(slots, req.get('framing_project') or req.get('project'))))
            elif two and len(face_ids) < 2:
                add("I2", "fail", f"提示词写了两张脸都入画，但身份脸参考只有 {len(face_ids)} 张（K-AST-263 / K-REN-705）")
            elif need_face and rec.get("primary") not in face_ids:
                add("I2", "fail", f"主体角色 {rec.get('primary')} 脸可见，但没有身份脸参考槽")
            else:
                # 脸板逐格计人（ISS-1103-023528-1509）：三人镜 = 一块脸板三张身份脸
                n_id = len([x for x in face_ids if x])
                add("I2", "pass" if n_id else "na", f"{n_id} 张身份脸参考" + ("（脸板）" if any(s.get("kind") == "faceboard" for s in slots) else ""),
                    files=[s["file"] for s in faces] + [m.get("file") for s in slots if s.get("kind") == "faceboard"
                                                        for m in s.get("members") or [] if m.get("role") == "face"],
                    characters=sorted(x for x in face_ids if x))
            # I3
            m = _kinds(rec, "造型未登记") + [x for x in _kinds(rec, "资产缺图") if x.get("detail", "").endswith("没有设定图")]
            sheets = [mm for s in slots for mm in s.get("members") or [] if mm.get("role") == "sheet"]
            # ISS-1103-024205-8856（MERGE-IMG-O 11-03）：露脸、景别里看得到服装的人没有造型设定图（只有清单单品图 / 文字）→ fail。
            # 原来 V2 资产包“设定图在 S3 才出”时按单品图放行，S8 近景“设定图 0 张”照样 pass、首帧没有任何服装约束（C-16：资料齐全才出）
            no_sheet = sheet_gaps(rec.get("subject"), rec.get("zones"), rec.get("characters"), rec.get("warnings"))
            if m:
                add("I3", "fail", "；".join(x["detail"] for x in m))
            elif no_sheet:
                add("I3", "fail", "造型没有设定图（只有清单单品图 / 文字，资料不齐不出）：" + "；".join(x["detail"] for x in no_sheet),
                    no_sheet=[x.get("character") for x in no_sheet])
            else:
                looks = {ch["name"]: ch.get("look") for ch in rec.get("characters") or []}
                add("I3", "pass", f"造型：{looks}；设定图 {len(sheets)} 张", looks=looks)
            # I4
            m = [x for x in _kinds(rec, "资产缺图") if not x.get("detail", "").endswith("没有设定图")]
            items_used = [mm for s in slots for mm in s.get("members") or [] if mm.get("role") == "item"]
            if m:
                add("I4", "fail", "资产缺图：" + "；".join(x["detail"] for x in m))
            elif "feet" in (rec.get("zones") or []) and rec.get("primary") and not any(mm.get("zone") == "feet" for mm in items_used) \
                    and not any("鞋" in d.get("label", "") for d in rec.get("dropped") or []):
                add("I4", "fail", "鞋在画面里可见，但参考里没有鞋的单品图")
            else:
                sheet_only = [w for w in rec.get("warnings") or [] if w.get("kind") == "仅设定图"]
                add("I4", "pass", f"单品参考 {len(items_used)} 件" + (f"（{len(sheet_only)} 件只有设定图，建议补单品图）" if sheet_only else ""),
                    items=[mm.get("label") for mm in items_used])
            # I5
            m = _kinds(rec, "场景缺图")
            add("I5", "fail" if m else "pass", "；".join(x["detail"] for x in m) if m else "场景参考已登记（槽位不足时走文字，见 I9）")
            # I9
            bad = []
            cap = int(rec.get("capacity") or 0)
            if cap and len(slots) > cap:
                bad.append(f"参考 {len(slots)} 张 > {rec.get('model')} 槽位 {cap}")
            if refs != files_in_slots and not _continuity_ok(req, refs, files_in_slots, prompt):
                bad.append("实际送进模型的参考图与槽位规划不一致")
            # 脸板（K-AST-263：露脸 ≥3 人且槽位不够，每格一张标了名字的身份脸）不算“同一槽两张脸”；其它槽仍只许一张
            if any(int(s.get("faces") or 0) > 1 and not (s.get("kind") == "faceboard" and _faceboard_ok(s)) for s in slots):
                bad.append("同一参考槽里有两张脸")
            n_face_slots = sum(1 for s in slots if s.get("kind") == "face")
            two_mode = rec.get("two_person") or (__import__("ns_agent.asset_refs", fromlist=["x"])._cfg().get("two_person") or "primary_face_only")
            if n_face_slots > 1 and two_mode == "primary_face_only":
                bad.append("双人镜给了两张身份脸参考（契约：只给主体角色脸，第二人用造型设定图）")
            if faces and slots and slots[0].get("kind") != "face":
                bad.append("身份脸不在第 1 槽（优先级 身份脸 > 造型设定图 > 单品 > 场景）")
            add("I9", "fail" if bad else "pass", "；".join(bad) if bad else f"{len(slots)}/{cap} 槽",
                slots=[(s.get("kind"), s.get("labels")) for s in slots])
        # I6
        bad = []
        if "ns_agent.compiler" not in str(req.get("compiled_by") or ""):
            bad.append("提示词不是系统编译器输出（compiled_by 缺 ns_agent.compiler）")
        bakeoff = req.get("scope") == "bakeoff"       # BAKEOFF R11-12 A/B（只限 scope=bakeoff，不进生产）：认官方 <imageN> 写法
        nums = sorted({int(x) for x in REF_LINE.findall(prompt)} |
                      ({int(x) for x in IMG_TAG.findall(prompt)} if bakeoff else set()))
        if refs and nums != list(range(1, len(refs) + 1)):
            bad.append(f"参考图 {len(refs)} 张，但提示词里“图N是…”标注为 {nums}")
        for s in (rec.get("slots") or []):
            line = s.get("line") or ""
            alt = re.sub(r"图(\d+)", r"<image\1>", line) if bakeoff else line
            if s.get("kind") != "face" and line and line not in prompt and alt not in prompt:
                bad.append(f"图{s.get('index')} 的“必须一致”标注不在提示词里")
                break
        from ns_agent import framing as _FR
        if _FR.framing_clash(prompt):     # K-REN-902（IMGLAB 11-02）：特写“脸占六成”与“两张脸都入画”同时出现 = 构图矛盾
            bad.append("构图矛盾：同时写了“脸部占画面约六成”和“两张脸都清晰入画”（特写 + ≥2 人露脸，K-REN-902）")
        add("I6", "fail" if bad else "pass", "；".join(bad) if bad else "编译器输出 + 参考逐一标注")
        # I7
        _p7 = req.get("framing_project") or req.get("project")
        _sufs = ST.run_suffixes(_p7) if _p7 and ST._manifest_mode(_p7) else [ST.preset_suffix()]
        if str(req.get("mode") or "") == SETTING_CANDIDATE and str(req.get("asset_stage")) in EMPTY_STAGES:
            _sufs = [ST.EMPTY_SUFFIX]     # 空镜 / 单品设定候选：只认空镜后缀（人像风格后缀不许进空镜，ISS-1103-004550-8787）；禁用词 / V1 短语照查
        suf = next((x for x in _sufs if x and x in prompt), _sufs[0] if _sufs else "")   # 分段剧（L-130）：哪一段的句子都算
        hits = ST.banned_hits(prompt)
        bad = []
        if hits:
            bad.append(f"出现禁用后缀词：{hits}")
        v1s = ST.v1_style_hits(prompt, _p7 if _p7 and ST._manifest_mode(_p7) else None)
        if v1s:                                     # PROMPTFIX 20:4x（L-126）：V2 提示词里出现 V1 风格短语 = V1 内容泄漏，阻断
            bad.append(f"V2 提示词出现 V1 风格短语：{v1s}（S★ / 徕卡暖调这一组不许进 V2）")
        if suf and suf not in prompt and not (bakeoff and req.get("suffix_variant")):
            # BAKEOFF A/B（scope=bakeoff 且声明 suffix_variant）可换后缀，禁用词照查；生产不放
            bad.append(f"没有默认出图预设（{c.get('suffix_preset')}）的系统后缀")
        add("I7", "fail" if bad else "pass", "；".join(bad) if bad else f"预设 {c.get('suffix_preset')} 后缀")
    else:
        ref2va = "ref2va" in str(req.get("mode") or "").lower() or "ref2va" in str(req.get("model") or "").lower()
        ff = req.get("first_frame_postflight") or {}
        if ref2va:
            # 契约 §3“或 Ref2VA 的身份 + 造型参考”（VIDEO10 提议、REFLOCK 定）：每个在画露脸角色都要有已登记身份图
            slots = req.get("ref_slots") or []
            have = {s.get("character"): s.get("path") for s in slots if s.get("role") == "identity"}
            on = [x.get("name") for x in (req.get("cast") or []) if x.get("presence") == "在画"]
            _proj = req.get("framing_project") or req.get("project")
            from ns_agent import asset_refs as _ARr
            if _proj and _ARr.manifest_rel(_proj):     # OVERSIGHT-F P2：资产包模式（V2）身份图只认清单里的脸，不读 V1 data/assets/identity_refs
                reg = {(c.get("face") or {}).get("file") for c in ((_ARr.manifest(_proj) or {}).get("characters") or {}).values()} - {None}
            else:
                reg = registered_identity_files()
            miss = [n for n in on if n not in have]
            unreg = [f"{n}:{p}" for n, p in have.items() if n in on and _relpath(p) not in reg]
            bad = ([f"缺身份参考：{miss}"] if miss else []) + ([f"不是已登记身份图：{unreg}"] if unreg else [])
            add("I2", "fail" if bad else "pass", "；".join(bad) if bad else f"Ref2VA 身份参考覆盖 {len(on)} 人（已登记）",
                refs=have)
            looks = [s for s in slots if s.get("role") in ("outfit", "sheet", "look")]
            if req.get("look_refs") or any(s.get("role") == "outfit" for s in slots):
                # VIDEO10 造型槽（11-01 REFLOCK 审）：每个在画角色一张，且必须是已选定设定图或它的系统裁切（refcrops/<设定图名>_<栏>.png）
                chosen = registered_sheet_files(req.get("project"))
                by = {s.get("character"): s for s in looks}
                miss = [n for n in on if n not in by]
                unreg = [f"{n}:{s.get('path')}" for n, s in by.items() if n in on and not look_file_registered(s, chosen)]
                bad = ([f"缺造型参考：{miss}"] if miss else []) + ([f"不是已选定设定图 / 其裁切：{unreg}"] if unreg else [])
                add("I3", "fail" if bad else "pass", "；".join(bad) if bad else f"Ref2VA 造型参考覆盖 {len(on)} 人（已选定设定图裁切）",
                    looks={n: s.get("path") for n, s in by.items()})
            else:
                add("I3", "pass" if looks or not on else "fail",
                    f"造型参考 {len(looks)} 张" if looks or not on else "Ref2VA 没有造型参考（造型设定图 / 单品），服装只靠文字")
            for i in ("I4", "I5", "I9"):
                add(i, "na", "Ref2VA：单品 / 场景由造型参考与提示词承载（视频 postflight 查一致）")
        else:
            for i in ("I2", "I3", "I4", "I5", "I9"):
                if i == "I2":
                    add(i, "pass" if ff.get("ok") else "fail",
                        "首帧已过 postflight（身份 / 造型 / 场景由首帧承载）" if ff.get("ok") else "视频必须用已通过 Q1–Q7 的首帧")
                else:
                    add(i, "na", "视频：由首帧 postflight 承载")
        add("I6", "pass" if "compiler" in str(req.get("compiled_by") or "") else "fail", "视频提示词须系统编译（官方格式）")
        add("I7", "na", "视频后缀按视频配方")

    # I8 LoRA
    allow = set(c.get("lora_allow") or {})
    scope = req.get("scope")
    if scope:                                       # 作用范围白名单（例：bakeoff 的步数蒸馏 LoRA，不进生产）
        allow |= {k for k in ((c.get("lora_allow_scoped") or {}).get(scope) or {}) if not k.startswith("_")}
    banned = c.get("lora_banned") or {}
    bad = []
    for f, _w in [tuple(x)[:2] for x in req.get("loras") or []]:
        if f in banned:
            bad.append(f"{f} 已禁用（{banned[f][:40]}…）")
        elif kind == "image" and allow and f not in allow:
            bad.append(f"{f} 不在白名单（knowledge 已确认有效的出图 LoRA：{sorted(allow)}）")
    for t, why in (c.get("trigger_banned_in_image") or {}).items():
        if kind == "image" and re.search(rf"(^|[，,\s]){re.escape(t)}([，,\s]|$)", prompt):
            bad.append(f"提示词里有禁用触发词 {t}（{why}）")
    add("I8", "fail" if bad else "pass", "；".join(bad) if bad else "LoRA 都在白名单", loras=req.get("loras") or [])
    # I11 资产包清单（L-125 / C10，FACTORY 11-02 18:4x）：项目配了 assets_manifest 才判；没配 = na（V1 / 第二季旧行为）
    proj = req.get("framing_project") or req.get("project")
    try:
        from ns_agent import asset_refs as _AR
        if proj and _AR.manifest_rel(proj):
            files = [x for x in refs] + [s.get("file") for s in (rec.get("slots") or []) if s.get("file")]
            bad = _AR.manifest_violations(proj, files)
            add("I11", "fail" if bad else "pass", "；".join(bad[:5]) if bad else f"{len(set(files))} 个参考都在资产包清单里")
        elif not proj and kind == "image":       # OVERSIGHT-F P2：出图作业没写项目 = 判不了资产包 → 失败关闭
            add("I11", "fail", "出图作业没有 framing_project，判不了资产包清单（失败关闭）")
        else:
            add("I11", "na", "本项目没有资产包清单")
    except Exception as e:  # noqa: BLE001  # arch-ok: 资产包模式下查不了 = 不放行（失败关闭）
        add("I11", "fail", f"资产包清单检查失败：{e!r}"[:300])
    # I12 / I13（PROMPTFIX 11-02 19:1x，K-REN-958 / 959）：资产包模式才判
    try:
        from ns_agent import asset_refs as _AR2
        if proj and _AR2.manifest_rel(proj):
            route = req.get("route") or "server_ok"
            lane = str(req.get("lane") or "")
            import importlib.util as _u
            _sp = _u.spec_from_file_location("factory_hosts_pf", ROOT / "tools" / "factory" / "hosts.py")
            _H = _u.module_from_spec(_sp)
            _sp.loader.exec_module(_H)
            owner = _H.owner_lane(lane)          # L-128：项目方自有机器（笔记本 + gpu33 + gpu36，hosts.json owner_controlled）
            if route == "local_only" and not owner:
                add("I12", "fail", "本镜用到本地专用资产，只许项目方自有机器的道：" + "；".join((req.get("route_reasons") or [])[:3])
                    + ("（未指定道 = 失败关闭）" if not lane else f"（道 {lane} 不是自有机器）"))
            else:
                add("I12", "pass", "没有本地专用资产" if route != "local_only" else f"本地专用资产，自有机器道 {lane}")
            hits = _AR2.brand_hits(prompt)
            add("I13", "fail" if hits else "pass", ("提示词出现品牌字样：" + "、".join(hits)) if hits else "无品牌字样")
        elif not proj and kind == "image":       # OVERSIGHT-F P2：同 I11，没写项目 = 失败关闭
            add("I12", "fail", "出图作业没有 framing_project（失败关闭）")
            add("I13", "fail", "出图作业没有 framing_project（失败关闭）")
        else:
            add("I12", "na", "本项目没有资产包清单")
            add("I13", "na", "本项目没有资产包清单")
    except Exception as e:  # noqa: BLE001  # arch-ok: 资产包模式下查不了 = 不放行（失败关闭）
        add("I12", "fail", f"本地专用资产检查失败：{e!r}"[:300])
    # I10
    HC = _hc()
    try:                                         # W38：V2 资产包剧不套 V1 林晚的指甲锁（hard_constraints.check_look_lock nail_lock）
        from ns_agent import asset_refs as _AR10
        _v2 = bool(proj and _AR10.manifest_rel(proj))
    except Exception:  # noqa: BLE001  # arch-ok: 判不了 = 按原口径（指甲锁照拦，失败关闭）
        _v2 = False
    iss = [x for x in HC.check_text(prompt) + HC.check_look_lock(prompt, nail_lock=not _v2) if x.get("level") == "block"]
    add("I10", "fail" if iss else "pass", "；".join(x["message"] for x in iss) if iss else "成年人 / 锁定外貌检查通过")
    apply_asset_stage(req, items)
    order = {k: n for n, k in enumerate(TITLES)}
    items.sort(key=lambda i: order.get(i.id, 99))
    return ContractResult("preflight", str(req.get("shot_id") or req.get("id") or "?"), items, c.get("version", ""))


# 设定候选模式 mode=setting_candidate（AUTOPIPE-A 11-03，总调度 M-AUTOPIPE-01 阶段 A；CAST-S 卡点 ISS-1103-001014-c7ea）：
# 文生图出“本作业要产出的那张资产”时，对应的那几项按定义不可能满足，由系统按下表判 na（detail / evidence 保留原判），其余各项照查；
# 不认脚本自己豁免。只有作业显式写 mode="setting_candidate" + asset_stage（tools/pipeline/asset_worker.py 写）才生效；
# 写了 mode 但 asset_stage 缺 / 不认识 = 失败关闭（I2 fail）。Q9 在这个模式下单独一档（tools/pipeline/asset_checks.q9_asset：
# face / sheet 是证件式近正脸胸像 / 全身设定图，接受视线偏一侧，“看镜头”不判；未成年外观任何时候都不过）。
SETTING_CANDIDATE = "setting_candidate"
ASSET_STAGE_WAIVE = {
    "face": ("I2", "I3"),       # 新人物身份脸：没有身份脸（I2）、清单里还没有这个人的造型（I3）
    "sheet": ("I3", "I4"),      # 配角造型设定图：造型 / 单品都是它的产出（单品图之后从设定图裁，ISS-1103-013228-cab4 总调度定 A）；I2 照查——身份脸必须已登记并送进去（ISS-1103-004547-ca20）
    "scene": ("I5",),           # 场景底图：场景参考就是要产出的
    "item": ("I3", "I4"),       # 单品图：造型 / 单品图就是要产出的
}
EMPTY_STAGES = ("scene", "item")   # 画面里不该有人的设定候选：I7 只认空镜后缀 ST.EMPTY_SUFFIX


def apply_asset_stage(req: dict, items: list) -> None:
    if str(req.get("mode") or "") != SETTING_CANDIDATE:
        return
    stage = req.get("asset_stage")
    waive = ASSET_STAGE_WAIVE.get(str(stage))
    if waive is None:
        items.append(Item("I2", TITLES["I2"], "fail", f"mode={SETTING_CANDIDATE} 但 asset_stage={stage!r} 不是设定候选模式认识的类型 {sorted(ASSET_STAGE_WAIVE)}（失败关闭）"))
        return
    for it in items:
        if it.id in waive and it.status not in ("pass", "na"):
            it.evidence = {**(it.evidence or {}), "mode": SETTING_CANDIDATE, "asset_stage": stage, "original_status": it.status, "original_detail": it.detail}
            it.status, it.detail = "na", f"设定候选模式（asset_stage={stage}）：{it.title}是本作业要产出的，按规则不查；原判：{it.detail}"


def enforce(req: dict) -> ContractResult:
    """提交入口调用：不过就抛 ContractViolation（拒交）"""
    r = check(req)
    if not r.ok:
        raise ContractViolation(r)
    return r


def run(kind: str, payload: dict) -> dict:
    """统一签名（EDITDEL / VIDEO10 / BAKEOFF 等调用）：preflight(kind, payload) → {ok, items, blocking, reasons, ...}"""
    req = dict(payload or {})
    req.setdefault("kind", kind)
    r = check(req)
    d = r.to_dict()
    d["reasons"] = r.reasons()
    return d


class _Callable(__import__("types").ModuleType):
    def __call__(self, kind: str, payload: dict) -> dict:
        return run(kind, payload)


sys.modules[__name__].__class__ = _Callable
