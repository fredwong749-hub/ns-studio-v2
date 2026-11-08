"""Who says what: script_v2.json shots → spoken lines with a resolved speaker.

The importer keeps the script's notes verbatim (DialogueLine.speaker_note: "顾沉低声", "她，很小声", "他",
"合上盒子" ...). This module turns them into speaker ids (config/voices.json "characters"):

- a named character anywhere in the note wins by position ("画外，陈宇的招呼声" → chen_yu);
- 她 / 自言自语 / 自答 / 对镜 → lin_wan;
- 他 → the male in the shot: a named male in the same shot's notes, else in its prompts
  (img/motion/raw dialogue), else the last male of the episode, else 顾沉;
- a note without a speaker ("合上盒子", "停顿两秒", "一顿") continues the previous speaker;
- no note: a continuation of an unclosed quote (importer split on "／") keeps the speaker, otherwise turns
  alternate (A, B, A ...);
- notes the importer left inside the text ("（她，一口气）“……") are pulled out; pure stage text
  ("（沉默三秒）", "—", "（无——全镜无对白…）") is not spoken.

Voiceover (🎙): spoken by config/voices.json "voiceover_speaker" (VOICE 2026-10-31, owner chose the QAg narrator
voice; was 林晚's own voice before). line_overrides still win per line.
"""

import re
from dataclasses import asdict, dataclass, field

from .tts import default_voiceover_speaker

LEAD_NOTE = re.compile(r"^\s*[（(]([^（）()]*)[）)]\s*")
PAREN = re.compile(r"[（(][^（）()]*[）)]")
SPLIT_INLINE = re.compile(r"[（(]([^（）()]*)[）)]")

NAMED = (  # (token, speaker id); searched by position in the note
    ("顾沉", "gu_chen"), ("陈宇", "chen_yu"), ("林晚", "lin_wan"), ("阿岚", "ch_a_lan"),
    # RECALL 2026-11-01：旧 60 集 Alex/Ryan/Kai/Leo/四声齐 已撤下（config/voices.json 同步删除）
    # VOICE10 2026-11-01（G9 / R3，K-EDT-020）：旧剧称呼（穿孔师 / 纹身师 / 同事 / 店员）已隔离到下面的
    # LEGACY_60EP_TITLES，不再参与解析——voices.json 早已没有 piercer / tattooist / coworker / clerk，
    # 留在这里只会让写到这些称呼的句子报 TtsError。series10 剧本里这些称呼出现 0 次（script_v2.json 实测）。
)
# 隔离不删（G0：被取代的旧口径标明取代者）：旧 60 集配角称呼 → 旧音色 id。不被任何代码读取；
# 新剧要加配角，先在 config/voices.json 加音色条目，再把称呼加进 NAMED（tests/test_voice10_fix.py 校验 NAMED ⊆ voices.json）。
LEGACY_60EP_TITLES = (
    ("女穿孔师", "piercer"), ("穿孔师", "piercer"), ("纹身师", "tattooist"),
    ("男同事", "male_coworker"), ("同事", "coworker"), ("店员", "clerk"),
)  # ⤴ 被 series10 演员表（林晚 / 顾沉 / 陈宇 / 阿岚）取代
HER = ("她", "自言自语", "自答", "对镜")
HIM = ("他",)
MALES = {"gu_chen", "chen_yu"}
MALE_NAMES = (("顾沉", "gu_chen"), ("陈宇", "chen_yu"))
DEFAULT_MALE = "gu_chen"
LEAD = "lin_wan"  # female lead: 她 / unnamed turns in dialogue
SILENT_MARKERS = ("留白", "无对白", "无——")


@dataclass(frozen=True)
class Cast:
    """说话人表。V1 = 本文件的常量（顾沉 / 陈宇 / 林晚 / 阿岚）；V2 = 按剧的音色表 cfg["cast"]
    （ns_post.run_media.voice_config ← ns_agent.cast_table.speaker_cast ← P1 人物表），不另起表"""
    named: tuple = NAMED
    males: frozenset = frozenset(MALES)
    male_names: tuple = MALE_NAMES
    default_male: str | None = DEFAULT_MALE
    lead: str = LEAD
    lead_label: str = "林晚"
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_cfg(cls, cfg: dict | None) -> "Cast":
        """音色表里有 cast（V2）就用它；没有 = V1 常量"""
        c = (cfg or {}).get("cast")
        if not c:
            return V1_CAST
        names = sorted(((n, cid) for n, cid in c["names"].items() if not re.fullmatch(r"[a-z0-9_]+", n)),
                       key=lambda x: -len(x[0]))
        males = frozenset(c.get("male") or [])
        lead_name = next((n for n, cid in names if cid == c["lead"]), c["lead"])
        return cls(named=tuple(names), males=males, male_names=tuple((n, cid) for n, cid in names if cid in males),
                   default_male=None, lead=c["lead"], lead_label=lead_name, extra={"source": c.get("source")})


V1_CAST = Cast()


@dataclass(frozen=True)
class SpokenLine:
    kind: str  # dialogue | voiceover
    shot: int  # shot index (1-based)
    line: int  # 0-based index among the shot's spoken lines (dialogue); 0 for the voiceover
    speaker: str
    text: str  # cleaned for TTS
    note: str = ""  # speaker note / voiceover note, verbatim
    raw: str = ""  # script text as imported
    how: str = ""  # how the speaker was resolved (for the timeline)

    @property
    def key(self) -> str:
        return f"S{self.shot}#{self.line}" if self.kind == "dialogue" else f"S{self.shot}#vo"

    def to_dict(self) -> dict:
        return asdict(self)


def tts_text(text: str) -> str:
    """Script text → what the TTS reads: no quotes/markdown/stage parentheses, dashes become pauses."""
    t = PAREN.sub("", text or "")
    for a, b in (("“", ""), ("”", ""), ("‘", ""), ("’", ""), ('"', ""), ("*", ""), ("「", ""), ("」", ""),
                 ("\\", ""), ("～", "，"), ("~", "，")):
        t = t.replace(a, b)
    t = re.sub(r"—{1,}", "，", t)
    t = re.sub(r"…+|\.{3,}", "…", t)
    t = re.sub(r"[，,]\s*([。？！?!…])", r"\1", t)
    t = re.sub(r"([，。？！…])[，]+", r"\1", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = t.lstrip("，。、；：").strip()
    return t


def speakable(text: str) -> bool:
    if any(m in (text or "") for m in SILENT_MARKERS) and not re.search(r"[“\"]", text or ""):
        return False
    t = tts_text(text)
    return any(ch.isalnum() for ch in t)


def speaker_from_note(note: str | None, cast: Cast = V1_CAST) -> tuple[str | None, str]:
    """(speaker id or None for "he"/unknown, kind) where kind is named | her | him | none."""
    if not note:
        return None, "none"
    hits = []
    for token, sid in cast.named:
        i = note.find(token)
        if i >= 0:
            hits.append((i, -len(token), sid, "named"))
    for token in HER:
        i = note.find(token)
        if i >= 0:
            hits.append((i, -len(token), cast.lead, "her"))
    for token in HIM:
        i = note.find(token)
        if i >= 0:
            hits.append((i, -len(token), None, "him"))
    if not hits:
        return None, "none"
    _, _, sid, kind = min(hits)
    return sid, kind


def shot_males(shot: dict, cast: Cast = V1_CAST) -> list[str]:
    """Named males mentioned in the shot (notes first, then prompts), in order of first mention."""
    found: list[str] = []
    for line in shot.get("dialogue") or []:
        sid, kind = speaker_from_note(line.get("speaker_note"), cast)
        if kind == "named" and sid in cast.males and sid not in found:
            found.append(sid)
    text = " ".join(str(shot.get(k) or "") for k in ("dialogue_raw", "img_prompt", "motion_prompt"))
    hits = sorted((text.find(tok), sid) for tok, sid in cast.male_names if tok in text)
    for _, sid in hits:
        if sid not in found:
            found.append(sid)
    return found


def _segments(line: dict) -> list[tuple[str | None, str, str]]:
    """One importer line → [(note, text, raw)]: pulls a leading "（note）" out of the text and splits
    "（她）完了。（他）这是什么？" into two lines."""
    note, text = line.get("speaker_note"), line.get("text") or ""
    raw = text
    if note is None:
        m = LEAD_NOTE.match(text)
        if m:
            note, text = m.group(1).strip() or None, text[m.end():]
    parts = SPLIT_INLINE.split(text)
    # parts = [text0, note1, text1, note2, text2 ...]
    out = [(note, parts[0], raw)]
    for i in range(1, len(parts) - 1, 2):
        n, t = parts[i].strip(), parts[i + 1]
        sid, kind = speaker_from_note(n)
        if kind in ("named", "her", "him") and speakable(t):
            out.append((n, t, raw))
        else:  # a mid-line action ("然后笑倒"): stays with the current line
            note0, t0, r0 = out[-1]
            out[-1] = (note0, t0 + t, r0)
    return out


class UnresolvedSpeaker(ValueError):
    """V2：“他”找不到是谁（本镜 / 本集都没出现过男性人物）——不猜、不退回 V1 的顾沉"""


def resolve_episode(episode: dict, overrides: dict[str, str] | None = None,
                    narrator: str | None = None, cast: Cast | None = None) -> list[SpokenLine]:
    """All spoken lines of an episode in script order (per shot: dialogue lines, then the voiceover).
    narrator: voiceover speaker; None = config/voices.json "voiceover_speaker" (fallback lin_wan).
    cast: 说话人表；None = V1 常量。V2 由调用方传 Cast.from_cfg(按剧的音色表)。"""
    overrides = overrides or {}
    cast = cast or V1_CAST
    narrator = narrator or default_voiceover_speaker()
    ep = int(episode.get("number", 0))
    last_male = None
    lines: list[SpokenLine] = []
    for shot in episode.get("shots") or []:
        idx = int(shot["index"])
        males = shot_males(shot, cast)
        shot_male = males[0] if males else (last_male or cast.default_male)
        speakers: list[str] = []
        open_quote = False
        n = 0
        for line in shot.get("dialogue") or []:
            for note, text, raw in _segments(line):
                continuation = open_quote
                if "“" in text or "”" in text:
                    open_quote = text.count("“") > text.count("”") or (open_quote and "”" not in text)
                if not speakable(text):
                    continue
                sid, kind = speaker_from_note(note, cast)
                if kind == "named":
                    how = f"note:{note}"
                elif kind == "her":
                    how = f"note:{note}"
                elif kind == "him":
                    sid = shot_male
                    how = f"he→{sid} ({'shot' if males else 'episode/default'})"
                elif note and speakers:  # an action note: same speaker continues
                    sid, how = speakers[-1], f"action note '{note}' → previous speaker"
                elif continuation and speakers:
                    sid, how = speakers[-1], "continuation of the quote"
                elif len(speakers) >= 2 and speakers[-1] != speakers[-2]:
                    sid, how = speakers[-2], "alternating turns"
                elif speakers:
                    sid = shot_male if speakers[-1] == cast.lead else cast.lead
                    how = "alternating turns"
                else:
                    sid, how = cast.lead, f"no note: {cast.lead_label}"
                if sid is None:      # 只有 V2（default_male = None）会到这里
                    raise UnresolvedSpeaker(f"ep{ep:02d}-S{idx} 第 {n + 1} 句：“他”是谁判断不了（本镜 / 本集都没出现男性人物），"
                                            f"请在剧本说话人注里写名字，或在 line_overrides 指定")
                key = f"ep{ep:02d}-S{idx}#{n}"
                if key in overrides:
                    sid, how = overrides[key], "config line_overrides"
                if sid in cast.males:
                    last_male = sid
                speakers.append(sid)
                lines.append(SpokenLine("dialogue", idx, n, sid, tts_text(text), note or "", raw, how))
                n += 1
        vo = shot.get("voiceover")
        if vo and speakable(vo.get("text") or ""):
            key = f"ep{ep:02d}-S{idx}#vo"
            sid = overrides.get(key, narrator)
            lines.append(SpokenLine("voiceover", idx, 0, sid, tts_text(vo["text"]), vo.get("note") or "",
                                    vo["text"], f"voiceover = {narrator} (voiceover_speaker)" if key not in overrides
                                    else "config line_overrides"))
    return lines
