"""
H3 FL2VA 提示词（SOP_production.md §9A.1 四块结构）的纯函数构建器。

来源优先级（S1-W7–W9）：剧本 `**H3提示词**`（Shot.h3_prompt，原样）> 本构建器的笔记本草稿。
构建器不翻译、不新写描述：
- 正文英文只来自调用方传入的 body_en（画布修改 / 将来剧本 v3 的英文正文）；没有就把剧本 motion 中文原文放进结构里，
  lint 会报 H3-A7「需要英文正文（剧本 v3 提供）」。
- overall_soundscape 来自 soundscape 参数（编译器按 画布 > 剧本【H3 audio】> 剧本 sound 行 给出）。
- non_diegetic_music（项目方 2026-10-30：进 H3，指南 §4.7）：本镜人工 > 剧本 **H3提示词** 自带（非 N/A）> 本集默认 > N/A；
  只收英文（或 N/A），中文配乐提示只给后期，不翻译（见 resolve_music）。
- 对白：(S1)/(S2) 按首次开口顺序编号，说话人说明在 <d> 外，<d>[Chinese] 剧本原文逐字</d>；画外台词按 H3-D5；
  🎙 内心独白永远不进（H3-D6）。不加引号，不加负面词，不拼图像风格后缀。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ns_schema.h3_lint import ALIGN_PREFIX, FIELDS, VO_PHRASE, alignment_line, frames_for_seconds, parse_h3

MUSIC = "N/A"
# 剧本说话人括号里表示“人不在画面里出声”的写法（H3-D5）
OFFSCREEN_MARKS = ("画外", "电话", "语音", "OS", "V.O", "VO")
# FIX（CHAIN F7，官方 base-en §4.4 Case）：画外台词的嘴唇句紧跟 </d>：“… <d>…</d> while her/his lips remain
# completely closed.”。画面里的人是谁、男女，兜底构建器不知道 → 默认用中性主语；调用方给 on_screen_pronoun 时用 her/his
LIPS_CLOSED = "while the on-screen character's lips remain completely closed."


def lips_closed(pronoun: str | None = None) -> str:
    return f"while {pronoun} lips remain completely closed." if pronoun in ("her", "his") else LIPS_CLOSED


@dataclass
class H3Draft:
    text: str
    body: str
    soundscape: str
    music: str
    frames: int
    source: str                     # script | laptop
    english_body: bool = False
    english_soundscape: bool = False
    missing: list[str] = field(default_factory=list)   # 缺英文的块：body / soundscape
    speakers: dict[str, str] = field(default_factory=dict)   # 说话人 key → S1/S2…
    offscreen: list[str] = field(default_factory=list)       # 画外台词原文（给 lint H3-D5）


_CJK = re.compile(r"[㐀-鿿]")


def _english(text: str) -> bool:
    outside = re.sub(r"<d>.*?</d>", " ", text or "", flags=re.S)
    return bool(outside.strip()) and not _CJK.search(outside)


def h3_music_ok(text: str | None) -> bool:
    """能直接放进 non_diegetic_music 的文字：N/A 或不含中文的一段英文"""
    t = (text or "").strip()
    return bool(t) and (t == "N/A" or (not _CJK.search(t) and "\n" not in t and "<d>" not in t))


# 剧本【配乐】提示里表示“本镜不要配乐”的写法（规则：强度 0 写 N/A）
SILENT_CUE_RE = re.compile(r"强度\s*0(?![.\d])|撤到静默|静默|无配乐")


def episode_music_ok(text: str) -> tuple[bool, str]:
    """本集【BGM】英文能否回退进 non_diegetic_music：过 H3-S3（1–3 句、一段英文、无抽象情绪词）"""
    from ns_schema.h3_lint import MOOD_EN_RE, _sentences
    if not h3_music_ok(text) or text.strip() == "N/A":
        return False, "不是一段英文"
    n = len(_sentences(text))
    moods = sorted({m.group(0).lower() for m in MOOD_EN_RE.finditer(text)})
    if not 1 <= n <= 3:
        return False, f"{n} 句（H3-S3 要 1–3 句）"
    if moods:
        return False, f"有抽象情绪词 {'、'.join(moods)}（H3-S3）"
    return True, ""


LIPS_RE = re.compile(r"\blips\b[^.]*\b(?:closed|sealed)\b", re.I)


def add_lips_closed(prompt: str) -> str:
    """项目方 2026-10-31 原话（独白镜加嘴唇闭合）“加”：纯独白镜（人在画、声音只有画外独白）在 H3 正文第一句末尾补
    官方写法 “, while the on-screen character's lips remain completely closed.”（编译器加，剧本文字不改）。
    放在第一句（锚定 Picture 1 的那句）而不是末句：只给首帧时末句的 Picture 2 分句会被去掉。已有嘴唇闭合句 → 原样"""
    p = parse_h3(prompt or "")
    body = p["fields"].get(FIELDS[0])
    if not body or LIPS_RE.search(re.sub(r"<d>.*?</d>", " ", body, flags=re.S)):
        return prompt
    masked = re.sub(r"<d>.*?</d>", lambda m: "x" * len(m.group(0)), body, flags=re.S)
    m = re.search(r"[.!?](?=\s|$)", masked)
    if not m:
        return prompt
    i = m.start()
    new_body = body[:i].rstrip(" ,") + ", " + LIPS_CLOSED + body[i + 1:]
    return replace_field(prompt, FIELDS[0], new_body)


def monologue_only(shot) -> bool:
    """纯独白镜：有 🎙 独白、没有台词"""
    vo = getattr(shot, "voiceover", None)
    has_vo = bool(vo is not None and (getattr(vo, "text", "") or "").strip())
    has_d = any((getattr(d, "text", "") or "").strip() for d in getattr(shot, "dialogue", None) or [])
    return has_vo and not has_d


def music_warnings(text: str | None) -> list[str]:
    """FIX（CHAIN F5）：剧本自带 / 人工写的配乐也过 H3-S3（只报 warn，不改文字）"""
    t = (text or "").strip()
    if not t or t == "N/A":
        return []
    ok, why = episode_music_ok(t)
    return [] if ok else [f"H3-S3：{why}"]


def resolve_music(shot_edit: str | None = None, script_music: str | None = None,
                  episode_default: str | None = None, cue: str | None = None) -> tuple[str, str]:
    """H3 配乐取值 → (text, source)。source = edit | script | cue_silent | episode | episode_invalid | none。
    - 本镜人工（英文或 N/A）优先；
    - 剧本 **H3提示词** 里写了 non_diegetic_music（包括明确的 N/A = 本镜不要配乐）就原样用；
    - 剧本【配乐】提示写了强度 0 / 撤到静默 → N/A；
    - 只有剧本完全没写配乐字段时，才回退本集【BGM】英文，且它必须过 H3-S3，不过就 N/A（source=episode_invalid，warn）。
    中文（剧本【配乐】后期提示、中文 BGM 行）不进 H3，也不翻译。"""
    if h3_music_ok(shot_edit):
        return shot_edit.strip(), "edit"
    if h3_music_ok(script_music):
        return script_music.strip(), "script"
    if any(x and SILENT_CUE_RE.search(x) for x in (shot_edit, cue)):   # 人工 / 剧本的中文后期提示写了强度 0 / 静默
        return MUSIC, "cue_silent"
    if episode_default and episode_default.strip() and episode_default.strip() != "N/A":
        ok, _why = episode_music_ok(episode_default.strip())
        return (episode_default.strip(), "episode") if ok else (MUSIC, "episode_invalid")
    return MUSIC, "none"


def assemble(frames: int, body: str, soundscape: str, music: str = MUSIC) -> str:
    """四块结构：对齐句、空行、三个字段（字段之间空一行）"""
    body = body.strip()
    if not body.startswith("[Shot 1]"):
        body = "[Shot 1] " + body
    return (f"{alignment_line(frames)}\n\n{FIELDS[0]}: {body}\n\n{FIELDS[1]}: {(soundscape or '').strip()}"
            f"\n\n{FIELDS[2]}: {music}")


def replace_field(text: str, name: str, value: str) -> str:
    """替换已有 H3 提示词里的一个字段值（其它部分原样保留）；不是 H3 结构就原样返回"""
    if not (text or "").startswith(ALIGN_PREFIX):
        return text
    if name == FIELDS[0] and not value.strip().startswith("[Shot 1]"):
        value = "[Shot 1] " + value.strip()
    pat = re.compile(rf"(?m)^({re.escape(name)}:[ \t]?)(.*?)(?=\n\n[A-Za-z_]+:|\Z)", re.S)
    return pat.sub(lambda m: m.group(1) + value.strip(), text, count=1)


def speaker_key(note: str | None, names: tuple[str, ...] = ()) -> str:
    """说话人归并：括号里出现的角色名优先；否则取开头的 她/他；都没有就用整段说明"""
    note = (note or "").strip()
    for n in names:
        if n and n in note:
            return n
    if note[:1] in ("她", "他"):
        return note[:1]
    return note or "?"


def is_offscreen(note: str | None, stage: str | None = None) -> bool:
    t = f"{note or ''}{stage or ''}"
    return any(m in t for m in OFFSCREEN_MARKS)


def dialogue_sentences(dialogue, names: tuple[str, ...] = (), voices: dict[str, str] | None = None,
                       on_screen_pronoun: str | None = None) -> tuple[list[str], dict[str, str], list[str]]:
    """剧本对白 → H3 句子（<d> 内只放 [Chinese] + 原文）。
    FIX（CHAIN F6，官方 §4.4）：<d> 外不再放剧本括号原文（中文舞台提示，“她慌 (S1) says:” 过不了 H3-A7）；
    说话人短语用人物表 VOICE 英文（voices = {角色名: VOICE短语}），查不到就只写 “(Sx) says:”，由 lint 报缺 VOICE"""
    ids: dict[str, str] = {}
    out, offscreen = [], []
    for d in dialogue or []:
        text = getattr(d, "text", None) if not isinstance(d, dict) else d.get("text")
        if not text or not text.strip():
            continue
        note = getattr(d, "speaker_note", None) if not isinstance(d, dict) else d.get("speaker_note")
        stage = getattr(d, "stage", None) if not isinstance(d, dict) else d.get("stage")
        key = speaker_key(note, names)
        sid = ids.setdefault(key, f"S{len(ids) + 1}")
        voice = (voices or {}).get(key, "").strip()
        lead = f"{voice} " if voice and not _CJK.search(voice) else ""
        if is_offscreen(note, stage):
            offscreen.append(text)
            out.append(f"{lead}({sid}) {VO_PHRASE}: <d>[Chinese] {text}</d> {lips_closed(on_screen_pronoun)}")
        else:
            out.append(f"{lead}({sid}) says: <d>[Chinese] {text}</d>")
    return out, ids, offscreen


def strip_triggers(text: str, triggers: list[str]) -> str:
    for t in triggers or []:
        text = text.lstrip()
        for sep in ("，", ",", " "):
            if text.startswith(t + sep):
                text = text[len(t) + 1:]
                break
        else:
            if text.startswith(t):
                text = text[len(t):]
    return text.strip()


def build(shot, soundscape: str, *, frames: int | None = None, body_en: str | None = None,
          names: tuple[str, ...] = (), music: str = MUSIC, voices: dict[str, str] | None = None) -> H3Draft:
    """笔记本构建器。shot：ns_schema.script.Shot（或同名属性的对象）。voices：人物表 VOICE 英文短语（CHAIN F6）"""
    frames = frames or frames_for_seconds(getattr(shot, "duration", 10) or 10)
    sents, ids, offscreen = dialogue_sentences(getattr(shot, "dialogue", None), names, voices)
    if body_en and body_en.strip():
        body = body_en.strip()
    else:
        motion = strip_triggers(getattr(shot, "motion_prompt", "") or "", getattr(shot, "motion_triggers", []) or [])
        body = " ".join([x for x in [motion.rstrip("。.") + "。" if motion else ""] + sents if x])
    if not body.startswith("[Shot 1]"):
        body = "[Shot 1] " + body
    text = assemble(frames, body, soundscape, music)
    eb, es = _english(body), _english(soundscape)
    return H3Draft(text=text, body=body, soundscape=(soundscape or "").strip(), music=music, frames=frames,
                   source="laptop", english_body=eb, english_soundscape=es,
                   missing=[k for k, ok in (("body", eb), ("soundscape", es)) if not ok],
                   speakers=ids, offscreen=offscreen)


def for_shot(shot, soundscape: str, *, soundscape_edited: bool = False, body_edit: str | None = None,
             names: tuple[str, ...] = (), music_edit: str | None = None,
             episode_music: str | None = None) -> H3Draft:
    cue = getattr(shot, "music_cue", "") or ""
    """本镜的 H3 提示词：剧本 **H3提示词** 原样（画布只可改正文 / 环境声 / 配乐），否则笔记本构建器"""
    frames = frames_for_seconds(getattr(shot, "duration", 10) or 10)
    script_text = (getattr(shot, "h3_prompt", None) or "").strip("\n")
    _, ids, offscreen = dialogue_sentences(getattr(shot, "dialogue", None), names)
    if script_text:
        text = script_text
        if body_edit and body_edit.strip():
            text = replace_field(text, FIELDS[0], body_edit)
        if soundscape_edited and soundscape:
            text = replace_field(text, FIELDS[1], soundscape)
        music, _ = resolve_music(music_edit, parse_h3(text)["fields"].get(FIELDS[2]), episode_music, cue)
        text = replace_field(text, FIELDS[2], music)
        f = parse_h3(text)["fields"]
        body, sc = f.get(FIELDS[0], ""), f.get(FIELDS[1], "")
        eb, es = _english(body), _english(sc)
        return H3Draft(text=text, body=body, soundscape=sc, music=f.get(FIELDS[2], ""), frames=frames,
                       source="script", english_body=eb, english_soundscape=es,
                       missing=[k for k, ok in (("body", eb), ("soundscape", es)) if not ok],
                       speakers=ids, offscreen=offscreen)
    return build(shot, soundscape, frames=frames, body_en=body_edit, names=names,
                 music=resolve_music(music_edit, None, episode_music, cue)[0])


def lint_inputs(shot) -> dict:
    """lint_h3 的可选参数（剧本侧事实）：seconds、dialogue、offscreen、monologue"""
    _, _, offscreen = dialogue_sentences(getattr(shot, "dialogue", None))
    vo = getattr(shot, "voiceover", None)
    return {"seconds": getattr(shot, "duration", 10) or 10,
            "dialogue": [d.text for d in getattr(shot, "dialogue", []) or [] if d.text and d.text.strip()],
            "offscreen": offscreen,
            "monologue": [vo.text] if vo is not None and getattr(vo, "text", "") else []}
