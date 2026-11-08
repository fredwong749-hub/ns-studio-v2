"""
gpu3 渲染侧字段（state_timeline 的附加字段；格式见 docs/state_timeline.md「gpu3 渲染侧字段」）。

纯文字规则、确定性：只读剧本原文（img / motion），不碰渲染。
    props           每镜道具 + 状态（worn | hanging | held | on_table），ep05-S1 束腰挂墙 vs 触发词=穿上 的教训
    changes         每角色镜内换装（脱下 / 解开 / 拉下 / 披上 …）→ items_end / visibility_end（尾帧用）
    framing         景别（extreme_close | close | medium | full）+ faces_visible
    light           场景 / 段的光线（time / key_direction / colour_temp），认不出为 null
    cut_type        段与段之间的切法（hard | time_skip | flashback）
    signature       每角色每镜穿戴签名（排序后单品 id 的哈希）
拿不准的一律标 *_source: "guess"。
"""
import hashlib
import re

from ns_schema.looks import CHARACTERS

# ------------------------------------------------------------------ 公共

CLAUSE_SPLIT = re.compile(r"[，,。；;：:！!？?（）()\[\]【】／/]|——|…")
FEMALE = {"lin_wan"}                                   # V1 旧表；运行时经 _female() / _names() / _lead()（cast_table，P1-5b）
NAMES = {v: k for k, v in CHARACTERS.items()}          # 林晚 → lin_wan …


def _v2() -> bool:
    from . import asset_refs as AR
    from . import cast_table as CT
    p = CT.current_project()
    return bool(p and AR.manifest_rel(p))


def _female() -> set:
    from . import cast_table as CT
    return CT.female_ids() if _v2() else FEMALE


def _names() -> dict:
    from . import cast_table as CT
    return CT.char_ids() if _v2() else NAMES


def _lead() -> str | None:
    from . import cast_table as CT
    return CT.lead_id() if _v2() else "lin_wan"


def clauses(text: str) -> list[tuple[int, str]]:
    """→ [(起点, 分句)]"""
    out, pos = [], 0
    for m in CLAUSE_SPLIT.finditer(text):
        if m.start() > pos:
            out.append((pos, text[pos:m.start()]))
        pos = m.end()
    if pos < len(text):
        out.append((pos, text[pos:]))
    return out


def _clause_at(text: str, i: int) -> tuple[int, str]:
    for st, c in clauses(text):
        if st <= i < st + len(c):
            return st, c
    return 0, text


def who(text: str, i: int, present: list[str]) -> tuple[str | None, str]:
    """位置 i 处的东西属于谁：紧挨的“X的” > 本分句里 i 之前最近的人名 / 代词 > 默认（林晚在场 → 林晚）。
    → (slug, "text" | "guess")"""
    st, c = _clause_at(text, i)
    before = text[st:i]
    fem, names, lead = _female(), _names(), _lead()
    male = next((p for p in present if p not in fem), None)
    refs = [(n, names[n]) for n in names] + [("她", lead), ("他", male), ("男性", male), ("女性", lead)]
    m = re.search(r"(" + "|".join(re.escape(n) for n, _ in refs) + r")的[^，,。的]{0,8}$", before)
    if m:
        slug = dict(refs)[m.group(1)]
        if slug:
            return slug, "text"
    best = None
    for n, slug in refs:
        j = before.rfind(n)
        if slug and j >= 0 and (best is None or j > best[0]):
            best = (j, slug)
    if best:
        return best[1], "text"
    if lead in present:
        return lead, "guess"
    return (present[0] if present else None), "guess"


def _h12(ids) -> str:
    return hashlib.sha256("|".join(sorted(set(ids))).encode()).hexdigest()[:12]


# ------------------------------------------------------------------ 1. 道具

# 通用道具词表（衣橱单品以外）：(词, 槽位 或 None=物件)。长词在前
GARMENT_WORDS: tuple[tuple[str, str], ...] = (
    ("西装外套", "outfit"), ("男式西装", "outfit"), ("长风衣", "outfit"), ("风衣", "outfit"), ("外套", "outfit"),
    ("大衣", "outfit"), ("白衬衫", "outfit"), ("衬衫", "outfit"), ("睡袍", "outfit"), ("浴袍", "outfit"),
    ("浴巾", "outfit"), ("睡衣", "outfit"), ("睡裙", "outfit"), ("连衣裙", "outfit"), ("超短裙", "outfit"),
    ("裙子", "outfit"), ("束腰", "outfit"), ("内衣", "outfit"), ("丝袜", "other"), ("高跟鞋", "shoes"),
    ("细高跟", "shoes"),
)
OBJECT_WORDS: tuple[str, ...] = (
    "文件夹", "手机", "汤碗", "图纸", "蓝图", "酒杯", "香槟", "咖啡杯", "钥匙", "丝绒盒", "丝绒小盒", "小盒",
    "妆匣", "项链", "私记本", "笔记本", "毛巾", "手包", "合同", "托盘", "珠串", "首饰盒",
)
# 状态线索（同一分句内，离道具最近的线索胜出；同距离按此顺序）
STATE_CUES: tuple[tuple[str, str], ...] = (
    ("hanging", r"挂在|挂着|挂了|挂回|挂上|悬挂|墙上|衣架|衣钩|挂(?![断号])"),
    ("on_table", r"桌上|桌角|桌面|茶几上|台面|放在|放到|摆在|搁在|平铺|扣在|放回|摊开在"),
    ("held", r"拿着|拿起|拿出|拿|握着|握|手里|手中|抱着|抱紧|怀里|捧着|捧|拎着|拎|攥|举着|举起|递|取出|取下|端着|端起"),
    ("worn", r"穿着|穿上|穿(?![孔过透梭行])|戴着|戴上|戴|系上|系着|系紧|身上|披上|披着|披|裹着|裹上|裹|套上|一身|罩着|扣到领口"),
)
_CUE_RE = [(st, re.compile(p)) for st, p in STATE_CUES]
PROP_EXCLUDE_SLOTS = {"makeup", "hair", "nails"}


def _clean_name(name: str) -> str:
    return re.sub(r"[（(].*?[)）]", "", (name or "").split("：")[0]).strip()


def prop_candidates(wardrobe: dict) -> list[dict]:
    """衣橱单品（不含妆/发/甲、纹身、不可摘件）的检测词：keywords + 名称（去括号，≥2 字）"""
    out = []
    for it in wardrobe.values():
        if it.slot in PROP_EXCLUDE_SLOTS or "纹身" in it.name or not it.removable:
            continue
        words = list(it.keywords)
        nm = _clean_name(it.name)
        if len(nm) >= 2 and nm not in words:
            words.append(nm)
        refs = [i.file for i in getattr(it, "images", [])] + [r for r in it.reference_images if "/" in r]
        out.append({"id": it.id, "name": nm or it.name, "description": it.description or it.name, "slot": it.slot,
                    "trigger": it.trigger, "character": it.character,
                    "words": sorted(set(words), key=len, reverse=True),
                    "ref_image": refs[0] if refs else None,
                    # 带触发词 / 有关键词的单品优先（同一段文字只算一次）
                    "rank": (0 if it.trigger else 1 if it.keywords else 2)})
    return sorted(out, key=lambda c: (c["rank"], c["id"]))


def find_mentions(text: str, cands: list[dict]) -> list[tuple[int, int, dict]]:
    """→ [(起, 止, 道具)]，不重叠；衣橱单品先占，再通用词表"""
    taken: list[tuple[int, int]] = []
    hits: list[tuple[int, int, dict]] = []

    def free(a, b):
        return all(b <= x or a >= y for x, y in taken)

    pool = [(w, c) for c in cands for w in c["words"]]
    pool += [(w, {"id": f"prop:{w}", "name": w, "description": w, "slot": s, "trigger": None, "ref_image": None})
             for w, s in GARMENT_WORDS]
    pool += [(w, {"id": f"prop:{w}", "name": w, "description": w, "slot": None, "trigger": None, "ref_image": None})
             for w in OBJECT_WORDS]
    # 衣橱优先，其次长词优先
    order = sorted(range(len(pool)), key=lambda k: (0 if not pool[k][1]["id"].startswith("prop:") else 1,
                                                     -len(pool[k][0]), k))
    for k in order:
        w, c = pool[k]
        for m in re.finditer(re.escape(w), text):
            if free(m.start(), m.end()):
                taken.append((m.start(), m.end()))
                hits.append((m.start(), m.end(), c))
    return sorted(hits, key=lambda h: h[0])


def state_near(text: str, a: int, b: int) -> str | None:
    """同一分句内离 [a,b) 最近的状态线索（前面的优先：距离 +0；后面的 +1）"""
    st, c = _clause_at(text, a)
    best = None
    for state, rx in _CUE_RE:
        for m in rx.finditer(c):
            ms, me = st + m.start(), st + m.end()
            if a <= ms < b:          # 线索在道具词里面（“衣架”本身）不算
                continue
            d = (a - me) * 2 if me <= a else (ms - b) * 2 + 1
            if best is None or d < best[0]:
                best = (d, state)
    return best[1] if best else None


def shot_props(shot, img: str, motion: str, cands: list[dict], present: list[str], chars: dict,
               prev_props: dict[str, dict] | None) -> list[dict]:
    """本镜道具：img（首帧）里写到的 + 本镜触发词对应的单品；motion 给 state_end"""
    found: dict[str, dict] = {}
    for a, b, c in find_mentions(img, cands):
        st = state_near(img, a, b)
        e = found.get(c["id"])
        if e is None:
            e = found[c["id"]] = {"c": c, "state": st, "pos": a, "mention": img[a:b], "in": "img"}
        elif e["state"] is None and st:
            e["state"], e["pos"] = st, a
    for a, b, c in find_mentions(motion, cands):
        st = state_near(motion, a, b)
        e = found.get(c["id"])
        if e is None:
            found[c["id"]] = {"c": c, "state": None, "pos": None, "mention": motion[a:b], "in": "motion",
                              "end": st, "end_pos": a}
        elif st and "end" not in e:
            e["end"], e["end_pos"] = st, a
    by_trig = {c["trigger"]: c for c in cands if c["trigger"]}
    for t in shot.img_triggers:
        c = by_trig.get(t)
        if c and c["id"] not in found and not any(e["c"]["name"] == c["name"] for e in found.values()):
            found[c["id"]] = {"c": c, "state": None, "pos": None, "mention": t, "in": "trigger"}
    # 通用词（“束腰”“风衣”）并入同镜 / 本段 / 造型里名称包含它的衣橱单品，或同镜更长的通用词（外套 → 西装外套）
    known = {e["c"]["id"]: e["c"]["name"] for e in found.values() if not e["c"]["id"].startswith("prop:")}
    known.update({k: v["name"] for k, v in (prev_props or {}).items() if not k.startswith("prop:")})
    known.update({i["id"]: i["name"] for ch in chars.values() if ch["present"]
                  for lst in ch["items"].values() for i in lst})
    for pid in [k for k in found if k.startswith("prop:")]:
        w = found[pid]["c"]["name"]
        tgt = next((k for k, nm in known.items() if w in nm), None) or next(
            (k for k, e in found.items() if k != pid and k.startswith("prop:") and w in e["c"]["name"]
             and w != e["c"]["name"]), None)
        if not tgt:
            continue
        e = found.pop(pid)
        if tgt in found:
            t = found[tgt]
            if t["state"] is None and e["state"]:
                t["state"], t["pos"] = e["state"], e["pos"]
            if "end" not in t and "end" in e:
                t["end"] = e["end"]
        else:
            c = next((c for c in cands if c["id"] == tgt), None) or \
                {"id": tgt, "name": known.get(tgt, tgt), "description": known[tgt], "slot": e["c"]["slot"], "trigger": None,
                 "ref_image": None}
            found[tgt] = dict(e, c=c)

    out = []
    for pid, e in found.items():
        c = e["c"]
        # 首饰的“挂”= 挂在身上（挂珠 / 阴环挂铁珠串），除非分句写了墙 / 衣架
        for k, txt, pk in (("state", img, "pos"), ("end", motion, "end_pos")):
            if c["slot"] == "accessory" and e.get(k) == "hanging" and e.get(pk) is not None                     and not re.search(r"墙|衣架|衣钩|架上|钩上", _clause_at(txt, e[pk])[1]):
                e[k] = "worn"
        state, src = e["state"], "text"
        if not state and prev_props and pid in prev_props:
            state, src = prev_props[pid]["state_end"], "inherited"
        if not state:
            state, src = ("worn" if c["slot"] else "held"), "guess"
        end, end_src = e.get("end"), "text"
        if not end:
            end, end_src = state, src
        holder, hsrc = None, None
        owner = next((sl for sl, ch in chars.items() if ch["present"]
                      for lst in ch["items"].values() for i in lst if i["id"] == pid), None)
        if state in ("worn", "held") and present:
            if owner and state == "worn":
                holder, hsrc = owner, "look"         # 造型里就有这件 → 归造型的主人
            elif c.get("character") and state == "worn":
                holder, hsrc = c["character"], "wardrobe"   # 专属单品（gc-shirt → 顾沉），即使出场检测漏了他
            elif e["pos"] is not None:
                holder, hsrc = who(img, e["pos"], present)
            else:
                holder, hsrc = (_lead(), "guess") if _lead() in present else (present[0], "guess")
        in_look = any(i["id"] == pid for ch in chars.values() if ch["present"]
                      for lst in ch["items"].values() for i in lst)
        p = {"id": pid, "name": c["name"], "description": c["description"], "slot": c["slot"],
             "state": state, "state_source": src, "state_end": end, "state_end_source": end_src,
             "holder": holder, "holder_source": hsrc, "mention": e["mention"], "found_in": e["in"],
             "trigger": c["trigger"], "ref_image": c["ref_image"],
             # 触发词 / 造型会把它画成“穿着”，原文却是挂着 / 拿着 / 放着 → 渲染侧须按 state 处理
             "trigger_in_shot": bool(c["trigger"] and c["trigger"] in shot.img_triggers), "in_look": in_look,
             "trigger_conflict": bool(c["trigger"] and c["trigger"] in shot.img_triggers and state != "worn"),
             "look_conflict": in_look and state != "worn"}
        out.append(p)
    return out


# ------------------------------------------------------------------ 2. 镜内换装

CHANGE_VERBS: tuple[tuple[str, str], ...] = (
    (r"脱下|脱掉|脱去|褪下|褪去|扯下|摘下|卸下|脱(?=了|下|掉|去)", "removed"),
    (r"解开|解(?=暗扣|扣|纽扣|开)|拉开(?=拉链|衣|外套|风衣|睡袍|浴袍)", "opened"),
    (r"拉下|褪到|拉到(?=腰|膝|脚)", "lowered"),
    (r"掀起|撩起|撩开", "lifted"),
    (r"穿上|套上|戴上|系上|披上|换上|裹上|罩住|裹(?=浴袍|浴巾|睡袍|风衣)|披(?=衣)", "worn"),
)
_CHANGE_RE = [(re.compile(p), to) for p, to in CHANGE_VERBS]
FASTENERS = ("暗扣", "纽扣", "扣子", "拉链", "衣领")
GARMENT_PARTS = {"裙摆": "裙", "下摆": "", "衣摆": ""}


def _garment_in(clause: str, st: int, vpos: int, vend: int, cands: list[dict]):
    """分句里离动词最近的衣物（动词后的优先）。→ (起点, 道具) 或 None"""
    best = None
    for a, b, c in find_mentions(clause, cands):
        if not c["slot"]:
            continue
        a2 = st + a
        d = (a2 - vend) * 2 if a2 >= vend else (vpos - (st + b)) * 2 + 1
        if best is None or d < best[0]:
            best = (d, a2, c)
    return (best[1], best[2]) if best else None


def _match_item(word_or_cand: dict, char: dict) -> str | None:
    """文字里的衣物 → 本角色当前造型里的单品 id（名称 / 关键词包含该词）"""
    w = word_or_cand["name"]
    for lst in char.get("items", {}).values():
        for i in lst:
            if word_or_cand["id"] == i["id"] or w in i["name"] or w in (i.get("description") or ""):
                return i["id"]
    return None


def shot_changes(img: str, motion: str, cands: list[dict], present: list[str], chars: dict,
                 props: list[dict] | None = None) -> dict[str, list[dict]]:
    """→ {slug: [{slot, item, from, to, text, verb, found_in, source}]}"""
    out: dict[str, list[dict]] = {}
    seen = set()
    for where, text in (("motion", motion), ("img", img)):
        for st, cl in clauses(text):
            for rx, to in _CHANGE_RE:
                for m in rx.finditer(cl):
                    vpos, vend = st + m.start(), st + m.end()
                    g = _garment_in(cl, st, vpos, vend, cands)
                    src = "text"
                    if g:
                        gpos, c = g
                        slug, wsrc = who(text, gpos, present)
                    else:
                        tail = cl[m.end():m.end() + 6] + cl[max(0, m.start() - 6):m.start()]
                        part = next((p for p in (*FASTENERS, *GARMENT_PARTS) if p in tail), None)
                        if not part:
                            continue            # 拉开窗帘 / 拉开车门：不是衣物
                        # 只写了扣子 / 下摆：归到本镜写到的外衣（没有就是 outfit 槽，单品未知）
                        both = img + "，" + motion
                        g2 = next(((a, c2) for a, b, c2 in find_mentions(both, cands) if c2["slot"] == "outfit"), None)
                        c = g2[1] if g2 else {"id": None, "name": part, "slot": "outfit"}
                        src = "guess"
                        slug, wsrc = who(both, g2[0], present) if g2 else who(text, vpos, present)
                    if not slug:
                        continue
                    char = chars.get(slug, {})
                    item = _match_item(c, char) or next(
                        (p["id"] for p in props or [] if not p["id"].startswith("prop:")
                         and (p["id"] == c.get("id") or c["name"] in p["name"])), None)
                    if item is None and c.get("id") and not str(c["id"]).startswith("prop:"):
                        item = c["id"]
                    key = (slug, c["slot"], item or c["name"], to)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.setdefault(slug, []).append({
                        "slot": c["slot"], "item": item, "garment": c["name"],
                        "from": "absent" if to == "worn" else "worn", "to": to,
                        "verb": m.group(0), "text": cl.strip(), "found_in": where,
                        "source": "guess" if "guess" in (src, wsrc) else "text"})
    return out


def reconcile(props: list[dict], changes: dict[str, list[dict]]) -> None:
    """换装写到的道具：脱下 → 首帧 worn；穿上 → 尾帧 worn（就地修改 props）"""
    by_id = {p["id"]: p for p in props}
    by_name = {p["name"]: p for p in props}
    for slug, lst in changes.items():
        for ch in lst:
            p = by_id.get(ch["item"]) if ch["item"] else by_name.get(ch["garment"])
            if not p:
                continue
            ch.setdefault("prop", p["id"])
            if ch["to"] in ("removed", "opened", "lowered", "lifted"):
                p.update(state="worn", state_source="text", holder=slug, holder_source=ch["source"])
                if ch["to"] == "removed" and p["state_end"] == "worn":
                    p.update(state_end="held", state_end_source="guess")
            elif ch["to"] == "worn":
                if p["state"] == "worn":          # 首帧还没穿上（挂着 / 放着的照原文）
                    p.update(state="held", state_source="guess")
                p.update(state_end="worn", state_end_source="text", holder=slug, holder_source=ch["source"])
            p["trigger_conflict"] = p["trigger_in_shot"] and p["state"] != "worn"
            p["look_conflict"] = p["in_look"] and p["state"] != "worn"


BOX_IN = re.compile(r"(放进|收进|装进|放回|收回)[^，。]{0,6}(盒|匣)")
BOX_OUT = re.compile(r"(盒|匣)[^，。]{0,8}(取出|拿出)|戴上")


def end_state(char: dict, changes: list[dict], wardrobe: dict, img: str, motion: str) -> dict:
    """→ {items_end, visibility_end, visibility_end_source, signature, signature_end}"""
    items_end = {sl: [i["id"] for i in lst] for sl, lst in char.get("items", {}).items()}
    for ch in changes:
        if not ch["item"]:
            continue
        lst = items_end.setdefault(ch["slot"], [])
        if ch["to"] == "removed" and ch["item"] in lst:
            lst.remove(ch["item"])
        elif ch["to"] == "worn" and ch["item"] not in lst and ch["item"] in wardrobe:
            lst.append(ch["item"])
    vis = char.get("visibility") or "visible"
    vend, vsrc = vis, "same"
    if char.get("present"):
        if BOX_IN.search(motion):
            vend, vsrc = "in_box", "text"
        elif vis == "in_box" and BOX_OUT.search(motion):
            vend, vsrc = "visible", "text"
        elif vis == "hidden" and any(c["slot"] == "outfit" and c["to"] in ("removed", "opened", "lowered", "lifted")
                                     for c in changes):
            vend, vsrc = "visible", "guess"
    return {"items_end": items_end, "visibility_end": vend, "visibility_end_source": vsrc}


SIGNATURE_SLOTS = ("outfit", "shoes", "accessory", "other")


def signature(items: dict[str, list[str]], adornments: list[dict], worn_props: list[str]) -> str:
    """穿戴签名：outfit/shoes/accessory/other 单品 + 在身上的装饰 + 本镜写明穿着的道具，排序后哈希（12 位）"""
    ids = [i for sl in SIGNATURE_SLOTS for i in items.get(sl, [])]
    ids += [a["id"] for a in adornments if a.get("worn")]
    return _h12(ids + list(worn_props))


# ------------------------------------------------------------------ 3. 景别 / 可见脸

FRAMING_WORDS: tuple[tuple[str, str], ...] = (
    ("extreme_close", r"极近特写|大特写|超近特写|巨细特写|极近|微距|眼部特写|瞳孔特写"),
    ("close", r"近特写|手部特写|面部特写|脸部特写|特写|近景|近距离|面部"),
    ("medium", r"中近景|中景|半身|腰部以上|双人构图|双人|三人同框|过肩|侧影"),
    ("full", r"大全景|全景|远景|全身|俯瞰|长镜|背影全景"),
)
_FRAMING_RE = re.compile("|".join(f"(?P<{k}>{p})" for k, p in FRAMING_WORDS))
GUESS_CLOSE = re.compile(r"(?:的|她|他)(?:手|指尖|脚|足弓)(?![机腕臂])|对话框|屏幕|搜索框|手机界面|聊天记录")
FACE_WORDS = re.compile(r"脸|眼|面容|面孔|瞳|唇|嘴|睫毛|表情|眉|五官|神情|笑|侧颜|泪")
BODY_WORDS = re.compile(r"手|指尖|指甲|脚|足|高跟|腰|胸|腿|背|颈|锁骨|臀|耳|唇|漆皮|表面|屏幕")
BACK_RE = re.compile(r"背影|背对镜头|背对|背着路")


def shot_framing(img: str, motion: str) -> tuple[str, str]:
    """img 原文里最先出现的景别词；没有就看 motion；都没有按内容猜（中景）"""
    for text in (img, motion):
        m = _FRAMING_RE.search(text)
        if m:
            return m.lastgroup, "text"
    if GUESS_CLOSE.search(img[:40]):
        return "close", "guess"
    if BACK_RE.search(img) or re.search(r"走|站立|全身", img):
        return "full", "guess"
    return "medium", "guess"


def faces_visible(img: str, framing: str, present: list[str]) -> tuple[list[str], str]:
    faces, src = list(present), "guess"
    if not present:
        return [], "text"
    if "剪影" in img:
        return [], "text"
    if framing in ("extreme_close", "close"):
        if not FACE_WORDS.search(img):
            return [], ("text" if BODY_WORDS.search(img) else "guess")
        src = "text"
    for m in BACK_RE.finditer(img):
        slug, _ = who(img, m.start(), present)
        if slug in faces:
            faces.remove(slug)
            src = "text"
    for m in re.finditer(r"画外", img):
        near = img[m.end():m.end() + 6]
        for n, slug in _names().items():
            if n in near and slug in faces and slug != _lead():
                faces.remove(slug)
                src = "text"
    return faces, src


# ------------------------------------------------------------------ 4. 光线

KEY_DIRECTIONS: tuple[tuple[str, str], ...] = (
    ("top", r"顶光|射灯|头顶|吊灯|天花板灯|聚光"),
    ("side", r"侧光|斜照|斜射|侧面打光|侧逆光|半边脸"),
    ("back", r"逆光|背光|从背后打亮|背后打亮|剪影|轮廓光"),
    ("window", r"窗光|窗外|落地窗|玻璃幕墙|百叶窗"),
    ("front", r"正面光|迎面|屏幕蓝光|屏幕光|照亮(?:她|他)的脸|映脸"),
)
COLOUR_TEMPS: tuple[tuple[str, str], ...] = (
    ("warm", r"暖黄|暖光|暖色|台灯|烛光|烛|夕阳|晚霞|金色|路灯|橘"),
    ("cool", r"冷光|冷白|蓝光|月光|荧光|冷色|冷调|霓虹|屏幕光|蓝调"),
    ("neutral", r"日光|白光|自然光|晨光|阳光"),
)


def _vote(texts: list[str], table) -> str | None:
    counts: dict[str, int] = {}
    for t in texts:
        for k, p in table:
            if re.search(p, t):
                counts[k] = counts.get(k, 0) + 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda kv: (-kv[1], [k for k, _ in table].index(kv[0])))[0][0]


def light_of(texts: list[str], time_of_day: str) -> dict:
    return {"time": time_of_day or None, "key_direction": _vote(texts, KEY_DIRECTIONS),
            "colour_temp": _vote(texts, COLOUR_TEMPS)}


# ------------------------------------------------------------------ 5. 切法

FLASHBACK = ("闪回", "回忆", "梦境")
TIME_SKIP = ("time", "次日", "第二天", "翌日", "数日后", "几天后", "一周后", "当晚")


def cut_type(transition: str) -> str:
    if transition in FLASHBACK:
        return "flashback"
    if transition in TIME_SKIP or transition.startswith("周"):
        return "time_skip"
    return "hard"          # episode / place / 转场
