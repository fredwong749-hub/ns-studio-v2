"""Background music (BGM) layer and gpu3 audio stems for the episode mix.

BGM is a post-production track per episode (not H3). The file comes from gpu3 (task T40):
data/music/epXX.(wav|flac|mp3) + data/music/epXX.json (cue timeline, bpm, model, seed, sha256). No file →
the layer is skipped with a warning. Cue sources, highest priority first:

1. epXX.json "cues" (gpu3's timeline, composed together with the music — followed as given);
2. owner overrides: ShotEdit.music in data/renders/epXX/SN/edits.json (free text cue for that shot) —
   only without a json timeline; with one, the edit is reported as needing a BGM rebuild on gpu3;
3. the script: Shot.music_cue (v3 【配乐】 segment);
4. none at all: music enters at the start at intensity 1 and fades out at the end.
epXX.json "duck_spans" (H3 dialogue/vocal activity, absolute seconds) duck the BGM as well as the TTS lines.

A cue is {action, intensity 0–3, duck}. action: enter | continue | swell | drop | stinger. Text cues are
parsed from Chinese or English ("进入，强度2，压低", "swell to 3", "落下", "点题").

Gain: the music is first normalised to MUSIC_REF_DBFS RMS, then intensity sets its level
(INTENSITY_DB), ramps follow the cues (enter = fade in, swell = ramp across the shot, drop = fade out,
stinger = short accent), and it is ducked under every TTS line (dialogue deeper than voiceover; a cue's
duck flag ducks that shot further). Ducking is keyed on the planned speech spans (a deterministic
envelope, equivalent to a sidechain keyed on the TTS but without compressor pumping).
"""

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

MUSIC_EXTS = (".wav", ".flac", ".mp3")
MUSIC_REF_DBFS = -20.0  # RMS the BGM file is normalised to before intensity gains
INTENSITY_DB = {0: None, 1: -10.0, 2: -6.0, 3: -2.5}  # relative to MUSIC_REF_DBFS; 0 = silent
ACTIONS = ("enter", "continue", "swell", "drop", "stinger")
_ACTION_WORDS = (  # first match wins; the cue's head (text before "：") is tried before the whole text
    ("drop", ("撤到静默", "撤", "静默", "落下", "淡出", "drop", "fade out", "fade to silence", "silence", "stop")),
    ("stinger", ("stinger", "点题", "重音", "强调", "accent")),
    ("swell", ("swell", "推高", "推到高点", "渐强", "升起", "build", "rise")),
    ("enter", ("enter", "进入", "转换", "切入", "fade in")),
    ("continue", ("continue", "延续", "持续", "保持", "hold")),
)
NO_DUCK = re.compile(r"无需避让|不避让|不用避让|无需压低|不压|no[- ]?duck")
DUCK = re.compile(r"避让|压低|duck")


@dataclass
class Cue:
    shot: int | None = None
    start_s: float | None = None  # absolute episode time (from epXX.json); else the shot start
    action: str = "continue"
    intensity: int | None = None
    duck_db: float = 0.0  # extra ducking under speech in this shot
    fade_s: float | None = None
    offset_s: float | None = None  # relative to the shot start (epXX.json cues may use shot + offset_s)
    source: str = ""
    text: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _action(text: str) -> str | None:
    for name, words in _ACTION_WORDS:
        if any(w in text for w in words):
            return name
    return None


def parse_cue_text(text: str) -> Cue | None:
    """A free-text cue (script 【配乐】, ShotEdit.music): "撤到静默：…，强度0" → drop to silence;
    "…，强度2，对白避让" → duck 6 dB; "无需避让" → no duck."""
    t = (text or "").strip()
    if not t:
        return None
    low = t.lower()
    head = re.split(r"[：:，,]", low, maxsplit=1)[0]
    action = _action(head) or _action(low) or "continue"
    m = re.search(r"(?:强度|intensity|level)\s*[:：=]?\s*([0-3])", low) or re.search(r"(?:\bto\s+|→\s*)([0-3])\b", low)
    intensity = int(m.group(1)) if m else None
    if action == "drop" and intensity is None and ("静默" in low or "silence" in low):
        intensity = 0
    duck = 0.0
    if not NO_DUCK.search(low) and DUCK.search(low):
        m2 = re.search(r"(?:避让|压低|duck(?:ing)?)\s*[:：=]?\s*-?(\d+(?:\.\d+)?)\s*db", low)
        duck = float(m2.group(1)) if m2 else 6.0
    return Cue(action=action, intensity=intensity, duck_db=duck, text=t)


def _cue_from_json(d: dict) -> Cue:
    action = str(d.get("action") or d.get("type") or d.get("cue") or "continue").lower()
    if action not in ACTIONS:
        parsed = parse_cue_text(action)
        action = parsed.action if parsed else "continue"
    duck = d.get("duck_db", d.get("ducking", d.get("duck", 0.0)))
    if isinstance(duck, bool):
        duck = 6.0 if duck else 0.0
    elif isinstance(duck, str):
        p = parse_cue_text(duck)
        duck = p.duck_db if p else 0.0
    shot = d.get("shot")
    if isinstance(shot, str):
        m = re.search(r"S?(\d+)$", shot)
        shot = int(m.group(1)) if m else None
    start = d.get("start_s", d.get("time_s", d.get("t")))
    inten = d.get("intensity")
    return Cue(shot=shot, start_s=float(start) if start is not None else None, action=action,
               intensity=int(inten) if inten is not None else None, duck_db=abs(float(duck or 0.0)),
               fade_s=float(d["fade_s"]) if d.get("fade_s") is not None else None,
               offset_s=float(d["offset_s"]) if d.get("offset_s") is not None else None,
               source="music_json", text=json.dumps(d, ensure_ascii=False))


def find_music(ep: int, music_dir: Path) -> tuple[Path | None, dict]:
    name = f"ep{ep:02d}"
    f = next((music_dir / f"{name}{e}" for e in MUSIC_EXTS if (music_dir / f"{name}{e}").is_file()), None)
    meta = {}
    j = music_dir / f"{name}.json"
    if j.is_file():
        try:
            meta = json.loads(j.read_text(encoding="utf-8"))
        except ValueError:
            meta = {"_error": f"{j.name} is not valid JSON"}
    return f, meta


def read_edit_music(renders: Path, ep: int, idx: int | None = None) -> str | None:
    p = renders / f"ep{ep:02d}" / ("episode_edit.json" if idx is None else f"S{idx}/edits.json")
    if not p.is_file():
        return None
    try:
        return (json.loads(p.read_text(encoding="utf-8")) or {}).get("music") or None
    except ValueError:
        return None


def h3_music_shots(shots: list[dict], episode: dict, shot_edits: dict[int, str],
                   episode_edit: str | None = None) -> dict[int, str]:
    """哪些镜头的 H3 视频已经自带配乐（non_diegetic_music 不是 N/A）→ {镜号: 来源}。
    项目方 2026-10-30 定（X-4 / E8）：配乐写进 H3，后期对这些镜头不再叠加 BGM。
    取值规则与编译器同一个函数（ns_agent.h3_prompt.resolve_music）：本镜修改 > 剧本 H3 字段 > 静默提示 > 本集默认"""
    from ns_agent import h3_prompt as H3
    ep_default = episode_edit or episode.get("music_bgm") or ""
    out: dict[int, str] = {}
    for x in shots:
        idx = int(x["index"])
        script_music = H3.parse_h3(x["h3_prompt"])["fields"].get(H3.FIELDS[2]) if x.get("h3_prompt") else None
        text, source = H3.resolve_music(shot_edits.get(idx), script_music, ep_default, x.get("music_cue") or "")
        if text != H3.MUSIC:
            out[idx] = source
    return out


def stale_timeline(meta: dict, n_shots: int) -> str | None:
    """封板修复：epXX.json 的提示时间轴按另一版剧本（镜数不同）生成 → 返回原因，不能再按镜号套用"""
    js = meta.get("shot_starts") or meta.get("shot_durations") or []
    if meta.get("cues") and js and len(js) != n_shots:
        return f"cue timeline was composed for {len(js)} shots, the episode now has {n_shots}"
    return None


def resolve_cues(shots: list[dict], starts: list[float], meta: dict, edits: dict[int, str]) -> list[Cue]:
    """One effective cue list, sorted by time.

    When epXX.json carries a cue timeline it is used as given (gpu3 composed the music to it); our own text
    parsing is not applied, and owner edits do not override it (the music itself must be regenerated for
    those — see `ignored_edits`). Cue times are re-anchored to our shot starts when the json's shot_starts
    differ. Without json cues: ShotEdit.music > Shot.music_cue > default (enter at 0 s, intensity 1)."""
    timed: list[Cue] = []
    if meta.get("cues") and not stale_timeline(meta, len(shots)):   # 过期时间轴 → 改用剧本 / 修改的提示
        json_starts = meta.get("shot_starts") or []
        for d in meta["cues"]:
            c = _cue_from_json(d)
            if c.shot and 0 < c.shot <= len(starts):
                if c.start_s is None:
                    c.start_s = starts[c.shot - 1] + (c.offset_s or 0.0)
                elif len(json_starts) >= c.shot:
                    c.start_s = starts[c.shot - 1] + (c.start_s - float(json_starts[c.shot - 1]))
            if c.start_s is None:
                continue
            timed.append(c)
    else:
        for i, s in enumerate(shots):
            idx = int(s["index"])
            cue = None
            if edits.get(idx):
                cue = parse_cue_text(edits[idx])
                if cue:
                    cue.source = "edit"
            if cue is None and s.get("music_cue"):
                cue = parse_cue_text(s["music_cue"])
                if cue:
                    cue.source = "script"
            if cue is not None:
                cue.shot, cue.start_s = idx, starts[i]
                timed.append(cue)
    for c in timed:  # shot of a timed cue (for per-shot ducking)
        if c.shot is None:
            c.shot = max((k + 1 for k, t0 in enumerate(starts) if t0 <= c.start_s + 1e-6), default=1)
    if not timed:
        timed = [Cue(shot=int(shots[0]["index"]) if shots else 1, start_s=0.0, action="enter", intensity=1,
                     source="default")]
    return sorted(timed, key=lambda c: c.start_s)


def ignored_edits(meta: dict, edits: dict[int, str], n_shots: int | None = None) -> dict[int, str]:
    """Owner music edits that a json cue timeline overrides (the BGM must be regenerated on gpu3 for them).
    A stale timeline (n_shots given and different) overrides nothing: the edits are applied."""
    if n_shots is not None and stale_timeline(meta, n_shots):
        return {}
    return dict(edits) if meta.get("cues") else {}


def level_curve(cues: list[Cue], n: int, sr: int, total_s: float, *, fade_in_s: float = 1.5,
                fade_out_s: float = 2.0, stinger_db: float = 6.0) -> np.ndarray:
    """Linear gain per sample (before ducking) from the cue list."""
    t = np.arange(n, dtype=np.float64) / sr
    db = np.full(n, -120.0)
    cur = None  # current intensity
    for k, c in enumerate(cues):
        a = c.start_s
        b = cues[k + 1].start_s if k + 1 < len(cues) else total_s
        seg = (t >= a) & (t < b)
        prev_db = INTENSITY_DB.get(cur) if cur else None
        prev_db = -120.0 if prev_db is None else prev_db
        if c.action == "drop":
            target = c.intensity if c.intensity is not None else 0
        elif c.action == "swell":
            target = c.intensity if c.intensity is not None else min(3, (cur or 1) + 1)
        elif c.action == "enter":
            target = c.intensity if c.intensity is not None else (cur or 1)
        else:
            target = c.intensity if c.intensity is not None else (cur if cur is not None else 1)
        tdb = INTENSITY_DB.get(target)
        tdb = -120.0 if tdb is None else tdb
        if c.action == "swell":
            ramp = max(0.5, b - a)
        elif c.action == "drop":
            ramp = c.fade_s or 1.0
        elif c.action == "enter" and (cur in (None, 0)):
            ramp = c.fade_s or fade_in_s
        else:
            ramp = c.fade_s or 0.5
        x = np.clip((t[seg] - a) / ramp, 0, 1)
        if prev_db <= -100 and tdb > -100:  # fade in from silence: ramp in the linear domain
            g = x * 10 ** (tdb / 20)
            db[seg] = 20 * np.log10(np.maximum(g, 1e-6))
        elif tdb <= -100 and prev_db > -100:
            g = (1 - x) * 10 ** (prev_db / 20)
            db[seg] = 20 * np.log10(np.maximum(g, 1e-6))
        else:
            db[seg] = prev_db + (tdb - prev_db) * x if prev_db > -100 else tdb
        if c.action == "stinger":
            acc = np.clip(1 - np.abs((t[seg] - a - 0.15) / 0.6), 0, 1) * stinger_db
            db[seg] = db[seg] + acc
        cur = target
    g = np.where(db <= -100, 0.0, 10 ** (db / 20))
    k = round(fade_out_s * sr)
    if k and n > k:
        g[-k:] *= np.linspace(1, 0, k)
    return g.astype(np.float32)


def normalise_rms(x: np.ndarray, ref_dbfs: float = MUSIC_REF_DBFS) -> np.ndarray:
    rms = float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) if len(x) else 0.0
    if rms < 1e-6:
        return x
    return (x * (10 ** (ref_dbfs / 20) / rms)).astype(np.float32)


def fit_length(x: np.ndarray, n: int, sr: int, xfade_s: float = 0.5) -> tuple[np.ndarray, bool]:
    """Trim, or loop with a short crossfade, to exactly n samples. Returns (audio, looped)."""
    if len(x) >= n:
        return x[:n], False
    k = min(round(xfade_s * sr), len(x) // 4)
    out = x.copy()
    while len(out) < n:
        if k > 0:
            ramp = np.linspace(0, 1, k, dtype=np.float32)[:, None]
            head = x[:k] * ramp + out[-k:] * (1 - ramp)
            out = np.concatenate([out[:-k], head, x[k:]])
        else:
            out = np.concatenate([out, x])
    return out[:n], True


# ---------------- gpu3 stems ----------------

STEM_DEFAULTS = {  # name -> (file, gain dB, role); role decides ducking under speech
    "h3_vocals": ("h3_vocals.wav", 0.0, "vocals"),
    "h3_rest": ("h3_rest.wav", -3.0, "scene"),
    "foley": ("foley.wav", -6.0, "foley"),
    "motifs": ("motifs.wav", -4.0, "foley"),
    "partner": ("partner.wav", -2.0, "vocals"),
}


def load_stems(shot_dir: Path) -> tuple[list[dict], str | None]:
    """Stems of one shot from SN/audio/stems.json (+ default file names). [] when there is no stems folder.
    Each: {name, path, offset_s, gain_db, role}."""
    d = shot_dir / "audio"
    if not d.is_dir():
        return [], None
    meta, err = {}, None
    j = d / "stems.json"
    if j.is_file():
        try:
            meta = json.loads(j.read_text(encoding="utf-8"))
        except ValueError:
            err = f"{j} is not valid JSON"
    entries = meta.get("stems", meta) if isinstance(meta, dict) else {}
    if isinstance(entries, list):
        entries = {e.get("name") or Path(e.get("file", "")).stem: e for e in entries if isinstance(e, dict)}
    out = []
    for name, (fname, gain, role) in STEM_DEFAULTS.items():
        e = entries.get(name) if isinstance(entries, dict) else None
        e = e if isinstance(e, dict) else {}
        p = d / (e.get("file") or fname)
        if not p.is_file():
            continue
        g = e.get("gain_db", e.get("suggested_gain_db", gain))
        out.append({"name": name, "path": p, "offset_s": float(e.get("offset_s") or 0.0),
                    "gain_db": float(g if g is not None else gain), "role": e.get("role") or role,
                    "duck_under_voice_db": e.get("duck_under_voice_db"),
                    "duck_under_vo_db": e.get("duck_under_vo_db")})
    return out, err
