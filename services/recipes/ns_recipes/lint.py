"""RenderPlan 静态校验（只查格式 / 参数一致性，不评内容）。level：error = 不能提交；warn = 提示写作方 / 总调度"""
from __future__ import annotations

import re

from . import graph as G
from .prompt import FIELDS, I2VA_LINE, REF_FIELDS

NEG_WORDS_RE = re.compile(r"\b(no|not|never|without|avoid|don't|doesn't|nothing|none|nobody|neither|nor)\b", re.I)


def _issue(level, code, msg):
    return {"level": level, "code": code, "message": msg}


def lint_prompt(text: str, mode: str, recipe: dict, spec=None, frames: int | None = None) -> list[dict]:
    out = []
    p = recipe.get("prompt", {})
    if mode == "ref2va":
        heads = [m.group(1) for m in re.finditer(r"^([a-z_]+):", text, re.M)]
        want = list(REF_FIELDS)
        if [h for h in heads if h in want] != want:
            out.append(_issue("error", "RL-R1", f"Ref2VA 六段顺序不对：{heads}"))
        m = re.search(r"^detailed_description:(.*?)(?=^overall_soundscape:)", text, re.M | re.S)
        n = len((m.group(1) if m else "").split())
        lo, hi = p.get("words_min"), p.get("words_max")
        if lo and n < lo:
            out.append(_issue("warn", "RL-R2", f"detailed_description {n} 词，官方建议 {lo}–{hi}（写作方补充细节）"))
    else:
        first = text.splitlines()[0] if text else ""
        if mode == "i2va" and first != I2VA_LINE:
            out.append(_issue("error", "RL-A1", "I2VA 第一行不是官方指令句"))
        if mode == "fl2va" and not first.startswith("How the reference pictures align"):
            out.append(_issue("error", "RL-A1", "FL2VA 第一行不是官方指令句"))
        if mode == "i2va" and re.search(r"\bPicture 2\b", text):
            out.append(_issue("error", "RL-A4", "I2VA 正文仍引用 Picture 2（没有送尾帧）"))
        if mode == "i2va" and re.search(r"(?<!<)\bPicture 1\b(?!>)", text):
            out.append(_issue("warn", "RL-A5", "I2VA 里 Picture 1 没写成 <Picture 1>"))
        heads = [m.group(1) for m in re.finditer(r"^([a-z_]+):", text, re.M)]
        if heads != list(FIELDS):
            out.append(_issue("error", "RL-A2", f"三字段不全或顺序不对：{heads}"))
    sm = re.search(r"^overall_soundscape:[ \t]*(.*)$", text, re.M)
    if p.get("require_soundscape") and (not sm or not sm.group(1).strip() or sm.group(1).strip() == "N/A"):
        out.append(_issue("error", "RL-S1", "overall_soundscape 为空（H3 不会处理静音，MiniMax HF #76）"))
    if p.get("period_before_d_close"):
        for m in re.finditer(r"<d>(.*?)</d>", text, re.S):
            if not re.search(r"[。！？!?.…—~～」”\"]\s*$", m.group(1)):
                out.append(_issue("warn", "RL-D1", f"</d> 前没有句末标点：…{m.group(1)[-12:]}（交写作方）"))
    outside = re.sub(r"<d>.*?</d>", " ", text, flags=re.S)
    if p.get("no_negative") and mode != "ref2va":
        body = outside.split("\n", 1)[-1]
        neg = sorted({w.lower() for w in NEG_WORDS_RE.findall(body)})
        if neg:
            out.append(_issue("warn", "RL-N1", f"<d> 外有否定词 {neg}（官方：否定会把概念写进去；交写作方）"))
    if len(text) > int(p.get("max_chars", 7000)):
        out.append(_issue("error", "RL-A12", f"提示词 {len(text)} 字符，超过 {p.get('max_chars')}"))
    if "off-screen voiceover" in text and "lips remain completely closed" not in text:
        out.append(_issue("warn", "RL-V1", "画外音 <d> 后缺官方句 “… lips remain completely closed.”（交写作方）"))
    if spec is not None:
        for c in spec.visible:
            if c.en_phrase and c.en_phrase.strip().rstrip(".") not in text:
                out.append(_issue("warn", "RL-X1", f"{c.name} 的人物表 EN外貌短语没有逐字出现在正文（交写作方）"))
        out += lint_people_eyeline(text, spec, p)
        if p.get("timeline", True) and frames:
            out += timeline_for_spec(text, spec, frames, int(recipe.get("frames", {}).get("fps", 24)))
        speakers = spec.on_screen_speakers
        mx = p.get("max_speakers")
        if mx and len(speakers) > mx:
            out.append(_issue("warn", "RL-D7", f"本镜画内说话人 {len(speakers)} 个，配方上限 {mx}"))
    return out


def lint_graph(wf: dict, recipe: dict, frames: int, w: int, h: int) -> list[dict]:
    out = []
    banned = set(recipe["sampling"].get("banned_nodes") or [])
    for nid, n in wf.items():
        if n["class_type"] in banned:
            out.append(_issue("error", "RL-G1", f"节点 {nid} {n['class_type']} 在禁用列表"))
    out += [_issue("error", "RL-G2", e) for e in G.dangling_links(wf)]
    fr = recipe["frames"]
    if (frames - 5) % 17 or not fr["min"] <= frames <= fr["max"]:
        out.append(_issue("error", "RL-G3", f"帧数 {frames} 不在 17k+5 网格或超出 {fr['min']}–{fr['max']}"))
    if w % 32 or h % 32:
        out.append(_issue("error", "RL-G4", f"分辨率 {w}×{h} 不是 32 的倍数"))
    mp = w * h / 1_000_000
    if mp > float(recipe["resolution"].get("max_megapixels_3090", 99)) + 1e-9:
        out.append(_issue("error", "RL-G5", f"{w}×{h} = {mp:.2f} MP 超过 3090 上限"))
    loras = [lo["file"].lower() for lo in G.enabled_loras(recipe).values()]
    turbo = [x for x in loras if "turbo" in x]
    shift = tuple(recipe["sampling"]["shift"])
    if turbo and all("768p" in x for x in turbo) and shift != (6.0, 3.0):
        out.append(_issue("error", "RL-G6", "768p Turbo LoRA 必须 shift 6/3"))
    if turbo and any("pika" in x for x in loras):
        out.append(_issue("error", "RL-G7", "Pika Flex 不能叠 Turbo"))
    if any(k in recipe["sampling"]["sampler"].lower() for k in ("ancestral", "sde")):
        out.append(_issue("error", "RL-G8", "H3 不用 ancestral / SDE 采样器"))
    return out


# ---------------------------------------------------------------------------------------------
# 时间轴（sysimpl-v2，ep02 v1 项目方 bug：切换生硬 / 表情互动生硬 / 时间轴粗 / 节奏慢）
# 只查格式与槽位是否齐（有没有时间、有没有写视线 / 表情 / 对方反应 / 衔接 / 结尾动作），不评写了什么。
# ---------------------------------------------------------------------------------------------
STAMP_RE = re.compile(r"\bAt (\d{1,2}):(\d{2}(?:\.\d{1,3})?),\s*")
# 自然语言分拍（备选写法：防官方切镜时间码被 H3 当成镜内硬切）：“At the 2-second mark,” / “At the 4.5-second mark,”
STAMP_NL_RE = re.compile(r"\bAt the (\d{1,2}(?:\.\d{1,2})?)-second mark,\s*")


# L-141（项目方 11-03 03:2x 拍板 ISS-6b8d：按官方写法来）：`At MM:SS.mmm` 只用于切镜（`[Shot N] At 00:03.500, the camera cuts to…`，
# base-en §4.2）；镜内分拍改写自然英文的时间顺序：第一拍 “In the first two seconds, …”，之后每拍 “Then, around the third second, …”
# （序数 = 该拍起点所在的第几秒，起点 t0 → 第 ⌊t0⌋+1 秒），最后一拍写到 “… as the shot ends”。
ORDINALS = {w: i for i, w in enumerate(("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth",
                                        "tenth", "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth"), 1)}
NUM_WORDS = {w: i for i, w in enumerate(("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"), 1)}
_ORD = r"(" + "|".join(ORDINALS) + r"|\d{1,2}(?:st|nd|rd|th))"
NL_BEAT_RE = re.compile(r"\b(?:[Tt]hen,?\s+)?(?:[Aa]round|[Bb]y)\s+the\s+" + _ORD + r"\s+second\b,?\s*")
NL_FIRST_RE = re.compile(r"\b[Ii]n the first (" + "|".join(NUM_WORDS) + r"|\d{1,2}(?:\.\d)?) seconds?\b,?\s*")
CUT_RE = re.compile(r"\[Shot (\d+)\]\s*At (\d{1,2}):(\d{2}(?:\.\d{1,3})?),")
BEAT_MARK_PAT = (r"\bAt (?:\d{1,2}:\d{2}|the \d)|\b(?:[Tt]hen,?\s+)?(?:[Aa]round|[Bb]y)\s+the\s+" + _ORD + r"\s+second\b")


def _ord(s: str) -> int:
    s = s.lower()
    return ORDINALS.get(s) or int(re.match(r"\d+", s).group(0))


def nl_beats(text: str) -> list[tuple[int, int, float]]:
    """镜内自然语言分拍标记 → [(start, end, 该拍起点秒 = 序数 − 1)]"""
    return [(m.start(), m.end(), float(_ord(m.group(1)) - 1)) for m in NL_BEAT_RE.finditer(text)]


def find_stamps(text: str) -> list[tuple[int, int, float]]:
    """→ [(start, end, 秒)]：拍界都认——自然语言分拍（L-141 现行）、`At 00:SS.mmm,`（切镜；V1 旧稿镜内也这么写）、`At the N-second mark,`"""
    out = [(m.start(), m.end(), int(m.group(1)) * 60 + float(m.group(2))) for m in STAMP_RE.finditer(text)]
    out += [(m.start(), m.end(), float(m.group(1))) for m in STAMP_NL_RE.finditer(text)]
    out += nl_beats(text)
    return sorted(out)


def in_shot_timecodes(text: str) -> list[str]:
    """不在 `[Shot N]` 后面的 `At MM:SS.mmm` / `At the N-second mark`（L-141：镜内不许用 At 时间码）"""
    cut_pos = {m.start(2) - 3 for m in CUT_RE.finditer(text)}
    bad = [m.group(0).strip() for m in STAMP_RE.finditer(text) if m.start() not in cut_pos]
    return bad + [m.group(0).strip() for m in STAMP_NL_RE.finditer(text)]


GAZE_RE = re.compile(r"\b(eyes?|gaze[sd]?|gazing|look(?:s|ed|ing)?|glanc\w*|eyeline|star(?:e|es|ed|ing)|watch\w*|"
                     r"peer(?:s|ed|ing)?|pupils?)\b", re.I)
EXPR_RE = re.compile(r"\b(brows?|eyebrows?|lips?|mouth|jaw|eyelids?|lids|lashes|blink\w*|smil\w*|smirk\w*|frown\w*|"
                     r"swallow\w*|grimac\w*|purs\w*|bit(?:e|es|ing)|nostrils?|chin|wince\w*|squint\w*|"
                     r"expression)\b", re.I)
OPEN_RE = re.compile(r"\b(already|still|continu\w*|mid-\w+|keeps?)\b", re.I)
SETTLE_RE = re.compile(r"\b(settl\w*|holds? the pose|freez\w*|remains? still|stays? still|comes? to rest|"
                       r"stops? moving|motionless)\b", re.I)
ONGOING_END_RE = re.compile(r"\b(as the shot ends|still \w+ing|continues? to|keeps? \w+ing)\b", re.I)
SLOW_RE = re.compile(r"\b(slowly|gently|gradually|softly|lingering|languid\w*|leisurely|at slow speed)\b", re.I)
LISTEN_CLOSED_RE = re.compile(r"lips? (?:stay|stays|remain|remains|are|kept|pressed)?\s*(?:closed|shut|together)|"
                              r"closed lips|with (?:her|his) lips closed", re.I)
CUES = {"f": re.compile(r"\b(she|her|hers|herself|woman|girl)\b", re.I),
        "m": re.compile(r"\b(he|him|his|himself|man|guy)\b", re.I)}
P2_SENT_RE = re.compile(r"[^.!?]*\bPicture 2\b[^.!?]*[.!?]\s*$")
BEAT_MIN, BEAT_MAX, BEAT_HARD = 1.0, 2.6, 3.1
WORDS_PER_S_MIN = 15


def _sex(phrase: str | None) -> str | None:
    p = (phrase or "").lower()
    if re.search(r"\b(woman|girl|female|lady)\b", p):
        return "f"
    if re.search(r"\b(man|boy|male|guy)\b", p):
        return "m"
    return None


def split_beats(body: str, duration: float) -> list[dict]:
    """正文 → [{t0, t1, text}]：第一拍 = [Shot 1] 到第一个 At 时间；之后每个 `At 00:SS.mmm,` 一拍。
    FL2VA 末句（settles into … Picture 2）不算进最后一拍。"""
    b = re.sub(r"<d>.*?</d>", " <d/> ", body or "", flags=re.S)
    b = P2_SENT_RE.sub("", b.strip())
    stamps = find_stamps(b)
    out, pos, t = [], 0, 0.0
    for s, e, ts in stamps:
        out.append({"t0": t, "t1": ts, "text": b[pos:s]})
        pos, t = e, ts
    out.append({"t0": t, "t1": float(duration), "text": b[pos:]})
    return out


def lint_timeline(text: str, duration: float, *, sexes: list[str] | None = None, strict: bool = True,
                  allow_slow: bool = False, faces: bool = True) -> list[dict]:
    """sysimpl-v2 时间轴校验。strict=True（写作时用的是 sysimpl-v2）→ 缺项为 error（block）；否则只 warn。
    sexes：本镜在画、看得见脸的角色性别（'f' / 'm'），≥2 个不同性别时检查每拍有没有写到对方。
    faces=False：本镜没有露脸角色（手部 / 局部 / 空镜），不查视线和表情槽位。"""
    lv = "error" if strict else "warn"
    out = []
    m = re.search(r"(?:integrated_multimodal_description|detailed_description):\s*(.*?)(?=^\s*overall_soundscape:|\Z)",
                  text or "", re.S | re.M)
    body = (m.group(1) if m else text or "").strip()
    stamps = [x[2] for x in find_stamps(re.sub(r"<d>.*?</d>", "", body, flags=re.S))]
    if not stamps:
        out.append(_issue(lv, "RL-T1", f"正文没有按秒分拍（{duration:.2f}s 镜头应每 1.5–2 秒一拍：In the first two seconds, … Then, around the third second, …；L-141）"))
    else:
        if any(b <= a for a, b in zip(stamps, stamps[1:])) or stamps[0] <= 0:
            out.append(_issue(lv, "RL-T1", f"分拍时间不是严格递增或从 0 开始：{stamps}"))
        if stamps[-1] >= duration:
            out.append(_issue(lv, "RL-T1", f"分拍时间 {stamps[-1]:.2f}s 超过镜长 {duration:.2f}s"))
    beats = split_beats(body, duration)
    for i, bt in enumerate(beats):
        span = bt["t1"] - bt["t0"]
        tag = f"第 {i + 1} 拍 {bt['t0']:.1f}–{bt['t1']:.1f}s"
        if stamps and span > BEAT_HARD:
            out.append(_issue(lv, "RL-T2", f"{tag} 长 {span:.1f}s，超过 {BEAT_MAX}s（每 1.5–2 秒一拍）"))
        elif stamps and span > BEAT_MAX:
            out.append(_issue("warn", "RL-T2", f"{tag} 长 {span:.1f}s，建议 ≤ 2 秒"))
        elif stamps and 0 < span < BEAT_MIN and i < len(beats) - 1:
            out.append(_issue("warn", "RL-T2", f"{tag} 只有 {span:.1f}s，太碎"))
        tx = bt["text"]
        if faces and not GAZE_RE.search(tx):
            out.append(_issue(lv, "RL-T3", f"{tag} 没写视线落点（eyes / gaze / looks / glances …）"))
        if faces and not EXPR_RE.search(tx):
            out.append(_issue(lv, "RL-T3", f"{tag} 没写表情变化（brows / lips / mouth / jaw / blink / smile …）"))
        ss = sorted(set(sexes or []))
        if len(ss) >= 2 and not all(CUES[s].search(tx) for s in ss):
            out.append(_issue(lv, "RL-T4", f"{tag} 双人镜没写到另一位在画角色的可见反应"))
        if len(ss) >= 2 and "<d/>" in tx:
            nxt = beats[i + 1]["text"] if i + 1 < len(beats) else ""
            if not (LISTEN_CLOSED_RE.search(tx) or LISTEN_CLOSED_RE.search(nxt)):
                out.append(_issue("warn", "RL-T4", f"{tag} 台词拍没写听者 lips closed（防嘴型串到听者）"))
    if beats:
        first = beats[0]["text"]
        sents = re.split(r"(?<=[.!?])\s+", first.strip())
        if not OPEN_RE.search(" ".join(sents[1:]) or first):
            out.append(_issue("warn" if not strict else "error", "RL-T5",
                              "开头没有衔接句（锚点句后写第 0 秒已在进行的动作：already / still / continues / mid-）"))
        last = beats[-1]["text"]
        if SETTLE_RE.search(last):
            out.append(_issue(lv, "RL-T6", f"最后一拍写了定住类词 {SETTLE_RE.search(last).group(0)!r}（结尾要留仍在进行的动作）"))
        elif not ONGOING_END_RE.search(last):
            out.append(_issue("warn", "RL-T6", "最后一拍没写到镜尾仍在进行的动作（… as the shot ends）"))
    slow = sorted({w.lower() for w in SLOW_RE.findall(re.sub(r"<d>.*?</d>", " ", body, flags=re.S))})
    if slow and not allow_slow:
        out.append(_issue("warn", "RL-T7", f"正文有偏慢词 {slow}（项目方要求正常偏快节奏）"))
    nw = len(re.sub(r"<d>.*?</d>", " ", body, flags=re.S).split())
    if duration and nw / duration < WORDS_PER_S_MIN:
        out.append(_issue("warn", "RL-T7", f"正文 {nw} 词 / {duration:.1f}s = {nw / duration:.0f} 词每秒，动作量偏少（建议 18–25）"))
    return out


def timeline_for_spec(text: str, spec, frames: int, fps: int = 24, strict: bool | None = None) -> list[dict]:
    """compile 用：镜长取实际帧数 / fps；strict 默认看 Shot Spec 的写作规则版本是否含 sysimpl-v2 及以上"""
    if strict is None:   # 开关 config/recipes/timing_lint.json（默认 warn）+ 写作规则 ≥ sysimpl-v2
        from ns_schema.shot_spec import rules_at_least
        strict = spec is not None and rules_at_least(spec, 2)
    sexes = [s for s in (_sex(c.en_phrase) for c in (spec.faces if spec is not None else [])) if s]
    slow_ok = bool(spec is not None and spec.camera.speed == "slow")
    faces = spec is None or bool(spec.faces)
    out = lint_timeline(text, frames / fps, sexes=sexes, strict=strict, allow_slow=slow_ok, faces=faces)
    if spec is not None and strict:      # L-141 的拍序 / 切镜规则只对 strict（V2 / 新剧）生效，V1 旧稿不动
        out += timeline_alignment(text, spec, strict=strict)
    return out


# ---------------------------------------------------------------------------------------------
# TIMELINE-O 11-03 / L-141（项目方 03:2x 拍板 ISS-6b8d：按官方写法来）：
# 官方 base-en §4.2 “Do not add a timestamp to the first shot… begin each one with a strictly increasing cut time”——
# `At MM:SS.mmm` 只用于切镜（`[Shot N] At …, the camera cuts to …`）。镜内分拍用自然英文写时间顺序，
# 拍序 / 拍数必须和剧本 **分拍** 一一对上（RL-T8）；镜内出现 At 时间码、切镜缺 At 或不递增（RL-T9）。
# ---------------------------------------------------------------------------------------------
from ns_schema.shot_spec import ALIGN_TOL_S  # noqa: E402,F401  写手包同一份常量（自然语言序数按秒取整，对齐容差见 NL_TOL_S）
NL_TOL_S = 1.0     # “around the third second” = 第 3 秒（2–3 s）；该拍起点与序数所指秒的起点相差 ≤ 1 秒
NEXT_CUT_RE = re.compile(r"\[Shot (\d+)\](?!\s*At \d{1,2}:\d{2})")


def h3_body(text: str) -> str:
    m = re.search(r"(?:integrated_multimodal_description|detailed_description):\s*(.*?)(?=^\s*overall_soundscape:|\Z)",
                  text or "", re.S | re.M)
    return (m.group(1) if m else text or "").strip()


def _ord_word(t0: float) -> str:
    k = int(t0) + 1
    return next((w for w, i in ORDINALS.items() if i == k), f"{k}th")


def nl_example(beats) -> str:
    """按 **分拍** 生成的拍序示例（写手包 / 报错提示用）"""
    if not beats:
        return ""
    first = beats[0].t1
    n = next((w for w, i in NUM_WORDS.items() if i == round(first)), f"{first:g}")
    return " … ".join([f"In the first {n} seconds, …"] + [f"Then, around the {_ord_word(b.t0)} second, …" for b in beats[1:]]
                      + ["… as the shot ends."])


def cut_issues(body: str, lv: str) -> list[dict]:
    """RL-T9：镜内不许有 At 时间码；`[Shot N≥2]` 必须以 `At MM:SS.mmm,` 开头，时间严格递增（官方 §4.2）"""
    out = []
    bad = in_shot_timecodes(body)
    if bad:
        out.append(_issue(lv, "RL-T9", f"镜内用了 At 时间码 {bad[:3]}（L-141：At MM:SS.mmm 只用于切镜，镜内分拍写 In the first … / Then, around the … second）"))
    for m in NEXT_CUT_RE.finditer(body):
        if int(m.group(1)) >= 2:
            out.append(_issue(lv, "RL-T9", f"[Shot {m.group(1)}] 切镜没写 `At MM:SS.mmm,`（官方 §4.2）"))
    cuts = [int(m.group(2)) * 60 + float(m.group(3)) for m in CUT_RE.finditer(body)]
    if any(b <= a for a, b in zip(cuts, cuts[1:])) or (cuts and cuts[0] <= 0):
        out.append(_issue(lv, "RL-T9", f"切镜时间不是严格递增：{cuts}"))
    return out


def timeline_alignment(text: str, spec, strict: bool = True) -> list[dict]:
    """RL-T8（L-141）：H3 镜内自然语言分拍 ↔ 剧本 **分拍**：第一拍 “In the first N seconds”（N ≈ 第一拍止），
    之后每拍 “around / by the <序数> second”，个数 = 分拍数 − 1、序数严格递增、所指秒起点与该拍 t0 相差 ≤ NL_TOL_S；
    最后一拍写到 “as the shot ends”。外加 RL-T9 切镜规则。剧本没写 **分拍** / H3 没有分拍 = 缺时间轴"""
    lv = "error" if strict else "warn"
    beats = sorted(getattr(getattr(spec, "motion", None), "beats", None) or [], key=lambda b: b.t0)
    body = re.sub(r"<d>.*?</d>", " ", h3_body(text), flags=re.S)
    if not beats:
        return [_issue(lv, "RL-T8", f"缺时间轴：剧本没有 **分拍**（{spec.duration_s:g}s 镜头每 1.5–2 秒一拍，动作 / 运镜 / 视线 / 表情都写起止）")]
    out = cut_issues(body, lv)
    marks = nl_beats(body)
    want = [b.t0 for b in beats[1:]]
    fm = NL_FIRST_RE.search(body)
    if not marks and not fm:
        return out + [_issue(lv, "RL-T8", f"缺时间轴：H3 正文没有按 **分拍** 写镜内时间顺序（{len(beats)} 拍，应写：{nl_example(beats)}）")]
    if not fm or (marks and fm.start() > marks[0][0]):
        out.append(_issue(lv, "RL-T8", f"第一拍没写 “In the first … seconds,”（应写：{nl_example(beats)}）"))
    else:
        g = fm.group(1).lower()
        n = NUM_WORDS.get(g) or float(g)
        if abs(n - beats[0].t1) > NL_TOL_S + 1e-6:
            out.append(_issue(lv, "RL-T8", f"第一拍写 In the first {g} seconds，**分拍** 第一拍到 {beats[0].t1:g} 秒"))
    ts = [x[2] for x in marks]
    if len(ts) != len(want):
        out.append(_issue(lv, "RL-T8", f"H3 镜内分拍 {len(ts) + 1} 拍，**分拍** {len(beats)} 拍（应写：{nl_example(beats)}）"))
    else:
        if any(b <= a for a, b in zip(ts, ts[1:])):
            out.append(_issue(lv, "RL-T8", f"镜内分拍序数不是严格递增：{[int(t) + 1 for t in ts]}"))
        bad = [(a, b) for a, b in zip(ts, want) if abs(a - b) > NL_TOL_S + 1e-6]
        if bad:
            out.append(_issue(lv, "RL-T8", "镜内分拍和 **分拍** 起点对不上：" + "；".join(
                f"around the {_ord_word(a)} second ↔ 分拍 {b:g}s（应写 {_ord_word(b)}）" for a, b in bad)))
    tail = body[marks[-1][1]:] if marks else body
    if "as the shot ends" not in tail:
        out.append(_issue(lv, "RL-T8", "最后一拍没写到 “… as the shot ends”"))
    return out


def missing_timeline(text: str, spec) -> str | None:
    """编译用（失败关闭）：缺时间轴 / 与分拍对不上 → 原因；齐了 → None"""
    iss = [i for i in timeline_alignment(text, spec, strict=True) if i["level"] == "error"]
    return "；".join(i["message"] for i in iss) or None


def timeline_gate(spec) -> list[dict]:
    """导入闸门 / 补丁环用：本镜 H3 时间轴全部 strict 检查（RL-T1–T7 + RL-T8 对齐），只返回 error。
    只对 rules_at_least(spec, 2)（V2 / 新剧且稿件 ≥ sysimpl-v2）的镜头生效"""
    from ns_schema.shot_spec import rules_at_least
    if not rules_at_least(spec, 2):
        return []
    text = (spec.h3.text or "") if getattr(spec, "h3", None) else ""
    if not text.strip():
        return []      # 缺 H3 由 SS-H1 报
    out = timeline_for_spec(text, spec, spec.frames, 24, strict=True)
    return [i for i in out if i["level"] == "error"]


# ACTING 11-01（项目方 R11-9 原话：“当画面出现超过一个人,第二个等,他们面部表情,眼神是没有,这是剧本提示词的错误,写入剧本镜头规则
# 按照minimax h3的镜头提示词写”）：多人露脸镜，H3 正文里视线短语 / 表情短语的个数都要 ≥ 露脸人数（官方 ref-en 多人例：每人一句
# “Her annoyance softens as she looks toward …”）。系统固定提示句（prompt.eyeline_cue / open_motion_cue / min_motion_cue）不计。
# 粗检（按个数，不按人归属）：warn，交写作方（QW-W9 是写作侧逐人检查）。RL-E2：H3 正文写了看镜头 / 看观众
GAZE_EN_RE = re.compile(r"\b(?:eyes?|gaze|gazes|gazing|looks?|looking|glances?|glancing|stares?|staring|watch(?:es|ing)?|eyeline)\b", re.I)
EXPR_EN_RE = re.compile(r"\b(?:smil\w*|grin\w*|frown\w*|brows?|lips?|jaw|expression|scowl\w*|blush\w*|tears?|"
                        r"narrow\w*|soften\w*|tighten\w*|flinch\w*|wince\w*|smirk\w*|lashes|pout\w*|mouth)\b", re.I)
CAMERA_GAZE_RE = re.compile(r"\b(?:look(?:s|ing)?|star(?:es|ing)|gaz(?:es|ing)|glanc(?:es|ing))\s+(?:straight\s+|directly\s+)?"
                            r"(?:in(?:to)?|at|toward)\s+(?:the\s+)?(?:camera|lens|viewer|audience)\b|"
                            r"\b(?:eyes|gaze)\s+(?:on|to|toward|into)\s+(?:the\s+)?(?:camera|lens|viewer)\b|"
                            r"\beye contact with (?:the )?(?:camera|viewer|audience)\b", re.I)


SYS_CAMERA_FACING_RE = re.compile(r"\b(?:facing|faces|turned toward|turns toward|toward)\s+(?:the\s+)?(?:camera|lens|viewer)\b", re.I)



def system_facing_camera(text: str) -> str | None:
    """RL-E3 的派单期版本（K-OPS-Q24 扩展到提示词，AUDIT 20:15）：只看编译稿里的系统锚点句“The frame keeps …”，
    命中朝向镜头 → 返回命中片段。vidgen.PROMPT_FACING_RE 与本模块 SYS_CAMERA_FACING_RE 必须同一模式（测试核对）"""
    sys_txt = " ".join(re.findall(r"The frame keeps[^.]*\.", text or ""))
    m = SYS_CAMERA_FACING_RE.search(sys_txt)
    return m.group(0) if m else None

POSE_CHANGE_RE = re.compile(r"\b(rises?|rising|rose|stands? up|standing up|stood up|gets? up|getting up|got up|sits? down|sitting down|sat down)\b", re.I)
OUTER_EN_RE = re.compile(r"\b(blazer|jacket|coat|overcoat|trench|cardigan|suit jacket)\b", re.I)


def lint_people_eyeline(text: str, spec, p: dict) -> list[dict]:
    out = []
    m = re.search(r"^(?:integrated_multimodal_description|detailed_description):(.*?)(?=^overall_soundscape:)", text or "", re.M | re.S)
    body = m.group(1) if m else (text or "")
    for k in ("eyeline_cue", "open_motion_cue", "min_motion_cue", "outfit_cue"):
        c = (p.get(k) or "").strip()
        if c:
            body = body.replace(c, " ")
    body = re.sub(r"<d>.*?</d>", " ", body, flags=re.S)
    # VIDEO-RCA 11-01（RL-E3）：系统编译句（锚点句 / 站位词表）不得让人朝向镜头——写作方正文另由 RL-E2 管
    sys_txt = " ".join(re.findall(r"The frame keeps[^.]*\.", text or "")) + " " + " ".join(
        (p.get(k) or "") for k in ("eyeline_cue", "open_motion_cue", "min_motion_cue", "outfit_cue"))
    fc = SYS_CAMERA_FACING_RE.search(sys_txt)
    if fc:
        out.append(_issue("error", "RL-E3", f"编译稿含朝向镜头的系统句：“{fc.group(0)}”（K-REN-702：首帧 / 正文朝镜头 → 片头看镜头 49/54）"))
    # VIDEO-RCA r4（K-WRT-704 编译层）：在画人物穿外层衣物 + 分拍里有全身姿态切换（坐→站…）→ warn RL-P1，
    # 计划里标“建议拆镜”：这一镜停在准备起身，下一镜从站定开始（编译器不改镜头结构，交写作方 / DIRECTOR cut_plan）
    _bm = re.search(BEAT_MARK_PAT, body)       # 第一个拍界（L-141 自然语言 / 旧稿 At 00:）之后 = 分拍动作段
    beats_txt = body[_bm.start():] if _bm else ""
    if POSE_CHANGE_RE.search(beats_txt) and OUTER_EN_RE.search(body.split(". ", 1)[0]):
        out.append(_issue("warn", "RL-P1", f"穿外层衣物的人在本镜做“{POSE_CHANGE_RE.search(beats_txt).group(0)}”：外套会变形（VIDEO-RCA 10/10），"
                                           "建议拆成站定起始的下一镜（K-WRT-704 / QW-W12）"))
    cg = CAMERA_GAZE_RE.search(body)
    if cg:
        out.append(_issue("warn", "RL-E2", f"H3 正文写了看镜头：“{cg.group(0)}”（项目方 R11-2：演员不看镜头；交写作方）"))
    n = len(getattr(spec, "faces", None) or [])
    if n >= 2:
        g, e = len(GAZE_EN_RE.findall(body)), len(EXPR_EN_RE.findall(body))
        if g < n or e < n:
            out.append(_issue("warn", "RL-E1", f"多人镜 {n} 张露脸，H3 正文视线短语 {g} 个、表情短语 {e} 个，少于人数"
                                               f"（R11-9：每个在画的人都要写视线和表情；交写作方）"))
    return out
