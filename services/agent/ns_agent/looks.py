"""
每集角色造型（衣橱）：确定性种子器、JSON 存储、三层解析（默认 → 集计划 → 本镜覆盖）、
冲突检查、造型哈希、提示词片段。只读写 data/looks/*.json，从不触发渲染。

    uv run python -m ns_agent.looks seed            # data/looks/ 不存在时生成
    uv run python -m ns_agent.looks seed --force    # 覆盖（会丢掉人工修改！）
    uv run python -m ns_agent.looks show ep05-S3    # 打印该镜的解析结果和提示词片段
    uv run python -m ns_agent.looks confirm 1-8 lin_wan "项目方原话"   # 记录项目方确认集造型（只在项目方确认后跑）

种子器原则：只写剧本里有依据的东西；拿不准的放进 candidate_items / candidate_references，
标 needs_review + notes，不编造。依据（剧本原文摘录）写在集计划的 evidence 里。
"""
import hashlib
import json
import os
import re
import sys
import threading
from pathlib import Path

from ns_schema.looks import (CHARACTER_SLUG, CHARACTERS, SLOTS, AdornmentState, AffectedShots, CharacterLookPrompt,
                             Look, LookBook, LookConflict, LookPatch, LookPrompt, LookSchedule, LoraRef,
                             ResolvedField, ResolvedLook, ResolvedSlot, ShotLooks, Wardrobe, WardrobeItem)
from ns_schema.script import ScriptImport, Shot

ROOT = Path(os.environ.get("NS_PROJECT_ROOT") or Path(__file__).resolve().parents[3])  # T42：子进程可指向 projects/<slug>
DEFAULT_LOOKS_DIR = ROOT / "data" / "looks"                   # V1（legacy）位置；LookStore 缺省改经 run_paths 按剧取（I-14）
DEFAULT_SCRIPT_JSON = ROOT / "data" / "import" / "script_v2.json"
LIN_WAN_FACE = "assets/characters/lin_wan/lin_wan.png"
LAYERS = ("default", "episode", "shot")

# LoRA 触发词 → 槽位；身体装饰类为 when_in_shot（一直在身上，但只有画面里出现才进提示词）
TRIGGER_SLOT = {
    "hh12clear": "shoes", "hh12patent": "shoes",
    "ptxcorset": "outfit", "ptxharness": "outfit", "ptxtrench": "outfit", "ltxsuit": "outfit",
    "nprng2": "accessory", "lbrng9": "accessory", "lbrng9_beads": "accessory",
}
ADORNMENT_TRIGGERS = ("nprng2", "lbrng9", "lbrng9_beads")
REMOVABLE_ADORNMENTS = ("lbrng9_beads",)  # 珠链可摘（“在匣”）；环是穿孔，永久
# 剧本原文里指代 LoRA 单品的短语（不带触发词时用于检测；只作候选/冲突依据）
TRIGGER_KEYWORDS = {
    "hh12clear": ["透明细高跟", "透明高跟"],
    "hh12patent": ["漆皮细高跟", "黑漆皮高跟"],
    "ptxcorset": ["漆皮束腰"],
    "ptxharness": ["捆绑内衣"],
    "ptxtrench": ["漆皮长风衣", "漆皮风衣"],
    "ltxsuit": ["乳胶连体衣"],
    "nprng2": ["乳环"],
    "lbrng9": ["阴环", "九环"],
    "lbrng9_beads": ["铁珠", "珠串", "珠链"],
}
# 没有 LoRA、但剧本里明确出现的单品：一律 needs_review
KEYWORD_ITEMS = [
    # id, slot, name, keywords, policy, removable
    ("kw-evening-gown", "outfit", "黑色晚礼服", ["黑色晚礼服"], "always", True),
    ("kw-string-thong", "outfit", "黑色漆皮string丁字裤", ["string丁字裤"], "always", True),
    ("kw-mini-skirt", "outfit", "超短裙", ["超短裙"], "always", True),
    ("kw-suit-skirt", "outfit", "西装裙", ["西装裙"], "always", True),
    ("kw-pencil-skirt", "outfit", "及膝铅笔裙", ["铅笔裙"], "always", True),
    ("kw-black-stockings", "other", "黑色丝袜", ["黑色丝袜", "黑丝"], "always", True),
    ("kw-tongue-stud", "accessory", "舌钉", ["舌钉"], "when_in_shot", True),
    ("kw-tattoo-mandala", "other", "阴阜曼陀罗纹身", ["曼陀罗"], "when_in_shot", False),
    ("kw-tattoo-snake", "other", "大腿黑蛇纹身", ["黑蛇"], "when_in_shot", False),
    ("kw-tattoo-chen", "other", "乳下“沉”字纹身", ["“沉”字", "乳下“沉”"], "when_in_shot", False),
    ("kw-tattoo-feather", "other", "背部黑羽纹身", ["背羽", "黑羽"], "when_in_shot", False),
]
MAKEUP_BARE, MAKEUP_HEAVY, LW_HAIR = "lw-makeup-bare", "lw-makeup-heavy", "lw-hair"
MAKEUP_KEYWORDS = {MAKEUP_BARE: ["素颜", "无妆", "淡妆"], MAKEUP_HEAVY: ["浓妆", "正红唇", "烟熏"]}
# 从角色锁里抽取的单品（短语必须在锁原文里出现才会生成）
LOCK_ITEMS = [
    (LW_HAIR, "lin_wan", "hair", "蓝发及腰齐刘海", ["蓝发及腰", "齐刘海"]),
    ("gc-shirt", "gu_chen", "outfit", "黑色高定衬衫", ["黑色高定衬衫"]),
    ("cy-casual", "chen_yu", "outfit", "休闲装", ["休闲装"]),
]
CHARACTER_REFS = {"gu_chen": "REF-GC-01", "chen_yu": "REF-CY-01"}
PRESENCE_HINTS = {
    "lin_wan": ("林晚", "蓝发"), "gu_chen": ("顾沉",), "chen_yu": ("陈宇",),
    # RECALL 2026-11-01：旧 60 集 Alex/Ryan/Kai/Leo 撤下；其余登记角色（如 series10 阿岚）按中文名判在画
    **{cid: (name,) for cid, name in CHARACTERS.items() if cid not in ("lin_wan", "gu_chen", "chen_yu")},
}
SHOT_ID_RE = re.compile(r"^ep(\d+)-S(\d+)$")
# 参与解析的 LookPatch 字段（其余是审核元数据）
RESOLUTION_FIELDS = ("look_id", "slots", "d_state", "visibility", "present")
# 项目方确认字段：只由 confirm_episodes（CLI，须原话）写；HTTP 修改一律不接收
OWNER_FIELDS = ("owner_confirmed_at", "owner_confirmed_ref", "owner_confirmed_hash")


# ---------------------------------------------------------------- helpers

def parse_shot_id(shot_id: str) -> tuple[int, int]:
    m = SHOT_ID_RE.match(shot_id)
    if not m:
        raise KeyError(f"bad shot id: {shot_id}")
    return int(m.group(1)), int(m.group(2))


def resolve_character(c: str) -> str:
    """slug 或中文名 → slug"""
    if c in CHARACTERS:
        return c
    if c in CHARACTER_SLUG:
        return CHARACTER_SLUG[c]
    if c.lower() in CHARACTERS:
        return c.lower()
    raise KeyError(f"unknown character: {c}")


def parse_episode_list(text: str, valid: set[int] | None = None) -> list[int]:
    """严格解析 '1-8, 17' → [1..8, 17]；格式错、倒序区间、不存在的集号一律 ValueError，不猜。"""
    out: set[int] = set()
    tokens = [t.strip() for t in re.split(r"[,，、]", text)]
    if not text.strip() or any(not t for t in tokens):
        raise ValueError(f"bad episode list: {text!r}")
    for t in tokens:
        m = re.fullmatch(r"(?:ep)?(\d+)(?:\s*[-–]\s*(?:ep)?(\d+))?", t, re.I)
        if not m:
            raise ValueError(f"bad episode token: {t!r}")
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if lo > hi:
            raise ValueError(f"reversed range: {t!r}")
        out.update(range(lo, hi + 1))
    if valid is not None and (bad := sorted(out - valid)):
        raise ValueError(f"unknown episodes: {bad}")
    return sorted(out)


def _loose_ranges(text: str) -> set[int]:
    """注册表用途文本（带“等”“后”等字）的宽松提取，仅用于种子"""
    out: set[int] = set()
    for a, b in re.findall(r"(?:ep)?(\d+)(?:\s*[-–]\s*(?:ep)?(\d+))?", text):
        out.update(range(int(a), int(b or a) + 1))
    return out


def _start_ep(starts: str | None) -> int | None:
    m = re.search(r"ep(\d+)起", starts or "")
    return int(m.group(1)) if m else None


def parse_continuity_tables(md: str) -> dict[int, list[str]]:
    """剧本 md 里各批“装饰连续性自查表”：|集|列…| 表格 → {集: ["自查表 列名:值｜…"]}（原文摘录）"""
    out: dict[int, list[str]] = {}
    header: list[str] | None = None
    for line in md.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            header = None
            continue
        cells = [c.strip().replace("\\", "").replace("**", "") for c in line.strip("|").split("|")]
        if cells and cells[0] == "集":
            header = cells
            continue
        m = re.match(r"ep(\d+)$", cells[0]) if cells else None
        if header and m:
            out.setdefault(int(m.group(1)), []).append(
                "自查表 " + "｜".join(f"{h}:{v}" for h, v in zip(header[1:], cells[1:]) if v and v != "—"))
    return out


def _script_md(script: ScriptImport) -> str | None:
    for s in script.sources:
        if "剧本和镜头" in s.path:
            for p in (Path(s.path), ROOT / "script play lib" / Path(s.path.replace("\\", "/")).name):
                if p.exists():
                    return p.read_text(encoding="utf-8")
    return None


def detect_characters(shot: Shot, bible=None) -> list[str]:
    from .bible import load_bible
    # 封板修复（D-6）：“她”= 该角色的 motion 触发词读 visual_bible [characters.*] detect_she_with_trigger
    # （bible.CharacterRule.she_trigger，与编译器 present_characters 同一处配置），不再写死 lnwan01
    she = {c.id: c.she_trigger for c in (bible or load_bible()).characters if c.she_trigger}
    text = f"{shot.img_prompt} {shot.motion_prompt}"
    out = []
    for slug, hints in PRESENCE_HINTS.items():
        hit = any(h in text for h in hints) or (slug in she and she[slug] in shot.motion_triggers)
        if slug == "lin_wan":
            hit = hit or any(t in ADORNMENT_TRIGGERS for t in shot.img_triggers)
        if hit:
            out.append(slug)
    return out


def item_in_shot(item: WardrobeItem, shot: Shot) -> bool:
    return bool(item.trigger and item.trigger in shot.img_triggers) or any(k in shot.img_prompt for k in item.keywords)


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- seeder

def seed(script: ScriptImport, continuity_md: str | None = None) -> tuple[Wardrobe, LookBook, LookSchedule]:
    """确定性：同一份 script_v2.json（+ 剧本 md）永远产出同样的三份数据。"""
    g = script.globals
    locks = {CHARACTER_SLUG.get(c.name, c.name): c.description for c in g.character_locks}
    continuity = parse_continuity_tables(continuity_md) if continuity_md else {}
    items: list[WardrobeItem] = []

    # 1) LoRA 单品（剧本里这些 LoRA 都是林晚的穿戴）
    for t in g.lora_triggers:
        if t.kind != "image":
            continue
        slot = TRIGGER_SLOT.get(t.trigger, "other")
        adorn = t.trigger in ADORNMENT_TRIGGERS
        notes = ["归属林晚：剧本中该 LoRA 只用于林晚"]
        if slot == "shoes":
            notes.append("参考图未指定：候选在 assets/high heels/（人工挑选）")
        elif slot == "outfit":
            notes.append("参考图未指定：候选在 assets/clothes/（人工挑选）")
        if t.starts:
            notes.append(f"起用：{t.starts}")
        items.append(WardrobeItem(
            id=f"lora-{t.trigger}", slot=slot, name=t.description, description=t.description, character="lin_wan",
            trigger=t.trigger, lora_file=t.file, lora_strength=t.strength, keywords=TRIGGER_KEYWORDS.get(t.trigger, []),
            prompt_policy="when_in_shot" if adorn else "always",
            removable=not adorn or t.trigger in REMOVABLE_ADORNMENTS, source="lora_trigger", notes=notes))

    # 2) 角色锁单品
    for iid, char, slot, name, phrases in LOCK_ITEMS:
        lock = locks.get(char, "")
        found = [p for p in phrases if p in lock]
        if not found:
            continue
        refs = [LIN_WAN_FACE] if char == "lin_wan" and (ROOT / LIN_WAN_FACE).exists() else []
        items.append(WardrobeItem(id=iid, slot=slot, name=name, description="、".join(found), character=char,
                                  keywords=found, reference_images=refs, source="character_lock",
                                  notes=[f"来自角色锁：{lock}"]))

    # 3) 妆容（来自基准图注册表 REF-LW-01/02）
    refs = {r.id: r for r in g.references}
    for iid, ref_id, word in ((MAKEUP_BARE, "REF-LW-01", "素颜"), (MAKEUP_HEAVY, "REF-LW-02", "浓妆")):
        if ref_id in refs:
            items.append(WardrobeItem(id=iid, slot="makeup", name=word, description=word, character="lin_wan",
                                      keywords=MAKEUP_KEYWORDS[iid], reference_images=[ref_id], source="reference",
                                      notes=[f"{ref_id} {refs[ref_id].description}；用途：{refs[ref_id].usage}"]))

    # 4) 关键词单品
    for iid, slot, name, kws, policy, removable in KEYWORD_ITEMS:
        items.append(WardrobeItem(id=iid, slot=slot, name=name, description=name, character="lin_wan", keywords=kws,
                                  prompt_policy=policy, removable=removable, source="keyword", needs_review=True,
                                  notes=["剧本原文出现但无 LoRA；描述取原文短语，请确认"]))
    by_id = {i.id: i for i in items}

    # 5) 装饰状态（decor_schedule，含 overrides，如 ep17=D4）
    ep_decor: dict[int, str] = {}
    ep_decor_note: dict[int, str] = {}
    range_notes: dict[str, list[str]] = {}
    for r in g.decor_schedule:
        for ep in range(r.first_ep, r.last_ep + 1):
            ep_decor[ep] = r.overrides.get(ep, r.state)
            if r.note:
                ep_decor_note[ep] = r.note
        if r.note:
            range_notes.setdefault(r.state, []).append(f"ep{r.first_ep:02d}-{r.last_ep:02d} {r.state} {r.note}")
    for ep in script.episodes:  # 装饰表没覆盖的集用镜头里的 decor_state 补洞
        for s in ep.shots:
            if s.decor_state and ep.number not in ep_decor:
                ep_decor[ep.number] = s.decor_state
    starts = {t.trigger: _start_ep(t.starts) for t in g.lora_triggers if t.trigger in ADORNMENT_TRIGGERS}
    states: list[AdornmentState] = []
    for sid in sorted(set(ep_decor.values()), key=lambda x: (not x[1:].isdigit(), int(x[1:]) if x[1:].isdigit() else 0)):
        eps = sorted(e for e, s in ep_decor.items() if s == sid)
        notes = ["剧本未给出该状态的逐条定义（数据C 五层锁全文库不在仓库内），description 待人工填写",
                 "items 由 LoRA 起用集推出：该状态最早一集 ≥ 起用集即视为在身"]
        notes += range_notes.get(sid, [])
        sitems = [f"lora-{t}" for t, st in starts.items() if st is not None and eps and min(eps) >= st]
        if sid == "DN" and "lora-lbrng9_beads" in sitems:
            sitems.remove("lora-lbrng9_beads")
            notes.append("排除 lbrng9_beads：装饰表“ep53:D9+珠链在匣(DN)”提示 DN=珠链在匣")
        states.append(AdornmentState(id=sid, items=sitems, episodes=eps, notes=notes))
    wardrobe = Wardrobe(items=items, decor_states=states)

    # 6) 出场检测
    schedule = LookSchedule()
    for ep in script.episodes:
        for s in ep.shots:
            schedule.shot_characters[s.id] = detect_characters(s)

    # 7) 第一层：角色默认造型
    looks: list[Look] = []
    for slug in CHARACTERS:
        look = Look(id=f"{slug}-default", character=slug, name="默认")
        for it in items:
            if it.character == slug and it.source == "character_lock":
                look.items.setdefault(it.slot, []).append(it.id)
        if slug in CHARACTER_REFS and CHARACTER_REFS[slug] in refs:
            look.reference_images.append(CHARACTER_REFS[slug])
        if not look.items:
            look.needs_review = True
            look.notes.append("剧本没有该角色的角色锁（数据C 未入库），造型待人工填写")
        looks.append(look)
        schedule.character_defaults[slug] = look.id

    # 8) 第二层：林晚的集造型计划（妆容 + D 状态 + 可见性；服装只给候选）
    bare_eps = _loose_ranges(refs["REF-LW-01"].usage or "") if "REF-LW-01" in refs else set()
    heavy_from = min(_loose_ranges(refs["REF-LW-02"].usage or "") or {0}) if "REF-LW-02" in refs else None
    full_from = min(_loose_ranges(refs["REF-LW-03"].usage or "") or {0}) if "REF-LW-03" in refs else None
    worn_items = [i for i in items if i.character == "lin_wan" and i.slot in ("outfit", "shoes", "other")
                  and i.prompt_policy == "always"]
    for ep in script.episodes:
        n = ep.number
        p = LookPatch(d_state=ep_decor.get(n))
        note = ep_decor_note.get(n, "")
        if "隐藏" in note:
            p.visibility = "hidden"
            p.notes.append(f"装饰表“{note}”→ visibility=hidden")
        elif "在匣" in note:
            p.visibility = "in_box"
            p.notes.append(f"装饰表“{note}”→ visibility=in_box（可摘装饰收匣，穿孔仍在）")
        ep_text = " ".join(s.img_prompt for s in ep.shots)
        if n in bare_eps and MAKEUP_BARE in by_id:
            p.slots = {"makeup": [MAKEUP_BARE]}
            p.reference_images.append("REF-LW-01")
            heavy_hits = [k for k in MAKEUP_KEYWORDS[MAKEUP_HEAVY] if k in ep_text]
            if heavy_hits or n == heavy_from:
                p.needs_review = True
                p.notes.append(f"妆容冲突：REF-LW-01 列为素颜集，但 REF-LW-02 写“ep{heavy_from:02d}后浓妆集”"
                               + (f"，且本集镜头出现 {'/'.join(heavy_hits)}，可能需要逐镜覆盖" if heavy_hits else ""))
        elif heavy_from and n >= heavy_from and MAKEUP_HEAVY in by_id:
            p.slots = {"makeup": [MAKEUP_HEAVY]}
            p.reference_images.append("REF-LW-02")
            p.needs_review = True
            p.notes.append("妆容为推断：不在 REF-LW-01 素颜集清单（清单带“等”字，不完整），按 REF-LW-02 用途归浓妆")
            bare_hits = [k for k in MAKEUP_KEYWORDS[MAKEUP_BARE] if k in ep_text]
            if bare_hits:
                p.notes.append(f"本集镜头出现 {'/'.join(bare_hits)}，与浓妆推断矛盾")
        else:
            p.needs_review = True
            p.notes.append("妆容未定：REF 注册表没有覆盖本集")
        if full_from and n >= full_from and n not in bare_eps and "REF-LW-03" in refs:
            p.candidate_references.append("REF-LW-03")
        p.evidence += continuity.get(n, [])
        for s in ep.shots:
            hits = []
            for it in worn_items:
                if it.trigger and it.trigger in s.img_triggers:
                    hits.append(f"{it.trigger}(触发词)")
                elif any(k in s.img_prompt for k in it.keywords):
                    hits.append(f"{it.name}(关键词)")
                else:
                    continue
                if it.id not in p.candidate_items.setdefault(it.slot, []):
                    p.candidate_items[it.slot].append(it.id)
            mk = [k for kws in MAKEUP_KEYWORDS.values() for k in kws if k in s.img_prompt]
            if mk:
                hits.append("妆容关键词:" + "/".join(mk))
            if hits:
                p.evidence.append(f"{s.id} img: " + "，".join(hits))
        if p.candidate_items:
            p.needs_review = True
            p.notes.append("本集镜头里检测到服装/鞋（含挂着、拿着的道具镜），是否为本集穿着需人工确认，"
                           "确认后写进 slots 或建命名造型")
        p.notes.append("服装/鞋未设：剧本只有逐镜描述，没有整集服装定义")
        schedule.episodes.setdefault(str(n), {})["lin_wan"] = p

    return wardrobe, LookBook(looks=looks), schedule


# ---------------------------------------------------------------- resolution

class Resolver:
    """纯计算：输入四份数据，不读写文件。"""

    def __init__(self, script: ScriptImport, wardrobe: Wardrobe, book: LookBook, schedule: LookSchedule,
                 canvas=None):
        self.script, self.schedule = script, schedule
        # 画布参考覆盖（W5，ns_agent.ref_overrides.Canvas）：场景层 / 画布本镜层；None = 没有
        self.canvas = canvas if canvas else None
        self._scene_of: dict[str, str] | None = None
        self.items = {i.id: i for i in wardrobe.items}
        self.states = {s.id: s for s in wardrobe.decor_states}
        self.looks = {lk.id: lk for lk in book.looks}
        self.locks = {CHARACTER_SLUG.get(c.name, c.name): c.description for c in script.globals.character_locks}
        self.shots = [s for ep in script.episodes for s in ep.shots]
        self.pos = {s.id: k for k, s in enumerate(self.shots)}

    # 出场角色
    def characters(self, shot: Shot) -> list[tuple[str, str]]:
        ov = self.schedule.shot_overrides.get(shot.id, {})
        # 按当前剧本现算（封板修复）：schedule.shot_characters 只在种子时写，重新 / 合并导入后会过期
        # （ep01 换成 7 镜后 S7 没有角色、S2/S5/S6 角色错位）；人工增删仍走 shot_overrides
        out = [(c, "detected") for c in detect_characters(shot)
               if not (c in ov and ov[c].present is False)]
        out += [(c, "shot") for c, p in ov.items() if p.present is not False and c not in dict(out)]
        return out

    def layers(self, char: str, ep: int, shot_id: str | None) -> list[tuple[str, LookPatch | None]]:
        default = self.schedule.character_defaults.get(char)
        out = [("default", LookPatch(look_id=default) if default else None),
               ("episode", self.schedule.episodes.get(str(ep), {}).get(char)),
               ("shot", self.schedule.shot_overrides.get(shot_id, {}).get(char) if shot_id else None)]
        if self.canvas and shot_id:     # 画布覆盖（§6）：场景层在集计划之后，画布本镜层最后
            from .ref_overrides import to_patch
            out.insert(2, ("scene", to_patch(self.canvas.scene(ep, self.scene_of(shot_id)), char)))
            out.append(("shot", to_patch(self.canvas.shot(shot_id), char)))
        return out

    def scene_of(self, shot_id: str | None) -> str | None:
        """镜头所在场景 scene_id（state_timeline.group_scenes，与时间线同一口径）；只在有场景覆盖时才算"""
        if not shot_id or not self.canvas or not self.canvas.scenes:
            return None
        if self._scene_of is None:
            from .state_timeline import group_scenes
            self._scene_of = {r["shot_id"]: r["scene_id"] for r in group_scenes(
                self.script, lambda s: {c for c, _ in self.characters(s)})}
        return self._scene_of.get(shot_id)

    def canvas_slots(self, char: str, ep: int, shot_id: str | None) -> list[str]:
        """画布覆盖（场景 / 本镜）改过的槽位：整套换 look → 该造型的全部槽位；单件 → 该槽位"""
        if not self.canvas or not shot_id:
            return []
        from .ref_overrides import to_patch
        out: list[str] = []
        for p in (to_patch(self.canvas.scene(ep, self.scene_of(shot_id)), char), to_patch(self.canvas.shot(shot_id), char)):
            if not p:
                continue
            lk = self.looks.get(p.look_id or "")
            out += list(lk.items) if lk else []
            out += list(p.slots or {})
        return [s for s in SLOTS if s in out]

    def core(self, char: str, ep: int, shot_id: str | None = None, present_source: str = "detected") -> ResolvedLook:
        r = ResolvedLook(character=char, name=CHARACTERS.get(char, char), present_source=present_source)
        slots: dict[str, tuple[list[str], str]] = {}
        for layer, p in self.layers(char, ep, shot_id):
            if not p:
                continue
            if layer == "shot":
                r.has_override = True
            if p.look_id:
                lk = self.looks.get(p.look_id)
                if not lk:
                    r.conflicts.append(LookConflict(kind="unknown_ref", severity="error",
                                                    message=f"{layer} 层引用了不存在的造型 {p.look_id}"))
                else:
                    r.look_id, r.look_id_source, r.look_name = lk.id, layer, lk.name
                    for s, ids in lk.items.items():
                        slots[s] = (list(ids), layer)
                    if lk.decor_state:
                        r.d_state = ResolvedField(value=lk.decor_state, source=layer)
                    r.reference_images += [x for x in lk.reference_images if x not in r.reference_images]
                    r.needs_review |= lk.needs_review
                    r.notes += lk.notes
            for s, ids in (p.slots or {}).items():
                slots[s] = (list(ids), layer)
            if p.d_state:
                r.d_state = ResolvedField(value=p.d_state, source=layer)
            if p.visibility:
                r.visibility = ResolvedField(value=p.visibility, source=layer)
            r.reference_images += [x for x in p.reference_images if x not in r.reference_images]
            r.needs_review |= p.needs_review
            r.notes += p.notes
        for s in SLOTS:
            if s in slots:
                ids, src = slots[s]
                r.slots[s] = ResolvedSlot(items=ids, names=[self.items[i].name if i in self.items else f"?{i}"
                                                            for i in ids], source=src)
        r.label = " · ".join(x for x in (r.name, r.look_name, r.d_state.value) if x)
        r.hash = self._look_hash(r)
        return r

    def _look_hash(self, r: ResolvedLook) -> str:
        """只含影响画面的内容：单品内容（不只是 id）、D 状态及其单品、可见性。审核元数据不进哈希。"""
        def fp(iid: str):
            it = self.items.get(iid)
            return [iid, it.model_dump(include={"slot", "description", "trigger", "lora_file", "lora_strength",
                                                "prompt_policy", "removable", "reference_images"}) if it else None]
        st = self.states.get(r.d_state.value or "")
        return _hash({
            "c": r.character,
            "slots": {s: [fp(i) for i in v.items] for s, v in r.slots.items()},
            "d": [r.d_state.value, st.description if st else None, [fp(i) for i in st.items] if st else None],
            "v": r.visibility.value,
        })

    def conflicts(self, r: ResolvedLook, shot: Shot) -> list[LookConflict]:
        out: list[LookConflict] = []
        st = self.states.get(r.d_state.value or "")
        if r.d_state.value and not st:
            out.append(LookConflict(kind="unknown_ref", severity="error", message=f"未知装饰状态 {r.d_state.value}"))
        state_items = st.items if st else []
        in_effect = [i for v in r.slots.values() for i in v.items]
        for iid in in_effect:
            it = self.items.get(iid)
            if not it:
                out.append(LookConflict(kind="unknown_ref", severity="error", message=f"未知单品 {iid}", item_id=iid))
            elif not it.reference_images:
                out.append(LookConflict(kind="missing_reference", severity="warning", slot=it.slot, item_id=iid,
                                        message=f"{it.name} 没有参考图"))
        for it in self.items.values():
            if it.character != r.character or not item_in_shot(it, shot):
                continue
            if it.prompt_policy == "when_in_shot":
                if it.id not in state_items and it.id not in in_effect:
                    out.append(LookConflict(kind="adornment_not_in_state", severity="error", slot=it.slot,
                                            item_id=it.id, message=f"剧本本镜写到 {it.name}，但 {r.d_state.value or '无D状态'} 里没有"))
                elif r.visibility.value == "hidden" or (r.visibility.value == "in_box" and it.removable):
                    out.append(LookConflict(kind="adornment_hidden", severity="warning", slot=it.slot, item_id=it.id,
                                            message=f"剧本本镜写到 {it.name}，但可见性为 {r.visibility.value}"))
                continue
            slot = r.slots.get(it.slot)
            if slot and it.id in slot.items:
                continue
            if slot and slot.items:
                out.append(LookConflict(kind="script_item_not_in_look", severity="warning", slot=it.slot, item_id=it.id,
                                        message=f"剧本本镜写到 {it.name}，造型 {it.slot} 为 {'、'.join(slot.names)}"))
            else:
                out.append(LookConflict(kind="script_item_unplanned", severity="info", slot=it.slot, item_id=it.id,
                                        message=f"剧本本镜写到 {it.name}，造型未设 {it.slot}"))
        if r.character == "lin_wan" and shot.decor_state and r.d_state.value and r.d_state.value != shot.decor_state:
            out.append(LookConflict(kind="d_state_differs_from_script", severity="warning",
                                    message=f"剧本标 {shot.decor_state}，造型为 {r.d_state.value}"))
        return out

    def prev_shot(self, shot: Shot, char: str) -> Shot | None:
        for k in range(self.pos[shot.id] - 1, -1, -1):
            s = self.shots[k]
            if any(c == char for c, _ in self.characters(s)):
                return s
        return None

    def resolve(self, shot: Shot, char: str, present_source: str = "detected") -> ResolvedLook:
        r = self.core(char, shot.episode, shot.id, present_source)
        r.conflicts = r.conflicts + self.conflicts(r, shot)
        prev = self.prev_shot(shot, char)
        if prev:
            pr = self.core(char, prev.episode, prev.id)
            r.prev_shot_id = prev.id
            for s in SLOTS:
                a = r.slots.get(s, ResolvedSlot()).items
                b = pr.slots.get(s, ResolvedSlot()).items
                if a != b:
                    r.changed_fields.append(s)
                    if s in r.slots:
                        r.slots[s].changed_from_prev = True
            if r.d_state.value != pr.d_state.value:
                r.changed_fields.append("d_state")
                r.d_state.changed_from_prev = True
            if r.visibility.value != pr.visibility.value:
                r.changed_fields.append("visibility")
                r.visibility.changed_from_prev = True
            r.changed_from_prev = bool(r.changed_fields)
        return r

    def prompt(self, shot: Shot, resolved: list[ResolvedLook]) -> LookPrompt:
        out = LookPrompt(shot_id=shot.id, fragment="")
        for r in resolved:
            cp = CharacterLookPrompt(character=r.character, name=r.name, fragment="")
            ids = [i for s in SLOTS for i in r.slots.get(s, ResolvedSlot()).items]
            st = self.states.get(r.d_state.value or "")
            if st:
                ids += [i for i in st.items if i not in ids]
            parts = []
            for iid in ids:
                it = self.items.get(iid)
                if not it:
                    continue
                if it.prompt_policy == "when_in_shot":
                    hidden = r.visibility.value == "hidden" or (r.visibility.value == "in_box" and it.removable)
                    if hidden or not item_in_shot(it, shot):
                        cp.skipped_not_in_shot.append(iid)
                        continue
                if it.trigger and it.trigger not in cp.triggers:
                    cp.triggers.append(it.trigger)
                if it.lora_file and not any(lr.file == it.lora_file for lr in cp.loras):
                    cp.loras.append(LoraRef(file=it.lora_file,
                                            strength=it.lora_strength[1] if it.lora_strength else 0.8))
                if not it.description:
                    continue
                if it.description in shot.img_prompt or it.description in self.locks.get(r.character, ""):
                    cp.already_in_prompt.append(iid)
                else:
                    parts.append(it.description)
            if st and st.description:
                parts.append(st.description)
            if parts:
                cp.fragment = f"【{r.name}造型】" + "，".join(parts)
            out.characters.append(cp)
            out.triggers += [t for t in cp.triggers if t not in out.triggers]
            out.loras += [lr for lr in cp.loras if not any(x.file == lr.file for x in out.loras)]
        out.fragment = "；".join(c.fragment for c in out.characters if c.fragment)
        return out

    def shot_looks(self, shot: Shot) -> ShotLooks:
        resolved = [self.resolve(shot, c, src) for c, src in self.characters(shot)]
        return ShotLooks(shot_id=shot.id, episode=shot.episode, index=shot.index,
                         script_decor_state=shot.decor_state, characters=resolved,
                         look_hash=_hash([[r.character, r.hash] for r in resolved]),
                         prompt=self.prompt(shot, resolved))

    def shot_hash(self, shot: Shot) -> str:
        return _hash([[c, self.core(c, shot.episode, shot.id, src).hash] for c, src in self.characters(shot)])

    def all_hashes(self) -> dict[str, str]:
        return {s.id: self.shot_hash(s) for s in self.shots}

    def confirmation_hash(self, char: str, ep: int) -> str:
        """项目方确认绑定的内容：该集该角色的集层解析结果（默认 → 集计划，含单品内容）"""
        return self.core(char, ep).hash

    def episode_confirmed(self, char: str, ep: int) -> bool:
        """确认有效 = 有确认时间 + 非空原话 + 确认时的内容哈希与当前一致（改了造型/单品/默认 → 作废）"""
        p = self.schedule.episodes.get(str(ep), {}).get(char)
        return bool(p and p.owner_confirmed_at and (p.owner_confirmed_ref or "").strip() and p.owner_confirmed_hash
                    and p.owner_confirmed_hash == self.confirmation_hash(char, ep))


def compute_look_prompt(shot: Shot, script: ScriptImport, wardrobe: Wardrobe, book: LookBook,
                        schedule: LookSchedule) -> LookPrompt:
    """纯函数版本：输入全部显式给出，不读文件。"""
    return Resolver(script, wardrobe, book, schedule).shot_looks(shot).prompt


# ---------------------------------------------------------------- store

class LookStore:
    """data/looks/ 下三份 JSON 的读写。文件缺失时用剧本种子生成。所有修改返回受影响的镜头，从不触发渲染。"""

    def __init__(self, looks_dir: Path | str | None = None, script_json: Path | str | None = None):
        from . import run_paths as RP                     # I-14：缺省按当前剧（V2 不读 V1 的 data/looks / 导入稿）
        self.dir = Path(looks_dir or os.environ.get("NS_LOOKS_DIR") or RP.looks_dir(ROOT))
        self.script_json = Path(script_json or RP.script_json(ROOT))
        self._lock = threading.RLock()
        self._script: ScriptImport | None = None
        self._script_key: tuple[int, int] | None = None   # 读入时剧本文件的 (mtime_ns, size)

    @property
    def script(self) -> ScriptImport:
        """封板修复（A-4）：剧本文件变了（重新 / 合并导入）就重读。原来进程里只读一次：API 单例在只有 ep01 的
        导入时读进来，合并导入回到 60 集后仍只认 ep01 → /api/looks/episodes/2 404。测试直接赋值 _script 的不重读"""
        try:
            st = self.script_json.stat()
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        if self._script is None or (self._script_key is not None and key is not None and key != self._script_key):
            self._script = ScriptImport.model_validate_json(self.script_json.read_text(encoding="utf-8"))
            self._script_key = key
        return self._script

    def _path(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    def exists(self) -> bool:
        return all(self._path(n).exists() for n in ("wardrobe", "looks", "schedule"))

    def seed(self, force: bool = False) -> None:
        with self._lock:
            if self.exists() and not force:
                raise FileExistsError(f"{self.dir} 已有造型数据；覆盖请加 --force（会丢掉人工修改）")
            w, b, s = seed(self.script, _script_md(self.script))
            self._write("wardrobe", w)
            self._write("looks", b)
            self._write("schedule", s)

    def _write(self, name: str, model) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path(name).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(model.model_dump(mode="json", exclude_none=False), ensure_ascii=False, indent=2)
                       + "\n", encoding="utf-8", newline="\n")
        tmp.replace(self._path(name))
        self._changed()

    def _changed(self) -> None:
        """T51：项目数据目录下的造型改动 → 后台防抖重建 data/state_timeline.json + scenes.json（沙箱/测试目录不触发）"""
        try:
            if self.dir.resolve() != (Path(os.environ.get("NS_PROJECT_ROOT") or ROOT) / "data" / "looks").resolve():
                return
            from .state_timeline import schedule_rebuild
            schedule_rebuild(self.dir.parent.parent)
        except Exception:  # noqa: BLE001 — 重建失败不影响保存
            pass

    def _read(self, name: str, cls):
        with self._lock:
            if not self.exists():
                self.seed()
            return cls.model_validate_json(self._path(name).read_text(encoding="utf-8"))

    def wardrobe(self) -> Wardrobe:
        return self._read("wardrobe", Wardrobe)

    def lookbook(self) -> LookBook:
        return self._read("looks", LookBook)

    def schedule(self) -> LookSchedule:
        return self._read("schedule", LookSchedule)

    @property
    def renders_dir(self) -> Path:
        """画布覆盖索引所在（data/renders，与 data/looks 同级）"""
        return self.dir.parent / "renders"

    def canvas(self):
        from .ref_overrides import load_canvas
        return load_canvas(self.renders_dir)

    def resolver(self, canvas=True) -> Resolver:
        """canvas=True：并入画布参考覆盖（data/renders/ref_overrides.json）；False = 只看 data/looks；也可直接传 Canvas"""
        with self._lock:
            cv = self.canvas() if canvas is True else (canvas or None)
            return Resolver(self.script, self.wardrobe(), self.lookbook(), self.schedule(), cv)

    # ---- 查询
    def episode(self, ep: int):
        e = next((e for e in self.script.episodes if e.number == ep), None)
        if not e:
            raise KeyError(f"unknown episode: {ep}")
        return e

    def shot(self, shot_id: str) -> Shot:
        ep, idx = parse_shot_id(shot_id)
        s = next((s for s in self.episode(ep).shots if s.index == idx), None)
        if not s:
            raise KeyError(f"unknown shot: {shot_id}")
        return s

    def shot_looks(self, shot_id: str) -> ShotLooks:
        return self.resolver().shot_looks(self.shot(shot_id))

    def look_prompt(self, shot_id: str) -> LookPrompt:
        return self.shot_looks(shot_id).prompt

    def get_look(self, look_id: str) -> Look | None:
        return next((lk for lk in self.lookbook().looks if lk.id == look_id), None)

    # ---- 校验
    def _check_items(self, slots: dict[str, list[str]], ids: set[str]) -> None:
        for slot, lst in slots.items():
            if slot not in SLOTS:
                raise ValueError(f"unknown slot: {slot}")
            if missing := [i for i in lst if i not in ids]:
                raise ValueError(f"unknown wardrobe items: {missing}")

    def _check_patch(self, char: str, p: LookPatch) -> None:
        w = self.wardrobe()
        if p.look_id:
            lk = self.get_look(p.look_id)
            if not lk:
                raise KeyError(f"unknown look: {p.look_id}")
            if lk.character != char:
                raise ValueError(f"look {p.look_id} belongs to {lk.character}, not {char}")
        ids = {i.id for i in w.items}
        self._check_items(p.slots or {}, ids)
        self._check_items(p.candidate_items, ids)
        if p.d_state and p.d_state not in {s.id for s in w.decor_states}:
            raise ValueError(f"unknown decor state: {p.d_state}")

    # ---- 修改（都返回 AffectedShots）
    def _mutate(self, fn) -> AffectedShots:
        with self._lock:
            before = self.resolver().all_hashes()
            fn()
            after = self.resolver().all_hashes()
            changed = [k for k in after if before.get(k) != after[k]]
            return AffectedShots(affected_count=len(changed), affected_shots=changed)

    def upsert_item(self, item: WardrobeItem) -> AffectedShots:
        def fn():
            w = self.wardrobe()
            idx = next((k for k, i in enumerate(w.items) if i.id == item.id), None)
            if idx is None:
                w.items.append(item)
            else:
                w.items[idx] = item
            self._write("wardrobe", w)
        if item.character:
            item.character = resolve_character(item.character)
        return self._mutate(fn)

    def upsert_decor_state(self, state: AdornmentState) -> AffectedShots:
        w = self.wardrobe()
        if missing := [i for i in state.items if i not in {x.id for x in w.items}]:
            raise ValueError(f"unknown wardrobe items: {missing}")

        def fn():
            w.decor_states = [s for s in w.decor_states if s.id != state.id] + [state]
            self._write("wardrobe", w)
        return self._mutate(fn)

    def upsert_look(self, look: Look) -> AffectedShots:
        look.character = resolve_character(look.character)
        w = self.wardrobe()
        self._check_items(look.items, {i.id for i in w.items})
        if look.decor_state and look.decor_state not in {s.id for s in w.decor_states}:
            raise ValueError(f"unknown decor state: {look.decor_state}")
        old = self.get_look(look.id)
        if old and old.character != look.character:
            raise ValueError(f"look {look.id} belongs to {old.character}")

        def fn():
            b = self.lookbook()
            idx = next((k for k, lk in enumerate(b.looks) if lk.id == look.id), None)
            if idx is None:
                b.looks.append(look)
            else:
                b.looks[idx] = look
            self._write("looks", b)
        return self._mutate(fn)

    def set_character_default(self, char: str, look_id: str) -> AffectedShots:
        char = resolve_character(char)
        self._check_patch(char, LookPatch(look_id=look_id))

        def fn():
            s = self.schedule()
            s.character_defaults[char] = look_id
            self._write("schedule", s)
        return self._mutate(fn)

    @staticmethod
    def _merge(old: LookPatch | None, new: LookPatch) -> LookPatch:
        """body 里出现的字段覆盖旧值（显式 null 清空），没出现的字段保留。
        项目方确认字段从不从 body 接收（只有 confirm_episodes 能写）；解析内容字段变了 → 旧确认清掉。"""
        base = old or LookPatch()
        update = {f: getattr(new, f) for f in new.model_fields_set if f not in OWNER_FIELDS}
        if any(f in RESOLUTION_FIELDS and getattr(base, f) != v for f, v in update.items()):
            update.update({f: None for f in OWNER_FIELDS})
        return base.model_copy(update=update)

    def _set_patches(self, table: str, keys: list[str], char: str, patch: LookPatch | None) -> AffectedShots:
        """patch=None 删除该层（恢复继承）"""
        char = resolve_character(char)
        sched = self.schedule()
        tbl: dict = getattr(sched, table)
        merged = {}
        if patch is not None:
            for k in keys:
                merged[k] = self._merge(tbl.get(k, {}).get(char), patch)
                self._check_patch(char, merged[k])

        def fn():
            s = self.schedule()
            t: dict = getattr(s, table)
            for k in keys:
                if patch is None:
                    t.get(k, {}).pop(char, None)
                    if k in t and not t[k]:
                        t.pop(k)
                else:
                    t.setdefault(k, {})[char] = merged[k]
            self._write("schedule", s)
        return self._mutate(fn)

    def patch_episode(self, ep: int, char: str, patch: LookPatch | None) -> AffectedShots:
        self.episode(ep)
        return self._set_patches("episodes", [str(ep)], char, patch)

    def patch_episodes(self, episodes: str, char: str, patch: LookPatch | None) -> AffectedShots:
        eps = parse_episode_list(episodes, {e.number for e in self.script.episodes})
        return self._set_patches("episodes", [str(e) for e in eps], char, patch)

    def confirm_episodes(self, episodes: str, char: str, ref: str, at: str | None = None,
                         revoke: bool = False) -> list[int]:
        """记录项目方确认集造型（owner_confirmed_at/ref）。不改解析内容，所以不算“受影响镜头”。只在项目方本人确认后调用"""
        import time
        if not revoke and not ref.strip():
            raise ValueError("确认必须写项目方原话/消息引用")
        char = resolve_character(char)
        eps = parse_episode_list(episodes, {e.number for e in self.script.episodes})
        with self._lock:
            s = self.schedule()
            for e in eps:
                p = s.episodes.setdefault(str(e), {}).setdefault(char, LookPatch())
                p.owner_confirmed_at = None if revoke else (at or time.strftime("%Y-%m-%dT%H:%M:%S"))
                p.owner_confirmed_ref = None if revoke else ref.strip()
                p.owner_confirmed_hash = None
            if not revoke:     # 绑定确认时的解析内容：之后改造型/单品/默认 → 确认作废（Resolver.episode_confirmed）
                res = Resolver(self.script, self.wardrobe(), self.lookbook(), s)
                for e in eps:
                    s.episodes[str(e)][char].owner_confirmed_hash = res.confirmation_hash(char, e)
            self._write("schedule", s)
        return eps

    def patch_shot(self, shot_id: str, char: str, patch: LookPatch | None) -> AffectedShots:
        self.shot(shot_id)
        return self._set_patches("shot_overrides", [shot_id], char, patch)

    def replace_schedule(self, character_defaults: dict[str, str] | None,
                         episodes: dict[str, dict[str, LookPatch]] | None) -> AffectedShots:
        valid = {e.number for e in self.script.episodes}
        cd = {resolve_character(c): v for c, v in (character_defaults or {}).items()}
        for c, lid in cd.items():
            self._check_patch(c, LookPatch(look_id=lid))
        eps: dict[str, dict[str, LookPatch]] = {}
        cur = self.schedule()
        for k, chars in (episodes or {}).items():
            if not k.isdigit() or int(k) not in valid:
                raise ValueError(f"unknown episode: {k}")
            eps[str(int(k))] = {}
            for c, p in chars.items():
                c = resolve_character(c)
                self._check_patch(c, p)
                # 确认字段不从 body 接收；解析内容没变的沿用现有确认（内容哈希仍会再核一次）
                prev = cur.episodes.get(str(int(k)), {}).get(c)
                keep = prev is not None and all(getattr(prev, f) == getattr(p, f) for f in RESOLUTION_FIELDS)
                eps[str(int(k))][c] = p.model_copy(update={f: getattr(prev, f) if keep else None
                                                           for f in OWNER_FIELDS})

        def fn():
            s = self.schedule()
            if character_defaults is not None:
                s.character_defaults = cd
            if episodes is not None:
                s.episodes = eps
            self._write("schedule", s)
        return self._mutate(fn)


_default_store: LookStore | None = None


def default_store() -> LookStore:
    global _default_store
    if _default_store is None:
        _default_store = LookStore()
    return _default_store


def effective_look_prompt(shot_id: str, store: LookStore | None = None) -> LookPrompt:
    """
    某一镜当前生效造型（默认 → 集计划 → 本镜覆盖）贡献的提示词片段 + LoRA 列表。只读，不触发渲染。

    返回 LookPrompt：
        fragment  如 "【林晚造型】素颜"（多角色用“；”连接；已在镜头原文/角色锁里的描述不重复）
        triggers  造型单品的 LoRA 触发词（可能与 shot.img_triggers 重复）
        loras     [LoraRef(file, strength)]，strength 取单品强度区间上限（与 compiler 一致）
        characters  逐角色明细（已存在 / 本镜不可见而跳过的单品）
    身体装饰（when_in_shot）只在该镜 img 触发词/关键词命中、且可见性不是 hidden/in_box 时才进片段。

    接入提示词编译器（ns_agent.compiler.compile_shot）的方法（尚未接入）：
        lp = effective_look_prompt(shot.id)
        triggers = shot.img_triggers + [t for t in lp.triggers if t not in shot.img_triggers]
        → 与 shot 触发词一样按 available_loras 过滤（LoRA 文件不存在就去掉触发词，按 file 去重）
        image_prompt = _join(kept_triggers, *locks, lp.fragment, img_body, image_style)
    即：片段放在角色锁之后、镜头原文之前；视频提示词不需要（视频沿用首帧）。
    take 记录渲染时的 ShotLooks.look_hash（store.shot_looks(id).look_hash），与当前不同即“旧造型”。
    """
    return (store or default_store()).look_prompt(shot_id)


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    store = default_store()
    if argv[:1] == ["seed"]:
        from . import cast_table as CT
        from . import asset_refs as AR
        p = CT.current_project()
        if p and AR.manifest_rel(p):              # OVERSIGHT-F P2：seed 会塞 V1 林晚的脸 / 触发词；资产包模式（V2）的造型只认清单
            print(f"拒绝：{p} 是资产包模式，造型和身份脸只认 {AR.manifest_rel(p)}，不用 looks seed（会带进 V1 内容）")
            return 2
        store.seed(force="--force" in argv)
        w, b, s = store.wardrobe(), store.lookbook(), store.schedule()
        print(f"seeded {store.dir}: {len(w.items)} items, {len(w.decor_states)} decor states, {len(b.looks)} looks, "
              f"{sum(p.needs_review for e in s.episodes.values() for p in e.values())} episode plans need review")
        return 0
    if argv[:1] == ["confirm"] and len(argv) >= 4:
        eps = store.confirm_episodes(argv[1], argv[2], argv[3])
        print(f"已记录项目方确认：{argv[2]} ep{eps[0]}–ep{eps[-1]}（{len(eps)} 集）")
        return 0
    if argv[:1] == ["show"] and len(argv) > 1:
        print(json.dumps(store.shot_looks(argv[1]).model_dump(), ensure_ascii=False, indent=2))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
