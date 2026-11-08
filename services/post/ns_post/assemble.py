"""Episode assembly: 6 shot videos + TTS dialogue/voiceover + H3 sound design → data/episodes/epXX.mp4.

Picture: each shot runs for its H3 video's own length (G13: the script's whole seconds only size the H3
request); placeholder stills use Shot.duration. Scaled to cover 1080×1920 and
center-cropped, 24 fps, H.264 (libx264, CPU). No subtitles (owner rule). Shots without a gpu3 video get a
placeholder still clip made from the active first frame (marked in the timeline).

Sound (one audible copy of every line, the lesson from ns-studio media/audio.py): H3 was prompted with the
same dialogue, so its soundtrack carries its own take of the lines in an arbitrary voice. Under every TTS
dialogue line the H3 track is gated (-gate_db, linear ramps); under voiceover it is ducked (-vo_duck_db);
elsewhere it plays at h3_gain_db as the sound design bed. The sum is loudness-normalised with a two-pass
linear loudnorm to target_lufs (default -14 LUFS, true peak -1.5 dBTP).

Timing: dialogue is laid out inside its own shot (lead-in, gaps); if it does not fit, gaps shrink, then the
lines are sped up (atempo, ≤ max_tempo) and, as a last resort, cut at the shot end — each with a warning.
Voiceover starts in its own shot (after the shot's dialogue, if any) and may run on into the next shots,
avoiding later dialogue where it can. Audio that runs past the last shot holds the last frame (tail hold).
"""

import json
import math
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
import os
from pathlib import Path

import numpy as np

from .ffmpeg import FFmpeg, parse_loudnorm
from .music import (find_music, fit_length, h3_music_shots, ignored_edits, level_curve, load_stems, normalise_rms,
                    read_edit_music, resolve_cues, stale_timeline)
from .speakers import Cast, SpokenLine, resolve_episode
from . import director as DIRP   # DIRECTOR 11-02（总调度批准，CONSENT VIDEO10/WORKFLOW → DIRECTOR）：导演层落点，开关 config/post/director.json
from .tts import (CACHE_DIR, AsrBackend, TtsBackend, Voice, asr_limit, char_error_rate, default_voiceover_speaker, load_voice_config,
                  synthesize_cached, voice_for, wav_duration)

ROOT = Path(os.environ.get("NS_PROJECT_ROOT") or Path(__file__).resolve().parents[3])  # T42：子进程可指向 projects/<slug>
DATA = ROOT / "data"


@dataclass(frozen=True)
class Settings:
    width: int = 1080
    height: int = 1920
    fps: int = 24
    crf: int = 18
    preset: str = "medium"
    threads: int = 8  # x264 threads (CPU is shared with other jobs)
    sample_rate: int = 48000
    target_lufs: float = -14.0
    true_peak_db: float = -1.5
    lra: float = 11.0
    h3_gain_db: float = -3.0  # sound design bed under everything
    gate_db: float = 40.0  # H3 attenuation under TTS dialogue (its own take of the line)
    vo_duck_db: float = 10.0  # H3 attenuation under voiceover
    gate_pad_s: float = 0.12
    gate_fade_s: float = 0.08
    dialogue_lead_s: float = 0.3
    dialogue_gap_s: float = 0.35
    min_lead_s: float = 0.1
    min_gap_s: float = 0.12
    max_tempo: float = 1.2
    vo_lead_s: float = 0.5
    vo_after_dialogue_s: float = 0.4
    vo_margin_s: float = 0.2
    tail_s: float = 0.6  # audio must end this long before the picture does
    chorus_gain_db: float = -4.0
    # FIX（ep02_iter v0，DIR 切点 S5→S6 −18 dB、S10→S11 −53 dB）：0.05 → 0.12，H3 轨在切点交叉淡化（不是各自淡到 0）
    edge_fade_s: float = 0.12
    # gpu3 stems (docs/intimate_audio.md: foley -6..-10 dB under voice, scene -8..-12 dB under voice-over)
    scene_duck_dialogue_db: float = 6.0
    scene_duck_vo_db: float = 10.0
    foley_duck_db: float = 8.0
    # BGM
    bgm: bool = True
    bgm_duck_dialogue_db: float = 12.0
    bgm_duck_vo_db: float = 9.0
    bgm_duck_h3_db: float = 6.0  # epXX.json duck_spans without their own duck_db
    # X-4 / E8 (owner 2026-10-30): music goes into H3; shots whose H3 already carries music get no BGM on top
    bgm_skip_h3_music: bool = True
    # BGM10（2026-11-01）：配乐目录（相对项目根或绝对路径）。None = data/music（旧剧）；series10 用 data/music/series10。
    # 配乐 json 带 "duck": {"dialogue_db", "vo_db"} 时，按它压低（BGM 链接规则 K-BGM-004：10–14 dB）
    music_dir: str | None = None
    # deliveries: 1080x1920 always; 2160x3840 when "on", or "auto" and any 4K clip exists
    deliver_4k: str = "auto"
    width_4k: int = 2160
    height_4k: int = 3840
    # 4K H.264 (docs/upscale_4k.md "Notes for T38"): CRF 16-17 + VBV cap; grain sigma2 @ CRF 17 ~39 Mbit/s
    crf_4k: int = 17
    maxrate_4k: str = "60M"
    bufsize_4k: str = "120M"
    level_4k: str = "5.1"
    # per-shot colour correction to the shot's locked first/last keyframes (ns_post.colormatch; <clip>_cc.mp4).
    # None = config/render_quality.json post.color_match (default on, per_episode switch; fredw-92/Q1, research B1)
    color_match: bool | None = None
    # AUD（2026-10-31，项目方退回 ep02“声音有问题，嘴唇也没有和声音同步”）：对白来源。
    # "tts" = 现状（H3 轨在 TTS 对白处压 gate_db，叠 Kokoro）；"h3" = 保留 H3 原声对白（嘴型同步），TTS 只做
    # 独白/画外音和 H3 回听不合格时的兜底（ns_post.h3_dialogue）。None = config/render_quality.json
    # post.dialogue_source（没有则 "tts"）
    dialogue_source: str | None = None
    h3_max_cer: float = 0.35  # H3 原声回听 CER 上限（短句取 asr_limit 的 0.5）
    vo_max_tempo: float = 1.0  # 独白放不下本镜时最多提速到 ×这个值（1.0 = 现状，不提速）
    # H3 自带配乐：keep = 现状；duck_under_bgm = 整集 BGM 全程铺，H3 轨在“没有 H3 对白”的地方再压 h3_music_duck_db
    # （单轨无分轨时对白处的 H3 配乐分不出来，只能留着）
    # None = config/render_quality.json post.h3_music.default / duck_db（项目方 2026-10-31“整集统一 BGM”→ duck_under_bgm）；
    # CLI --h3-music / --h3-music-duck-db 覆盖
    h3_music: str | None = None
    h3_music_duck_db: float | None = None
    # ---- FIX（ep02_iter v0）合成硬规则。None = config/render_quality.json post.edit_rules（再没有 = EDIT_DEFAULTS）
    take_policy: str | None = None          # locked_qc：只用定稿且运动质检不是 fail 的 take；没有就标缺
    allow_unlocked_qc_pass: bool | None = None
    trim_frozen: bool | None = None         # 自动剪掉开头 / 结尾定住段
    freeze_still_diff: float | None = None
    freeze_min_s: float | None = None
    min_keep_s: float | None = None
    max_shot_s: float | None = None         # 单镜上限；超过拆刀，后一刀同素材推近 split_zoom 换景别
    split_zoom: float | None = None
    split_min_piece_s: float | None = None
    room_tone: bool | None = None           # 切点 / 静音处铺环境底噪，不允许数字静音
    room_tone_floor_dbfs: float | None = None
    room_tone_window_s: float | None = None
    overflow_policy: str | None = None      # fail：对白 / 独白溢出本镜 → 判不通过并在本镜结尾截断（不覆盖下一镜）
    open_first_cut_s: float | None = None   # 开场第一刀上限（秒）；0 = 不管
    open_min_piece_s: float | None = None   # 开场拆出来的第一刀最短
    skin_guard: bool | None = None          # 脸颊 a* 异常块限幅（ns_post.skin_guard，嘴唇排除）
    # V2（ep02_iter，项目方 10-31“节奏有点慢，慢溜溜的……很不 smooth”“镜头 1 和镜头 2 之间的 transition 非常生硬”）
    pace_tighten: bool | None = None        # 对白镜：第一句前只留 pace_lead_s、最后一句后只留 pace_tail_pad_s；无对白无独白镜 ≤ pace_idle_max_s
    pace_lead_s: float | None = None
    pace_tail_pad_s: float | None = None
    pace_idle_max_s: float | None = None
    pace_min_keep_s: float | None = None    # 收紧后每镜至少留（给表情 / 反应）；独白镜不收
    voiceover_drop: list | str | None = None   # V2：去掉独白的镜头号（None = config post.voiceover_drop）
    jl_cut_s: float | None = None           # J-cut：下一镜声音（剪掉的开头里有的话）提前进入的秒数；0 = 只做交叉淡化

    def to_dict(self) -> dict:
        return asdict(self)


QUALITY_CONFIG = ROOT / "config" / "render_quality.json"


def color_match_enabled(ep: int, s: "Settings | None" = None, config: Path | None = None) -> bool:
    """fredw-92/Q1 (research B1): Settings.color_match if set (CLI --color-match / --no-color-match), else
    render_quality.json post.color_match.per_episode["<ep>"], else its default (true)."""
    if s is not None and s.color_match is not None:
        return bool(s.color_match)
    try:
        cm = json.loads(Path(config or QUALITY_CONFIG).read_text(encoding="utf-8"))["post"]["color_match"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    per = cm.get("per_episode") or {}
    for key in (str(ep), f"ep{ep:02d}"):
        if key in per:
            return bool(per[key])
    return bool(cm.get("default", False))


def dialogue_source_for(ep: int, s: "Settings | None" = None, config: Path | None = None) -> str:
    """AUD：Settings.dialogue_source（CLI --dialogue-source）> render_quality.json post.dialogue_source
    .per_episode["<ep>"] > .default > "tts"。"""
    if s is not None and s.dialogue_source:
        v = s.dialogue_source
    else:
        try:
            ds = json.loads(Path(config or QUALITY_CONFIG).read_text(encoding="utf-8"))["post"]["dialogue_source"]
        except (OSError, ValueError, KeyError, TypeError):
            ds = {}
        per = ds.get("per_episode") or {}
        v = next((per[k] for k in (str(ep), f"ep{ep:02d}") if k in per), ds.get("default") or "tts")
    if v not in ("tts", "h3"):
        raise ValueError(f"dialogue_source must be 'tts' or 'h3', not {v!r}")
    return v


# FIX（ep02_iter v0）合成硬规则的兜底值（与 config/render_quality.json post.edit_rules 同值；证据见 docs/ep02_iter/LEARNED_SPECS.md）
EDIT_DEFAULTS = {
    "take_policy": "locked_qc", "allow_unlocked_qc_pass": False,
    "trim_frozen": True, "freeze_still_diff": 0.35, "freeze_min_s": 0.3, "min_keep_s": 1.5,
    "max_shot_s": 6.0, "split_zoom": 1.15, "split_min_piece_s": 2.0,
    "room_tone": True, "room_tone_floor_dbfs": -50.0, "room_tone_window_s": 0.1,
    "overflow_policy": "fail", "skin_guard": False,
    "open_first_cut_s": 3.0, "open_min_piece_s": 1.0,
    "pace_tighten": False, "pace_lead_s": 0.3, "pace_tail_pad_s": 0.5, "pace_idle_max_s": 4.0, "pace_min_keep_s": 3.0,
    "jl_cut_s": 0.0,
}
REJECT_QC = ("fail",)   # take.qc.motion.verdict 为这些值的版本不进成片


def _post_cfg(config: Path | None = None) -> dict:
    try:
        return json.loads(Path(config or QUALITY_CONFIG).read_text(encoding="utf-8")).get("post") or {}
    except (OSError, ValueError, AttributeError):
        return {}


def _per_episode(block: dict, ep: int, key: str = "default"):
    per = (block or {}).get("per_episode") or {}
    for k in (str(ep), f"ep{ep:02d}"):
        if k in per:
            return per[k]
    return (block or {}).get(key)


def resolve_settings(s: "Settings", ep: int, config: Path | None = None) -> "Settings":
    """FIX：Settings 里 None 的合成规则 → config post.edit_rules / post.h3_music / post.skin_guard → EDIT_DEFAULTS"""
    from dataclasses import replace
    post = _post_cfg(config)
    rules = {k: v for k, v in (post.get("edit_rules") or {}).items() if k in EDIT_DEFAULTS}
    upd = {}
    for k, v in EDIT_DEFAULTS.items():
        if getattr(s, k) is None:
            upd[k] = rules.get(k, v)
    if s.skin_guard is None:
        sg = _per_episode(post.get("skin_guard") or {}, ep, "enabled")
        if sg is not None:
            upd["skin_guard"] = bool(sg)
    hm = post.get("h3_music") or {}
    if s.h3_music is None:
        v = _per_episode(hm, ep) or "keep"
        upd["h3_music"] = v if v in ("keep", "duck_under_bgm", "dialogue_only") else "keep"
    if s.h3_music_duck_db is None:
        upd["h3_music_duck_db"] = float(hm.get("duck_db", 12.0))
    return replace(s, **upd) if upd else s


def voiceover_drop_shots(ep: int, s: "Settings | None" = None, config: Path | None = None) -> set[int]:
    """V2：本集去掉独白（🎙）的镜头号。Settings.voiceover_drop 显式给（含空集）优先，否则读
    config/render_quality.json post.voiceover_drop.per_episode {"2": [8, 11]}；"all" = 整集都去掉"""
    v = getattr(s, "voiceover_drop", None) if s is not None else None
    if v is None:
        v = _per_episode((_post_cfg(config).get("voiceover_drop") or {}), ep, "default")
    if v in (None, False, [], ""):
        return set()
    if v == "all" or v is True:
        return set(range(1, 1000))
    return {int(str(x).lstrip("Ss")) for x in v}


def _rule(s: "Settings", name: str):
    v = getattr(s, name, None)
    return EDIT_DEFAULTS[name] if v is None else v


# ---------------- picture sources ----------------

def _active_file(shot_dir: Path, stage: str) -> Path | None:
    tj = shot_dir / "takes.json"
    if not tj.exists():
        return None
    try:
        t = json.loads(tj.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    n = (t.get("active") or {}).get(stage)
    for take in t.get("takes") or []:
        if take.get("stage") == stage and take.get("n") == n and take.get("file"):
            p = Path(take["file"])
            for cand in (p, DATA / p, shot_dir / "takes" / p.name):
                if cand.is_file():
                    return cand
    return None


def qualified_take(shot_dir: Path, stage: str = "video", *, allow_unlocked_qc_pass: bool = False,
                   reject=REJECT_QC) -> tuple[Path | None, dict | None, str]:
    """FIX（ep02_iter P0 ①，chain_audit D6）：成片只用“已定稿且运动质检不是 fail、不是坏图 / 隔离”的版本，
    不再无条件取 active。allow_unlocked_qc_pass：没有合格定稿时可用质检 pass 的未定稿版本（默认关）。
    → (文件, take, 说明)；没有合格版本 → (None, None, 原因)"""
    try:
        t = json.loads((shot_dir / "takes.json").read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None, None, "没有 takes.json"
    takes = [x for x in t.get("takes") or [] if x.get("stage") == stage]

    def path_of(take):
        if not take.get("file"):
            return None
        p = Path(take["file"])
        return next((c for c in (p, DATA / p, shot_dir.parents[2] / p, shot_dir / "takes" / p.name)
                     if c.is_file()), None)

    def verdict(take):
        return (((take.get("qc") or {}).get("motion")) or {}).get("verdict")

    n = (t.get("locked") or {}).get(stage)
    why = "没有定稿版本"
    if n is not None:
        take = next((x for x in takes if x.get("n") == n), None)
        if take is None:
            why = f"定稿 v{n} 不在 takes.json"
        elif take.get("bad") or take.get("quarantined"):
            why = f"定稿 v{n} 是坏图 / 已隔离"
        elif verdict(take) in reject:
            why = f"定稿 v{n} 运动质检 {verdict(take)}（{'、'.join(((take.get('qc') or {}).get('motion') or {}).get('labels') or [])}）"
        elif path_of(take) is None:
            why = f"定稿 v{n} 文件不存在"
        else:
            return path_of(take), take, f"定稿 v{n}"
    if allow_unlocked_qc_pass:
        ok = [x for x in takes if verdict(x) == "pass" and not x.get("bad") and not x.get("quarantined")
              and path_of(x) is not None]
        if ok:
            take = max(ok, key=lambda x: x.get("n") or 0)
            return path_of(take), take, f"未定稿 v{take.get('n')}（质检 pass；{why}）"
    return None, None, why


def take_record(take: dict | None) -> dict | None:
    """时间线里记录本镜用的是哪一版（可追溯）"""
    if not take:
        return None
    p = take.get("params") or {}
    return {"n": take.get("n"), "locked": True if take.get("_locked") else None, "seed": take.get("seed"),
            "inputs_sha": take.get("inputs_sha"), "job_id": take.get("job_id"), "gpu": take.get("gpu"),
            "base": p.get("base") if isinstance(p, dict) else None,
            "qc_motion": (((take.get("qc") or {}).get("motion")) or {}).get("verdict")}


def _active_motion_qc(shot_dir: Path, stage: str = "video") -> dict | None:
    """QCV：当前版本 take.qc.motion（ns_render.motion_qc 写的运动量质检）；没有 → None"""
    try:
        t = json.loads((shot_dir / "takes.json").read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    n = (t.get("active") or {}).get(stage)
    take = next((x for x in t.get("takes") or [] if x.get("stage") == stage and x.get("n") == n), None)
    return ((take or {}).get("qc") or {}).get("motion")


def motion_qc_warning(idx: int, src: dict) -> str | None:
    """QCV：本镜视频运动质检 fail / warn → 合成警告（只提示，不改画面 / 声音）"""
    mq = src.get("motion_qc") or {}
    if src.get("kind") != "video" or mq.get("verdict") not in ("fail", "warn"):
        return None
    head = "运动质检不合格" if mq["verdict"] == "fail" else "运动质检提示"
    return f"S{idx}: {head}（{'、'.join(mq.get('labels') or [])}）{_rel(src.get('path')) or ''}".rstrip()


def find_source(ep: int, idx: int, *, data: Path = DATA, allow_laptop_video: bool = False,
                settings: "Settings | None" = None) -> dict:
    """Where shot epXX/SN's picture comes from: {kind, path, note}. kind: video | still | black.
    FIX（P0 ①）：take_policy locked_qc（默认）只取合格定稿版本（qualified_take）；没有 → missing=True 的占位
    （定稿首帧静帧或黑场），时间线标缺、报告列出，不静默用废片。take_policy active_legacy = 旧行为（只给对照用）"""
    s = settings or Settings()
    g = data / "renders_gpu3" / f"ep{ep:02d}" / f"S{idx}"
    l = data / "renders" / f"ep{ep:02d}" / f"S{idx}"
    if _rule(s, "take_policy") == "active_legacy":
        v = _active_file(g, "video") if g.is_dir() else None
        if v is None and (g / "video.mp4").is_file():
            v = g / "video.mp4"
        if v is not None:
            v4 = v.with_name(v.stem + "_4k" + v.suffix)
            return {"kind": "video", "origin": "gpu3", "path": v, "path_4k": v4 if v4.is_file() else None,
                    "motion_qc": _active_motion_qc(g), "take_policy": "active_legacy"}
        if allow_laptop_video:
            lv = _active_file(l, "video") or ((l / "video.mp4") if (l / "video.mp4").is_file() else None)
            if lv is not None:
                l4 = lv.with_name(lv.stem + "_4k" + lv.suffix)   # fredw-92/Q1: optional gpu3 upscale_4k result
                return {"kind": "video", "origin": "laptop", "path": lv, "path_4k": l4 if l4.is_file() else None,
                        "motion_qc": _active_motion_qc(l), "take_policy": "active_legacy"}
        missing_why = None
    else:
        reasons = []
        for d, origin, allowed in ((g, "gpu3", True), (l, "laptop", allow_laptop_video)):
            if not allowed or not d.is_dir():
                continue
            v, take, why = qualified_take(d, allow_unlocked_qc_pass=bool(_rule(s, "allow_unlocked_qc_pass")))
            if v is not None:
                v4 = v.with_name(v.stem + "_4k" + v.suffix)
                take = dict(take, _locked=why.startswith("定稿"))
                return {"kind": "video", "origin": origin, "path": v, "path_4k": v4 if v4.is_file() else None,
                        "motion_qc": ((take.get("qc") or {}).get("motion")), "take": take_record(take),
                        "take_note": why, "take_policy": "locked_qc"}
            reasons.append(f"{origin}：{why}")
        missing_why = "；".join(reasons) or "没有视频版本"
    for d, origin in ((g, "gpu3"), (l, "laptop")):
        img = _active_file(d, "image") if d.is_dir() else None
        if img is None and (d / "frame.png").is_file():
            img = d / "frame.png"
        if img is not None:
            return {"kind": "still", "origin": origin, "path": img,
                    **({"missing": True, "missing_reason": missing_why} if missing_why else {})}
    return {"kind": "black", "origin": None, "path": None,
            **({"missing": True, "missing_reason": missing_why} if missing_why else {})}


# ---------------- FIX（ep02_iter v0）剪辑规则（纯函数，可单测） ----------------

def frame_diffs(path: str | Path, ff: "FFmpeg | None" = None, w: int = 96, h: int = 168) -> np.ndarray:
    """96×168 灰度相邻帧平均差（同 ns_render.motion_qc / DIR review.py）"""
    import subprocess
    exe = getattr(ff, "ffmpeg", None) or "ffmpeg"
    r = subprocess.run([exe, "-v", "error", "-nostdin", "-i", str(path), "-map", "0:v:0",
                        "-vf", f"scale={w}:{h},format=gray", "-f", "rawvideo", "-"], capture_output=True,
                       timeout=600)   # K-OPS-S01：子进程必须有 timeout（SYSARCH 4-5）
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-300:].decode("utf-8", "replace"))
    fr = np.frombuffer(r.stdout, np.uint8).reshape(-1, h, w).astype(np.float32)
    return np.abs(np.diff(fr, axis=0)).mean(axis=(1, 2)) if len(fr) > 1 else np.zeros(0, np.float32)


def freeze_trims(diffs, fps: float, *, still_diff: float = 0.35, min_s: float = 0.3, min_keep_s: float = 1.5
                 ) -> tuple[int, int]:
    """开头 / 结尾“定住”段的帧数（相邻帧差 < still_diff 连续 ≥ min_s 才剪）；剪完至少留 min_keep_s。
    → (开头剪掉的帧数, 结尾剪掉的帧数)。DIR v0：10/11 镜结尾定住 0.67–6.5 s"""
    d = np.asarray(diffs, float)
    n = len(d) + 1                       # 帧数
    if n < 2:
        return 0, 0
    still = d < still_diff
    if not bool((~still).any()):         # 整条都定住：只剪结尾，留 min_keep_s
        head, tail = 0, len(d)
    else:
        head = int(np.argmax(~still))    # 开头连续静止的相邻帧对数 = 可剪帧数
        tail = int(np.argmax(~still[::-1]))
    head = head if head / fps >= min_s else 0
    tail = tail if tail / fps >= min_s else 0
    keep_min = int(math.ceil(min_keep_s * fps))
    if n - head - tail < keep_min:      # 剪太多：按比例少剪，保证最少留 min_keep_s
        over = keep_min - (n - head - tail)
        tot = head + tail
        if tot <= 0:
            return 0, 0
        cut_h = min(head, int(round(over * head / tot)))
        head, tail = head - cut_h, max(0, tail - (over - cut_h))
    return max(0, head), max(0, tail)


def split_pieces(seconds: float, *, max_s: float, min_piece_s: float, zoom: float,
                 busy: list[tuple[float, float]] | None = None) -> list[tuple[float, float, float]]:
    """单镜超过 max_s → 拆成 ceil(seconds/max_s) 刀（每刀 ≥ min_piece_s），切点避开对白句（busy，相对本镜起点），
    第 2、4… 刀同素材推近 zoom（换景别）。→ [(t0, t1, zoom)]"""
    if max_s <= 0 or seconds <= max_s:
        return [(0.0, seconds, 1.0)]
    k = int(math.ceil(seconds / max_s - 1e-9))
    while k > 1 and seconds / k < min_piece_s:
        k -= 1
    if k <= 1:
        return [(0.0, seconds, 1.0)]
    cuts = []
    for j in range(1, k):
        c = seconds * j / k
        for a, b in sorted(busy or []):
            if a < c < b:                # 切点落在一句话中间 → 挪到句间（离理想点近的那一侧）
                c = a if (c - a) <= (b - c) else b
        lo = (cuts[-1] if cuts else 0.0) + min_piece_s
        hi = seconds - min_piece_s * (k - j)
        cuts.append(min(max(c, lo), hi))
    edges = [0.0, *cuts, seconds]
    return [(round(edges[j], 3), round(edges[j + 1], 3), 1.0 if j % 2 == 0 else float(zoom)) for j in range(k)]


def opening_pieces(pieces: list[tuple[float, float, float]], *, first_cut_s: float, min_piece_s: float,
                   zoom: float, busy: list[tuple[float, float]] | None = None) -> list[tuple[float, float, float]]:
    """FIX（ep02_iter v0 DIR：前 3 s 没有切点，第一句对白在 8.3 / 10.8 s）：整集第一镜的第一刀 > first_cut_s → 在
    first_cut_s 处再切一刀（落在台词中间就提前到句前，不短于 min_piece_s），后面各刀推近 / 原景别交替（换景别）"""
    if first_cut_s <= 0 or not pieces or pieces[0][1] - pieces[0][0] <= first_cut_s + 1e-6:
        return pieces
    end = pieces[-1][1]
    c = first_cut_s
    for a, b in sorted(busy or []):
        if a < c < b:
            c = a
    if c < min_piece_s or end - c < min_piece_s:
        return pieces
    edges = [0.0, c] + [p[1] for p in pieces[:-1] if p[1] > c + min_piece_s - 1e-6] + [end]
    return [(round(edges[j], 3), round(edges[j + 1], 3), 1.0 if j % 2 == 0 else float(zoom))
            for j in range(len(edges) - 1)]


def pace_trims(dur: float, *, lines: list[tuple[float, float]], tts_speech_s: float | None = None,
               has_vo: bool = False, lead_s: float = 0.3, tail_pad_s: float = 0.4, idle_max_s: float = 4.0,
               min_keep_s: float = 1.5, diffs=None, fps: float = 24.0) -> tuple[float, float]:
    """V2（项目方 10-31“节奏有点慢，慢溜溜的”）：在剪完定住段之后再收紧一镜。→ (开头再剪 s, 结尾再剪 s)
    - 有独白（🎙）的镜：不动（独白要整句放进本镜）。
    - 有 H3 原声对白（lines = 本镜内各句 [t0, t1]，相对本镜起点）：第一句前只留 lead_s，最后一句后只留 tail_pad_s。
    - 只有 TTS 对白（tts_speech_s = 各句总长 + 句间）：开头不动，结尾留 lead_s + 语音 + tail_pad_s。
    - 无对白无独白：最长 idle_max_s；有逐帧运动量（diffs）时取运动量最大的那一段（在动作进行中切出 / 切入）。
    任何情况都至少留 min_keep_s。"""
    if has_vo or dur <= min_keep_s:
        return 0.0, 0.0
    head = tail = 0.0
    if lines:
        t0 = min(a for a, _ in lines)
        t1 = max(b for _, b in lines)
        head = max(0.0, t0 - lead_s)
        tail = max(0.0, dur - (t1 + tail_pad_s))
    elif tts_speech_s is not None:
        tail = max(0.0, dur - (lead_s + tts_speech_s + tail_pad_s))
    elif dur > idle_max_s:
        win = idle_max_s
        start = 0.0
        d = np.asarray(diffs if diffs is not None else [], float)
        if len(d) >= 2:
            n = len(d)
            w = max(1, min(n, int(round(win * fps))))
            c = np.concatenate([[0.0], np.cumsum(d)])
            sums = c[w:] - c[:-w]                      # 每个起点的窗口运动量
            k = int(np.argmax(sums + 1e-9 * -np.arange(len(sums))))   # 平手取最早
            start = min(max(0.0, k / fps), dur - win)
        head, tail = start, max(0.0, dur - start - win)
    keep = dur - head - tail
    if keep < min_keep_s:                               # 剪太多：先还结尾，再还开头
        need = min_keep_s - keep
        give = min(tail, need)
        tail -= give
        head = max(0.0, head - (need - give))
    return round(head, 3), round(tail, 3)


def piece_frames(pieces: list[tuple[float, float, float]], total_frames: int, fps: int) -> list[int]:
    """各刀帧数（整数，和 = total_frames）"""
    ends = [min(total_frames, round(p[1] * fps)) for p in pieces]
    ends[-1] = total_frames
    out, prev = [], 0
    for e in ends:
        out.append(max(0, e - prev))
        prev = e
    return out


def room_tone(n: int, sr: int, seed: int = 20261031) -> np.ndarray:
    """确定性的环境底噪（粉红化白噪声，立体声去相关），RMS 归一到 1"""
    rng = np.random.default_rng(seed)
    m = max(2, n)
    spec = np.fft.rfft(rng.standard_normal((m, 2)), axis=0)
    f = np.fft.rfftfreq(m, 1 / sr)
    shape = 1 / np.sqrt(np.maximum(f, 40.0))          # 粉红（1/f 功率），40 Hz 以下不再抬
    shape[f > 8000] *= np.exp(-(f[f > 8000] - 8000) / 4000)   # 高频滚降：像室内底噪，不像嘶声
    y = np.fft.irfft(spec * shape[:, None], n=m, axis=0)[:n].astype(np.float32)
    y -= y.mean(axis=0)
    return y / max(float(np.sqrt((y ** 2).mean())), 1e-9)


def fill_silence(mix: np.ndarray, sr: int, floor_dbfs: float = -50.0, window_s: float = 0.1,
                 noise: np.ndarray | None = None) -> tuple[np.ndarray, dict]:
    """FIX（DIR：S10→S11 切过去 100 ms 内 −86 dB 数字静音）：每个 window_s 窗口 RMS 低于 floor_dbfs 的地方补环境底噪
    到 floor（窗口间线性插值增益，不跳变）。→ (新 mix, 报告)"""
    n = len(mix)
    if n == 0:
        return mix, {"filled_windows": 0}
    w = max(1, int(round(window_s * sr)))
    k = int(math.ceil(n / w))
    pad = np.zeros((k * w, mix.shape[1]), np.float32)
    pad[:n] = mix
    rms = np.sqrt((pad.reshape(k, w, -1) ** 2).mean(axis=(1, 2)))
    floor = 10 ** (floor_dbfs / 20)
    need = np.sqrt(np.clip(floor ** 2 - rms ** 2, 0, None))      # 底噪功率补到 floor
    if not (need > 0).any():
        return mix, {"filled_windows": 0, "floor_dbfs": floor_dbfs}
    centers = (np.arange(k) + 0.5) * w
    g = np.interp(np.arange(n), centers, need).astype(np.float32)[:, None]
    nz = noise if noise is not None and len(noise) >= n else room_tone(n, sr)
    out = mix + nz[:n, : mix.shape[1]] * g
    return out.astype(np.float32), {"filled_windows": int((need > 0).sum()), "windows": k, "floor_dbfs": floor_dbfs,
                                    "min_window_dbfs_before": round(float(20 * np.log10(max(rms.min(), 1e-9))), 1)}


def min_window_dbfs(x: np.ndarray, sr: int, window_s: float = 0.1) -> float:
    w = max(1, int(round(window_s * sr)))
    k = len(x) // w
    if k == 0:
        return -200.0
    rms = np.sqrt((x[: k * w].reshape(k, w, -1) ** 2).mean(axis=(1, 2)))
    return float(20 * np.log10(max(float(rms.min()), 1e-10)))


# ---------------- timeline planning (pure) ----------------

@dataclass
class Placement:
    line: SpokenLine
    voices: list[str]
    clip_s: float  # natural TTS length (s)
    start_s: float = 0.0
    tempo: float = 1.0
    cut_s: float | None = None  # audible length after a forced cut at the shot end
    files: list[str] = field(default_factory=list)
    # AUD：source "h3" = 这句用 H3 原声（start/clip 取自回听时间戳，fixed：plan_timeline 不挪、不叠 TTS）
    source: str = "tts"
    fixed: bool = False
    overflow_s: float = 0.0   # FIX：溢出本镜的秒数（> 0 = 合成判不通过，已在本镜结尾截断）

    @property
    def length_s(self) -> float:
        n = self.clip_s / self.tempo
        return min(n, self.cut_s) if self.cut_s is not None else n

    @property
    def end_s(self) -> float:
        return self.start_s + self.length_s


def _overlaps(a0: float, a1: float, spans: list[tuple[float, float]], margin: float) -> tuple[float, float] | None:
    for b0, b1 in spans:
        if a0 < b1 + margin and b0 < a1 + margin:
            return b0, b1
    return None


def plan_timeline(durations: list[float], placements: list[Placement], s: Settings = Settings()
                  ) -> tuple[list[float], float, list[str]]:
    """Set start/tempo/cut on every placement. Returns (shot starts, tail hold seconds, warnings)."""
    warnings: list[str] = []
    starts = [sum(durations[:i]) for i in range(len(durations))]
    total = sum(durations)
    by_shot: dict[int, list[Placement]] = {}
    for p in placements:
        if p.line.kind == "dialogue" and not p.fixed:  # AUD：H3 原声句（fixed）位置由 H3 定，不排
            by_shot.setdefault(p.line.shot, []).append(p)
    for shot, lines in by_shot.items():
        i = shot - 1
        dur, t0 = durations[i], starts[i]
        speech = sum(p.clip_s for p in lines)
        n = len(lines)
        lead, gap, tempo = s.dialogue_lead_s, s.dialogue_gap_s, 1.0
        if lead + speech + gap * (n - 1) + s.min_lead_s > dur:
            lead, gap = s.min_lead_s, s.min_gap_s
            room = dur - 2 * s.min_lead_s - gap * (n - 1)
            if speech > room:
                tempo = min(s.max_tempo, speech / max(room, 1e-6))
                warnings.append(f"S{shot}: dialogue {speech:.2f}s does not fit the {dur:g}s shot; "
                                f"sped up ×{tempo:.2f}")
        t = t0 + lead
        for p in lines:
            p.start_s, p.tempo = round(t, 3), round(tempo, 4)
            t = p.end_s + gap
        last = lines[-1]
        if last.end_s > t0 + dur - 0.02:
            over = last.end_s - (t0 + dur)
            warnings.append(f"S{shot}: dialogue still {over:.2f}s longer than the shot after ×{tempo:.2f}; "
                            f"lines are cut at the shot end")
            for p in lines:
                if p.end_s > t0 + dur - 0.02:
                    p.overflow_s = round(p.end_s - (t0 + dur), 3)   # FIX：截断 = 判不通过（报告列出）
                    p.cut_s = max(0.0, t0 + dur - 0.02 - p.start_s)
    shot_of = {k + 1: (starts[k], durations[k]) for k in range(len(durations))}
    vos: list[Placement] = []

    def busy() -> list[tuple[float, float]]:
        return [(q.start_s, q.end_s) for q in placements if q.line.kind == "dialogue"] +                [(q.start_s, q.end_s) for q in vos]

    def shift_later_dialogue(a0: float, a1: float) -> bool:
        """Move the dialogue block of a later shot that overlaps [a0, a1] later inside its shot, if it fits."""
        for shot, lines in by_shot.items():
            first, last = lines[0], lines[-1]
            if not (a0 < last.end_s + s.vo_margin_s and first.start_s < a1 + s.vo_margin_s):
                continue
            k0, kd = shot_of[shot]
            need = a1 + s.vo_margin_s - first.start_s
            if first.start_s < a0 or last.end_s + need > k0 + kd - s.min_lead_s:
                return False
            for q in lines:
                q.start_s = round(q.start_s + need, 3)
            warnings.append(f"S{shot}: dialogue moved {need:.2f}s later to make room for the voiceover")
            return True
        return False

    for p in sorted((p for p in placements if p.line.kind == "voiceover"), key=lambda p: p.line.shot):
        i = p.line.shot - 1
        t0, dur = starts[i], durations[i]
        own = [q for q in placements if q.line.kind == "dialogue" and q.line.shot == p.line.shot]
        earliest = (max(q.end_s for q in own) + s.vo_after_dialogue_s) if own else t0 + s.vo_lead_s
        room = t0 + dur - s.tail_s - earliest
        if s.vo_max_tempo > 1.0 and p.clip_s > room > 0:  # AUD：放不下本镜 → 按 max 提速（不改文字）
            p.tempo = round(min(s.vo_max_tempo, p.clip_s / room), 4)
            warnings.append(f"S{p.line.shot}: voiceover {p.clip_s:.2f}s does not fit the {room:.2f}s left in its "
                            f"shot; sped up ×{p.tempo:.2f}")
        L = p.clip_s / p.tempo
        base = earliest
        if "尾入" in (p.line.note or "") and not own:  # enters late in its shot, ideally ending with it
            base = max(base, t0 + dur - s.tail_s - L)
        start = base
        hit = _overlaps(start, start + L, busy(), s.vo_margin_s)
        if hit is not None and hit[0] > start:  # runs into something later: start earlier in its own shot
            start = max(earliest, hit[0] - s.vo_margin_s - L)
            hit = _overlaps(start, start + L, busy(), s.vo_margin_s)
        # still: move the next shot's dialogue later if it has room（FIX：overflow fail 时独白不跨镜，不挪下一镜对白）
        if hit is not None and hit[0] > start and _rule(s, "overflow_policy") != "fail":
            if shift_later_dialogue(start, start + L):
                hit = _overlaps(start, start + L, busy(), s.vo_margin_s)
        for _ in range(50):
            if hit is None:
                break
            start = hit[1] + s.vo_margin_s
            hit = _overlaps(start, start + L, busy(), s.vo_margin_s)
        if start > t0 + dur - 0.5:  # cannot begin inside its own shot without talking over dialogue
            start = base
            warnings.append(f"S{p.line.shot}: voiceover overlaps later dialogue (no free slot in its shot)")
        p.start_s = round(start, 3)
        vos.append(p)
        if _rule(s, "overflow_policy") == "fail" and p.end_s > t0 + dur - 0.02:
            # FIX（ep02_iter v0：S8 独白 25.6 s 盖住 S9–S11 对白）：不覆盖下一镜 → 本镜结尾截断 + 判不通过
            p.overflow_s = round(p.end_s - (t0 + dur), 3)
            p.cut_s = max(0.0, t0 + dur - 0.02 - p.start_s)
            warnings.append(f"S{p.line.shot}: 独白溢出本镜 {p.overflow_s:.2f}s → 判不通过（在本镜结尾截断，不覆盖下一镜；"
                            f"长度交写作方）")
            continue
        if p.end_s > t0 + dur:
            nxt = [k + 1 for k in range(len(durations)) if starts[k] < p.end_s and k > i]
            if nxt:
                warnings.append(f"S{p.line.shot}: voiceover runs {p.end_s - (t0 + dur):.2f}s into S{nxt[-1]}"
                                + ("" if len(nxt) == 1 else f" (spans S{nxt[0]}–S{nxt[-1]})"))
    end = max([p.end_s for p in placements], default=0.0)
    tail = max(0.0, end + s.tail_s - total)
    tail = math.ceil(tail * s.fps - 1e-6) / s.fps  # whole frames, so picture and sound end together
    if tail > 0:
        warnings.append(f"audio runs past the last shot: last frame held {tail:.2f}s")
    return starts, round(tail, 3), warnings


# ---------------- rendering ----------------

PLACEHOLDER_PUSH = 0.06   # 占位静帧缓推幅度（0 = 静止）
COLOR_TAGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]


def _vf(s: Settings, frames: int, w: int, h: int, zoom: float = 1.0) -> str:
    """FIX（ep02_iter v0 R6）：显式输出 bt709 矩阵（dejudder 段以前是 601 / 无标记，与 bt709 段 -c copy 混流）；
    zoom > 1 = 同素材推近（长镜拆刀换景别）"""
    zw, zh = (w, h) if zoom <= 1.0 else (int(round(w * zoom / 2)) * 2, int(round(h * zoom / 2)) * 2)
    return (f"fps={s.fps},scale={zw}:{zh}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={w}:{h},setsar=1,tpad=stop_mode=clone:stop_duration=600,"
            f"trim=end_frame={frames},setpts=PTS-STARTPTS,"
            f"scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,"
            f"setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709")


def picture_path(src: dict, uhd: bool = False) -> str | None:
    """这一镜实际出画面的文件：skin guard 版 > 校色版 > 原片（4K 同理）"""
    if uhd:
        return src.get("path_4k_sg") or src.get("path_4k_cc") or src.get("path_4k") or src.get("path")
    return src.get("path_sg") or src.get("path_cc") or src.get("path")


def render_segment(ff: FFmpeg, src: dict, seconds: float, out: Path, s: Settings, *, uhd: bool = False,
                   start_s: float = 0.0, zoom: float = 1.0, frames: int | None = None) -> None:
    frames = round(seconds * s.fps) if frames is None else int(frames)
    w, h = (s.width_4k, s.height_4k) if uhd else (s.width, s.height)
    crf = s.crf_4k if uhd else s.crf
    if src["kind"] == "video":
        ss = ["-ss", f"{start_s:.4f}"] if start_s > 1e-6 else []
        inp = [*ss, "-i", picture_path(src, uhd)]
    elif src["kind"] == "still":
        inp = ["-loop", "1", "-framerate", str(s.fps), "-t", f"{seconds + 1:.3f}", "-i", src["path"]]
    else:
        inp = ["-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:r={s.fps}:d={seconds + 1:.3f}"]
    vbv = ["-maxrate", s.maxrate_4k, "-bufsize", s.bufsize_4k, "-level:v", s.level_4k] if uhd else []
    vf = _vf(s, frames, w, h, zoom)
    if src["kind"] == "still" and PLACEHOLDER_PUSH > 0:
        # ASSEMBLY 11-02：占位静帧缓推（线性放大到 1+PLACEHOLDER_PUSH，居中裁切），让占位镜看得出是占位又不死板
        d = max(frames / s.fps, 0.1)
        vf = vf.replace(f"crop={w}:{h},setsar=1,", f"crop={w}:{h},"
                        f"scale=w='trunc({w}*(1+{PLACEHOLDER_PUSH}*t/{d:.3f})/2)*2':h=-2:eval=frame:flags=bicubic,"
                        f"crop={w}:{h},setsar=1,", 1)
    ff.run([*inp, "-map", "0:v:0", "-vf", vf, "-an", "-c:v", "libx264", "-preset", s.preset,
            "-crf", str(crf), *vbv, "-profile:v", "high", "-pix_fmt", "yuv420p", *COLOR_TAGS, "-r", str(s.fps),
            "-threads", str(s.threads), "-video_track_timescale", "12288", str(out)])


def render_shot(ff: FFmpeg, src: dict, seconds: float, pieces: list, work: Path, tag: str, s: Settings, *,
                uhd: bool = False) -> list[Path]:
    """一镜 → 一个或多个段（长镜拆刀）。帧数按整数分配，总帧数 = round(seconds × fps)"""
    total = round(seconds * s.fps)
    if not pieces or len(pieces) == 1 or src["kind"] != "video":
        out = work / f"{tag}.mp4"
        render_segment(ff, src, seconds, out, s, uhd=uhd, start_s=float(src.get("trim_head_s") or 0.0))
        return [out]
    outs = []
    for j, ((t0, _t1, z), fr) in enumerate(zip(pieces, piece_frames(pieces, total, s.fps))):
        if fr <= 0:
            continue
        out = work / f"{tag}_{j}.mp4"
        render_segment(ff, src, fr / s.fps, out, s, uhd=uhd, frames=fr, zoom=z,
                       start_s=float(src.get("trim_head_s") or 0.0) + t0)
        outs.append(out)
    return outs


def read_audio(ff: FFmpeg, path: str | Path, sr: int, *, channels: int = 2, seconds: float | None = None,
               tempo: float = 1.0, work: Path, start_s: float = 0.0) -> np.ndarray:
    """Decode to float32 [n, channels] at sr (optionally sped up with atempo, cut/padded to seconds).
    start_s（FIX：开头定住段剪掉后）从这一秒开始读"""
    raw = work / f"dec_{abs(hash((str(path), tempo, seconds, start_s))) % 10**12}.f32"
    af = [f"aresample={sr}"]
    if abs(tempo - 1.0) > 1e-3:
        af.append(f"atempo={tempo:.4f}")
    if seconds is not None:
        af += ["apad", f"atrim=end_sample={round(seconds * sr)}"]
    ss = ["-ss", f"{start_s:.4f}"] if start_s > 1e-6 else []
    ff.run([*ss, "-i", path, "-vn", "-af", ",".join(af), "-ac", str(channels), "-ar", str(sr), "-f", "f32le", raw])
    x = np.fromfile(raw, dtype=np.float32)
    raw.unlink(missing_ok=True)
    return x.reshape(-1, channels)


def gain_envelope(n: int, sr: int, spans: list[tuple[float, float, float]], fade: float) -> np.ndarray:
    """Per-sample linear gain: 1 outside spans, 10^(-db/20) inside, linear ramps of `fade` outside each span.
    Overlaps take the deepest attenuation."""
    t = np.arange(n, dtype=np.float64) / sr
    g = np.ones(n, dtype=np.float64)
    for a, b, db in spans:
        floor = 10 ** (-db / 20)
        w = np.clip((t - (a - fade)) / fade, 0, 1) * np.clip(((b + fade) - t) / fade, 0, 1)
        g = np.minimum(g, 1 - (1 - floor) * w)
    return g.astype(np.float32)


def _fade_edges(x: np.ndarray, sr: int, seconds: float) -> np.ndarray:
    k = min(len(x) // 2, round(seconds * sr))
    if k > 0:
        ramp = np.linspace(0, 1, k, dtype=np.float32)[:, None]
        x[:k] *= ramp
        x[-k:] *= ramp[::-1]
    return x


def loudnorm(ff: FFmpeg, raw: Path, out: Path, s: Settings) -> dict:
    """Two-pass linear loudnorm of a raw f32le stereo mix → 48 kHz 16-bit WAV. When the gain to the target
    would push peaks past the true-peak ceiling, a limiter is put in front first (so the second pass can stay
    linear instead of loudnorm's dynamic fallback). Returns the measurements."""
    inp = ["-f", "f32le", "-ar", str(s.sample_rate), "-ac", "2", "-i", raw]
    base = f"loudnorm=I={s.target_lufs:g}:TP={s.true_peak_db:g}:LRA={s.lra:g}"

    def measure(pre: str) -> dict:
        return parse_loudnorm(ff.run(["-nostats", *inp, "-af", pre + base + ":print_format=json",
                                      "-f", "null", "-"]).stderr)

    m1 = measure("")
    if not math.isfinite(float(m1["input_i"])) or float(m1["input_i"]) < -70:
        ff.run([*inp, "-c:a", "pcm_s16le", out])  # silence: nothing to normalise
        return {"pass1": m1, "limiter": None, "applied": None}
    pre = ""
    gain = s.target_lufs - float(m1["input_i"])
    if float(m1["input_tp"]) + gain > s.true_peak_db:
        limit = 10 ** ((s.true_peak_db - 1.0) / 20)
        pre = f"volume={gain:.2f}dB,alimiter=limit={limit:.4f}:attack=5:release=80:level=false,"
        m1 = measure(pre)
    f = (f"{base}:measured_I={m1['input_i']}:measured_TP={m1['input_tp']}:measured_LRA={m1['input_lra']}"
         f":measured_thresh={m1['input_thresh']}:offset={m1['target_offset']}:linear=true:print_format=json")
    proc = ff.run(["-nostats", *inp, "-af", pre + f + f",aresample={s.sample_rate}", "-c:a", "pcm_s16le", out])
    res = {"pass1": m1, "limiter": pre.rstrip(",") or None, "applied": parse_loudnorm(proc.stderr)}
    return res


def converge_loudness(ff: FFmpeg, wav: Path, s: Settings) -> list[dict]:
    """ASSEMBLY 11-02（ep03 预览 -16.24 LUFS 的根因）：loudnorm 自报的 output_i 不是成品实测——它在 TP 顶不住时退回 dynamic，
    而且 room tone 填底噪之后用 measure_loudness 实测又掉约 1 LU。所以在最后一道工序（room tone 之后）以成品 WAV 的实测为准，
    偏差 > 0.15 LU 就补增益（带限幅），最多 3 次（阈值 0.15，留给 AAC 编码的余量）。返回修正记录"""
    fixes = []
    for k in range(3):
        got = float(measure_loudness(ff, wav)["integrated_lufs"])
        if not math.isfinite(got) or abs(got - s.target_lufs) <= 0.15:
            break
        tmp = wav.with_name(wav.stem + f"_fix{k}.wav")
        limit = 10 ** ((s.true_peak_db - 1.0) / 20)
        ff.run(["-i", wav, "-af", f"volume={s.target_lufs - got:.2f}dB,alimiter=limit={limit:.4f}:attack=5:release=80:level=false",
                "-c:a", "pcm_s16le", tmp])
        os.replace(tmp, wav)
        fixes.append({"measured": got, "gain_db": round(s.target_lufs - got, 2)})
    return fixes


def measure_loudness(ff: FFmpeg, path: Path) -> dict:
    m = parse_loudnorm(ff.run(["-nostats", "-i", path, "-vn", "-af", "loudnorm=print_format=json",
                               "-f", "null", "-"]).stderr)
    return {"integrated_lufs": float(m["input_i"]), "true_peak_dbtp": float(m["input_tp"]),
            "lra": float(m["input_lra"])}


def _color_match_source(ff: FFmpeg, src: dict, idx: int, warnings: list[str], log) -> None:
    """Settings.color_match: picture from <clip>_cc.mp4 (made/reused by ns_post.colormatch). src["path"] stays the
    original (qa.json sha256 gate, H3 audio)."""
    from .colormatch import ensure_cc, short_summary
    info = {}
    for key, out_key in (("path", "path_cc"), ("path_4k", "path_4k_cc")):
        if not src.get(key):
            continue
        try:
            cc, rep = ensure_cc(Path(src[key]), ff=ff, log=log)
        except Exception as e:  # noqa: BLE001 — colour correction is optional; fall back to the original
            warnings.append(f"S{idx}: colour match failed on {Path(src[key]).name}: {e}")
            continue
        if cc is None:
            warnings.append(f"S{idx}: colour match skipped ({rep.get('error')})")
        else:
            src[out_key] = str(cc)
        info[key] = {"file": _rel(cc), "before": short_summary(rep, "before"), "after": short_summary(rep, "after"),
                     "plan": rep.get("plan"), "source_sha256": rep.get("source_sha256")}
    src["color_match"] = info or None


def bgm_cues_for_duck_mode(cues: list, meta: dict, starts: list[float], durations: list[float]
                           ) -> tuple[list, list[str]]:
    """h3_music = duck_under_bgm（整集统一 BGM）：去掉“撤到静默（H3 已带配乐）”这类因 H3 自带配乐而设的 drop
    （改成 continue，强度沿用前一个提示点，没有就 1），整集 BGM 铺满。同时检查 BGM 文件本身是否按旧规则生成
    （编曲里各镜已经“近乎静音”、名义时间轴与实际时间线不符）→ 返回需要重新生成的原因（报告里列出，不静默）"""
    from .music import Cue
    out, cur = [], None
    for c in cues:
        if c.action == "drop" and (c.source == "h3_music" or "H3" in (c.text or "")):
            out.append(Cue(shot=c.shot, start_s=c.start_s, action="continue" if cur else "enter",
                           intensity=cur or 1, source="duck_under_bgm", text=f"整集 BGM（替代：{c.text}）"))
            cur = cur or 1
        else:
            out.append(c)
            if c.action != "drop" and c.intensity:
                cur = c.intensity
    reasons = []
    n_drop = sum(1 for c in cues if c.action == "drop" and (c.source == "h3_music" or "H3" in (c.text or "")))
    if n_drop:
        reasons.append(f"{n_drop} 个提示点是“撤到静默（H3 已带配乐）”，现有 BGM 是按这些静默点作曲的"
                       "（caption 里各镜 drops away to near silence），铺满后大部分仍近乎无声")
    js = [float(x) for x in meta.get("shot_starts") or []]
    if js and len(js) == len(starts):
        dev = max(abs(a - b) for a, b in zip(js, starts))
        if dev > 0.5:
            reasons.append(f"BGM 名义镜头起点与实际时间线最大偏差 {dev:.2f}s（按实际 {round(sum(durations), 2)}s 重新生成）")
    total = round(sum(durations), 2)
    if meta.get("duration_s") and abs(float(meta["duration_s"]) - total) > 0.5:
        reasons.append(f"BGM 长 {meta['duration_s']}s，时间线 {total}s")
    return out, reasons


def _skin_guard_source(src: dict, idx: int, warnings: list[str], log) -> None:
    """FIX（ep02_iter v0 R1）：脸颊 a* 异常块限幅（ns_post.skin_guard，嘴唇排除）→ <画面>_sg.mp4；失败就用原画面"""
    try:
        from .skin_guard import ensure_sg
    except Exception as e:  # noqa: BLE001
        warnings.append(f"S{idx}: skin guard 不可用（{e}）")
        return
    for key, out_key in (("path", "path_sg"), ("path_4k", "path_4k_sg")):
        base = (src.get("path_cc") or src.get("path")) if key == "path" else (src.get("path_4k_cc") or src.get("path_4k"))
        if not base:
            continue
        try:
            out, rep = ensure_sg(Path(base), log=log)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"S{idx}: skin guard 失败（{e}），用原画面")
            continue
        if out is not None:
            src[out_key] = str(out)
            if key == "path":
                src["skin_guard"] = {k: rep.get(k) for k in ("file", "threshold_a", "frames_guarded", "settings")}


def _room_tone_wav(wav: Path, floor_dbfs: float, window_s: float) -> dict:
    """响度归一后的 16-bit WAV：低于 floor_dbfs 的窗口补环境底噪（fill_silence），原地写回"""
    import wave
    with wave.open(str(wav), "rb") as r:
        ch, sw, sr, n = r.getnchannels(), r.getsampwidth(), r.getframerate(), r.getnframes()
        raw = r.readframes(n)
    if sw != 2:
        return {"skipped": f"sample width {sw}"}
    x = np.frombuffer(raw, np.int16).reshape(-1, ch).astype(np.float32) / 32768.0
    before = min_window_dbfs(x, sr, window_s)
    y, rep = fill_silence(x, sr, floor_dbfs, window_s)
    if rep.get("filled_windows"):
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(ch)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes((np.clip(y, -1, 32767 / 32768) * 32768).astype(np.int16).tobytes())
    return {**rep, "min_window_dbfs_before": round(before, 1),
            "min_window_dbfs_after": round(min_window_dbfs(y, sr, window_s), 1)}


# ---------------- episode ----------------

def _concat(ff: FFmpeg, segs: list[Path], out: Path, work: Path) -> Path:
    lst = work / f"{out.stem}.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in segs), encoding="utf-8")
    ff.run(["-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out])
    return out


def load_episode(script: Path, ep: int) -> dict:
    d = json.loads(Path(script).read_text(encoding="utf-8"))
    for e in d["episodes"]:
        if int(e["number"]) == ep:
            return e
    raise KeyError(f"episode {ep} not in {script}")


def _rel(p: Path | str | None) -> str | None:
    if p is None:
        return None
    try:
        return Path(p).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(p)



def _sha256(path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def qa_gate(qa_path: Path, srcs: list[dict], src_list: list[dict]) -> tuple[bool, list[str]]:
    """成片闸门（与 gpu3 约定）：renders_gpu3/epXX/qa.json 的 verdict 必须是 "pass"，
    且每个镜头实际用的视频与 qa.json.shots[SN].video_sha256 一致、该镜 verdict 为 pass。"""
    if not qa_path.is_file():
        return False, ["没有 qa.json（gpu3 未验收）"]
    try:
        qa = json.loads(qa_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return False, [f"qa.json 读不了：{e}"]
    reasons = []
    if qa.get("verdict") != "pass":
        reasons.append(f"整集 verdict={qa.get('verdict')!r}")
    shots = qa.get("shots") or {}
    for row, src in zip(srcs, src_list):
        key = f"S{row['index']}"
        q = shots.get(key)
        if not q:
            reasons.append(f"{key} 不在 qa.json")
            continue
        if q.get("verdict") != "pass":
            reasons.append(f"{key} verdict={q.get('verdict')!r}")
        if src.get("kind") != "video" or not src.get("path"):
            reasons.append(f"{key} 不是视频")
        elif q.get("video_sha256") != _sha256(src["path"]):
            reasons.append(f"{key} 视频与验收版本不一致（sha256）")
        elif src.get("path_cc") and ((src.get("color_match") or {}).get("path") or {}).get("source_sha256") \
                != q.get("video_sha256"):
            reasons.append(f"{key} 校色版 _cc 不是由验收版本视频生成（_cc.json source_sha256 不符）")
    return not reasons, reasons

def normalise_cut_trims(cut_edit: dict | None) -> dict[int, tuple[float, float]]:
    """CUTEDIT：cut_edit["trims"] → {镜号: (裁头秒, 裁尾秒)}；坏值丢弃，负数按 0。"""
    out: dict[int, tuple[float, float]] = {}
    for k, v in ((cut_edit or {}).get("trims") or {}).items():
        try:
            out[int(k)] = (max(0.0, float(v[0])), max(0.0, float(v[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def clamp_trim(head: float, tail: float, src_s: float, min_keep_s: float = 0.2) -> tuple[float, float]:
    """CUTEDIT：手动入出点夹到源片里（镜头重出后新片更短时自动收到片尾），至少留 min_keep_s。"""
    head = min(max(0.0, head), max(0.0, src_s - min_keep_s))
    tail = min(max(0.0, tail), max(0.0, src_s - head - min_keep_s))
    return round(head, 3), round(tail, 3)


def bgm_edit_envelope(n: int, sr: int, edit: dict) -> np.ndarray:
    """CUTEDIT：配乐整体音量（dB）× 线性淡入 / 淡出（秒），长度 n。"""
    g = np.full(n, 10 ** (float(edit.get("gain_db") or 0.0) / 20), dtype=np.float32)
    fi = min(n, int(round(max(0.0, float(edit.get("fade_in_s") or 0)) * sr)))
    fo = min(n, int(round(max(0.0, float(edit.get("fade_out_s") or 0)) * sr)))
    if fi > 0:
        g[:fi] *= np.linspace(0.0, 1.0, fi, dtype=np.float32)
    if fo > 0:
        g[n - fo:] *= np.linspace(1.0, 0.0, fo, dtype=np.float32)
    return g


def assemble_episode(episode: dict, out_dir: Path, *, tts: TtsBackend, asr: AsrBackend | None = None,
                     ff: FFmpeg | None = None, settings: Settings = Settings(), voices_cfg: dict | None = None,
                     data: Path = DATA, cache_dir: Path = CACHE_DIR, sources: dict[int, dict] | None = None,
                     allow_laptop_video: bool = False, keep_work: bool = False, log=print,
                     variant: str = "", cut_edit: dict | None = None, run: str | None = None) -> dict:
    """Build out_dir/epXX.mp4 and out_dir/epXX.timeline.json; returns the timeline dict.
    variant（AUD）：非空时作为对照版另存 <stem>_<variant>.mp4 + <stem>_<variant>.timeline.json，不碰原版、不建别名。
    cut_edit（CUTEDIT，成片页剪辑工程 cut_project.json 归一后的覆写；None = 行为完全不变）：
      {"trims": {镜号: [裁头秒, 裁尾秒]}, "bgm": {"file", "offset_s", "gain_db", "fade_in_s", "fade_out_s"}}
      有手动入出点的镜头：跳过自动的定住段裁切（trim_frozen）和节奏裁切（pace_tighten），以手动为准。"""
    manual_trims = normalise_cut_trims(cut_edit)
    bgm_edit = dict((cut_edit or {}).get("bgm") or {})
    ff = ff or FFmpeg.discover()
    # 音色表：调用方按剧传（compose / cut_export 传 load_voice_config(run=...)）；不传时 V2 会 VoiceRunRequired（失败关闭）
    cfg = voices_cfg or (load_voice_config(run=run) if run is not None else load_voice_config())
    ep = int(episode["number"])
    s = resolve_settings(settings, ep)      # FIX：合成硬规则 / h3_music 等 None → config/render_quality.json post.*
    if DIRP.enabled("shot_music_off") and s.h3_music != "keep":   # K-EDT-820：停掉镜头级配乐（H3 轨只留对白）
        from dataclasses import replace as _dc_replace
        s = _dc_replace(s, h3_music="dialogue_only")
    name = f"ep{ep:02d}"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    t_start = time.perf_counter()
    timings: dict[str, float] = {}
    shots = sorted(episode["shots"], key=lambda x: int(x["index"]))
    dplan = DIRP.plan_for(episode) if DIRP.enabled() else {}   # K-EDT-813/814：导演规格（关 = 空，行为不变）
    script_durations = [float(x.get("duration") or 10) for x in shots]
    # G13 (owner 2026-10-30): the script writes whole seconds, H3 decides the real clip length. A shot with a
    # video is assembled at the video's own duration; only placeholder stills fall back to the script seconds.
    found = []
    for shot in shots:
        idx = int(shot["index"])
        src = (sources or {}).get(idx) or find_source(ep, idx, data=data, allow_laptop_video=allow_laptop_video,
                                                       settings=s)
        src = {k: (str(v) if isinstance(v, Path) else v) for k, v in dict(src).items()}
        found.append((src, ff.info(src["path"]) if src["kind"] == "video" else None))
    # FIX（ep02_iter v0 DIR：10/11 镜结尾定住 0.67–6.5 s）：剪掉开头 / 结尾定住段（帧差 <0.35 连续 ≥0.3 s，至少留 1.5 s）
    diffs_by_i: dict[int, tuple] = {}   # V2：pace_trims 用（帧差, fps）
    if _rule(s, "trim_frozen"):
        for i_f, (src, info) in enumerate(found):
            if src["kind"] != "video" or not info:
                continue
            if int(shots[i_f]["index"]) in manual_trims:   # CUTEDIT：手动入出点优先
                continue
            fps_src = float(info.get("fps") or s.fps)
            try:
                d = frame_diffs(src["path"], ff)
            except Exception as e:  # noqa: BLE001 — 量不出来就不剪
                src["trim_error"] = str(e)[:200]
                continue
            diffs_by_i[i_f] = (d, fps_src)
            hf, tf = freeze_trims(d, fps_src, still_diff=float(_rule(s, "freeze_still_diff")),
                                  min_s=float(_rule(s, "freeze_min_s")), min_keep_s=float(_rule(s, "min_keep_s")))
            src["trim_head_s"], src["trim_tail_s"] = round(hf / fps_src, 3), round(tf / fps_src, 3)
    for i_f, (src, info) in enumerate(found):   # CUTEDIT：手动入出点（夹到源片长度内，至少留 0.2 s）
        mt = manual_trims.get(int(shots[i_f]["index"]))
        if mt is None or src["kind"] != "video" or not info:
            continue
        src["trim_head_s"], src["trim_tail_s"] = clamp_trim(mt[0], mt[1], float(info["duration"]))
        src["manual_trim"] = True
    durations = [round(info["duration"] - float(src.get("trim_head_s") or 0) - float(src.get("trim_tail_s") or 0), 3)
                 if info else script_durations[i] for i, (src, info) in enumerate(found)]
    color_match = color_match_enabled(ep, s)

    # 1. lines + TTS
    lines = resolve_episode(episode, cfg.get("line_overrides"), narrator=default_voiceover_speaker(cfg),
                            cast=Cast.from_cfg(cfg))   # 旁白 / 说话人表都按本剧音色表取（V2 = cast_table 人物表）
    # V2（项目方 10-31 原话：选项 "A. 去掉"——ep02 不保留林晚内心独白）：post.voiceover_drop 按集列出去掉独白的镜头
    drop_vo = voiceover_drop_shots(ep, s)
    if drop_vo:
        dropped = [ln.key for ln in lines if ln.kind == "voiceover" and int(ln.shot) in drop_vo]
        lines = [ln for ln in lines if not (ln.kind == "voiceover" and int(ln.shot) in drop_vo)]
    else:
        dropped = []
    placements, requests, req_index = [], [], []
    for ln in lines:
        vs: list[Voice] = voice_for(cfg, ln.speaker, voiceover=ln.kind == "voiceover")
        p = Placement(ln, [f"{v.id} (sid {v.sid}, speed {v.speed:g})" for v in vs], 0.0)
        placements.append(p)
        for v in vs:
            req_index.append((len(placements) - 1, v))
            requests.append((ln.text, v))
    t = time.perf_counter()
    paths = synthesize_cached(tts, requests, cache_dir) if requests else []
    timings["tts_s"] = round(time.perf_counter() - t, 2)
    for (pi, _v), path in zip(req_index, paths):
        placements[pi].files.append(str(path))
    for p in placements:
        p.clip_s = round(max(wav_duration(f) for f in p.files), 3)

    # 2. ASR round trip (one audible voice per line; chorus checked on its first voice)
    asr_rows: dict[int, dict] = {}
    if asr is not None and placements:
        t = time.perf_counter()
        heard = asr.transcribe([Path(p.files[0]) for p in placements], "zh")
        for i, (p, h) in enumerate(zip(placements, heard)):
            cer = char_error_rate(p.line.text, h)
            asr_rows[i] = {"heard": h, "cer": round(cer, 3), "ok": cer <= asr_limit(p.line.text)}
        timings["asr_s"] = round(time.perf_counter() - t, 2)

    # 2b. AUD：对白来源 = H3 原声 → 逐镜回听 H3 轨，合格句用 H3、不合格整镜 TTS 兜底（ns_post.h3_dialogue）
    dialogue_source = dialogue_source_for(ep, s)
    h3_report, pre_warnings = None, []
    vc_cfg = None   # VOX：config/render_quality.json post.voice_conversion（默认关；只在 h3 对白模式下用）
    if dialogue_source == "h3":
        from .voice_convert import load_hook
        vc_cfg = load_hook(QUALITY_CONFIG)
    if dialogue_source == "h3":
        from .h3_dialogue import apply_h3_dialogue
        t = time.perf_counter()
        try:
            _edits = {int(x["index"]): read_edit_music(data / "renders", ep, int(x["index"])) for x in shots}
            music_src = h3_music_shots(shots, episode, {k: v for k, v in _edits.items() if v},
                                       read_edit_music(data / "renders", ep))
        except Exception:  # noqa: BLE001 — 只用于报告
            music_src = None
        Path(cache_dir).parent.mkdir(parents=True, exist_ok=True)   # fredw-92：默认改 h3 后缓存目录可能还没建
        h3_tmp = Path(tempfile.mkdtemp(prefix=f"{name}_h3asr_", dir=str(Path(cache_dir).parent)))
        try:
            h3_report, pre_warnings = apply_h3_dialogue(ff, asr, found, shots, durations, placements, s, h3_tmp,
                                                        music_src)
        finally:
            shutil.rmtree(h3_tmp, ignore_errors=True)
        timings["h3_asr_s"] = round(time.perf_counter() - t, 2)

    # 2c. V2 节奏：对白前后空档、无对白镜上限（pace_trims）；H3 原声句（fixed）跟着平移
    pace_report = []
    if _rule(s, "pace_tighten"):
        old_starts = [sum(durations[:k]) for k in range(len(durations))]
        new_durs = list(durations)
        shifts = {}
        for i, (src, info) in enumerate(found):
            if src["kind"] != "video" or not info or src.get("manual_trim"):   # CUTEDIT：手动入出点不再加节奏裁切
                continue
            mine = [p for p in placements if p.line.shot == i + 1]
            has_vo = any(p.line.kind == "voiceover" for p in mine)
            dl = [p for p in mine if p.line.kind == "dialogue"]
            fixed = [(p.start_s - old_starts[i], p.end_s - old_starts[i]) for p in dl if p.fixed]
            tts = None
            if dl and not fixed:
                tts = sum(p.clip_s for p in dl) + s.dialogue_gap_s * (len(dl) - 1)
            elif dl and len(fixed) < len(dl):   # 部分 H3、部分 TTS：只按 H3 句收开头，结尾不动
                fixed = [(min(a for a, _ in fixed), durations[i] - float(_rule(s, "pace_tail_pad_s")))]
            d, fps_d = diffs_by_i.get(i, (None, s.fps))
            if d is not None:
                h0 = int(round(float(src.get("trim_head_s") or 0) * fps_d))
                t0n = int(round(float(src.get("trim_tail_s") or 0) * fps_d))
                d = d[h0: len(d) - t0n if t0n else None]
            hx, tx = pace_trims(durations[i], lines=fixed, tts_speech_s=tts, has_vo=has_vo,
                                lead_s=float(_rule(s, "pace_lead_s")), tail_pad_s=float(_rule(s, "pace_tail_pad_s")),
                                idle_max_s=float(_rule(s, "pace_idle_max_s")),
                                min_keep_s=max(float(_rule(s, "min_keep_s")), float(_rule(s, "pace_min_keep_s"))),
                                diffs=d, fps=fps_d)
            if hx <= 0 and tx <= 0:
                continue
            src["trim_head_s"] = round(float(src.get("trim_head_s") or 0) + hx, 3)
            src["trim_tail_s"] = round(float(src.get("trim_tail_s") or 0) + tx, 3)
            src["pace_trim"] = [hx, tx]
            new_durs[i] = round(durations[i] - hx - tx, 3)
            shifts[i] = hx
            pace_report.append({"shot": i + 1, "head_s": hx, "tail_s": tx, "from_s": durations[i], "to_s": new_durs[i],
                                "why": "独白镜不动" if has_vo else ("对白" if dl else "无对白（取运动量最大段）")})
        new_starts = [sum(new_durs[:k]) for k in range(len(new_durs))]
        for p in placements:
            i = p.line.shot - 1
            if p.fixed and 0 <= i < len(new_durs):
                p.start_s = round(new_starts[i] + (p.start_s - old_starts[i]) - shifts.get(i, 0.0), 3)
        if h3_report:
            for r in h3_report:
                hx = shifts.get(int(r.get("shot", 0)) - 1, 0.0)
                for ln in r.get("lines", []):
                    for k in ("t0", "t1"):
                        if ln.get(k) is not None:
                            ln[k] = round(ln[k] - hx, 3)
        durations = new_durs

    # 3. plan
    starts, tail, warnings = plan_timeline(durations, placements, s)
    warnings[:0] = pre_warnings
    for i, row in asr_rows.items():
        if placements[i].source != "tts":  # AUD：H3 原声句不播 TTS，TTS 回听结果只记录不报警
            continue
        if not row["ok"]:
            warnings.append(f"{placements[i].line.key}: ASR heard '{row['heard']}' (CER {row['cer']:.2f})")

    # 4. picture
    total = sum(durations) + tail
    srcs, src_list = [], []
    work_root = Path(cache_dir).parent / "post"
    work_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"{name}_", dir=str(work_root)))
    deliveries: dict[str, dict] = {}
    bgm_info: dict = {"used": False}
    try:
        t = time.perf_counter()
        seg_files = []
        for i, shot in enumerate(shots):
            idx = int(shot["index"])
            src, info = found[i]
            seconds = durations[i] + (tail if i == len(shots) - 1 else 0.0)
            if src.get("missing"):   # FIX P0 ①：没有合格 take → 标缺（占位），不静默用废片
                warnings.append(f"S{idx}: 缺镜——没有合格视频版本（{src.get('missing_reason')}），用占位 {src['kind']}")
            elif src["kind"] != "video":
                warnings.append(f"S{idx}: no gpu3 video — placeholder {src['kind']} clip"
                                + (f" from {_rel(src['path'])}" if src.get("path") else ""))
            mqw = motion_qc_warning(idx, src)     # QCV：静止 / 结尾定住的镜头在合成警告里列出
            if mqw:
                warnings.append(mqw)
            if color_match and src["kind"] == "video":
                _color_match_source(ff, src, idx, warnings, log)
            if _rule(s, "skin_guard") and src["kind"] == "video":
                _skin_guard_source(src, idx, warnings, log)
            # FIX（DIR：S8 12.25 s、S11 10.1 s 一刀到底）：单镜 > max_shot_s 拆刀，后一刀同素材推近换景别，切点避开对白句
            busy = [(max(0.0, p.start_s - starts[i]), p.end_s - starts[i]) for p in placements
                    if p.line.shot == i + 1 and p.line.kind == "dialogue"]
            pieces = split_pieces(durations[i], max_s=DIRP.shot_max_s(dplan, idx, float(_rule(s, "max_shot_s") or 0)),
                                  min_piece_s=float(_rule(s, "split_min_piece_s")), zoom=float(_rule(s, "split_zoom")),
                                  busy=busy) if src["kind"] == "video" else [(0.0, durations[i], 1.0)]
            if i == 0 and src["kind"] == "video":   # FIX：开场第一刀 ≤ open_first_cut_s
                pieces = opening_pieces(pieces, first_cut_s=float(_rule(s, "open_first_cut_s") or 0),
                                        min_piece_s=float(_rule(s, "open_min_piece_s")),
                                        zoom=float(_rule(s, "split_zoom")), busy=busy)
            if len(pieces) > 1 and seconds > durations[i]:   # 末镜的尾帧保持算进最后一刀
                pieces[-1] = (pieces[-1][0], round(seconds, 3), pieces[-1][2])
            src["pieces"] = pieces
            seg_files += render_shot(ff, src, seconds, pieces, work, f"seg{idx}", s)
            src_list.append(src)
            srcs.append({"shot": f"{name}-S{idx}", "index": idx, "start_s": round(starts[i], 3),
                         "duration_s": durations[i], "duration_source": shot.get("duration_source"),
                         "script_duration_s": script_durations[i], "duration_from": "h3_video" if info else "script",
                         "picture_s": round(seconds, 6), "kind": src["kind"], "origin": src.get("origin"),
                         "placeholder": src["kind"] != "video", "file": _rel(src.get("path")),
                         "file_4k": _rel(src.get("path_4k")),
                         "source_duration_s": round(info["duration"], 3) if info else None,
                         "source_size": [info["width"], info["height"]] if info else None,
                         "trimmed_s": round(max(0.0, info["duration"] - durations[i]), 3) if info else None,
                         "h3_audio": bool(info and info["has_audio"]), "adult": shot.get("adult"),
                         # FIX：用的是哪一版、缺镜原因、剪掉的定住段、拆刀
                         "take": src.get("take"), "take_note": src.get("take_note"),
                         "take_policy": src.get("take_policy") or ("sources_override" if sources else None),
                         "missing": bool(src.get("missing")), "missing_reason": src.get("missing_reason"),
                         "trim_head_s": src.get("trim_head_s"), "trim_tail_s": src.get("trim_tail_s"),
                         **({"manual_trim": True} if src.get("manual_trim") else {}),
                         "pieces": [list(p) for p in src.get("pieces") or []] if len(src.get("pieces") or []) > 1
                         else None, "skin_guard": src.get("skin_guard"),
                         **({"color_match": src.get("color_match")} if color_match else {})})
        picture = _concat(ff, seg_files, work / "picture.mp4", work)
        timings["picture_s"] = round(time.perf_counter() - t, 2)

        # 5. sound
        t = time.perf_counter()
        sr = s.sample_rate
        n_total = round(sum(round(x["picture_s"] * s.fps) for x in srcs) / s.fps * sr)
        dlg = [(p.start_s - s.gate_pad_s, p.end_s + s.gate_pad_s) for p in placements if p.line.kind == "dialogue"]
        vo = [(p.start_s - s.gate_pad_s, p.end_s + s.gate_pad_s) for p in placements if p.line.kind == "voiceover"]
        # AUD：H3 原声句（source h3）不压 H3 轨 / 人声分轨，只让 BGM 和其他分轨避让；tts 模式下两者相同
        dlg_tts = [(p.start_s - s.gate_pad_s, p.end_s + s.gate_pad_s) for p in placements
                   if p.line.kind == "dialogue" and p.source == "tts"]
        dlg_h3 = [(p.start_s - s.gate_pad_s, p.end_s + s.gate_pad_s) for p in placements
                  if p.line.kind == "dialogue" and p.source == "h3"]

        def env(dlg_db: float, vo_db: float, gate: list | None = None, h3_bed: bool = False) -> np.ndarray:
            spans = [(a, b, dlg_db) for a, b in (dlg if gate is None else gate) if dlg_db > 0] + \
                    [(a, b, vo_db) for a, b in vo if vo_db > 0]
            g = gain_envelope(n_total, sr, spans, s.gate_fade_s)[:, None]
            if gate is None:
                return g
            keep = (1 - gain_envelope(n_total, sr, [(a, b, 200.0) for a, b in dlg_h3], s.gate_fade_s)[:, None]) \
                if dlg_h3 else np.zeros((n_total, 1), dtype=np.float32)
            g = g + (1 - g) * keep  # 画面上正在说的 H3 原声句：独白压过来时也不压它
            if h3_bed and s.h3_music == "duck_under_bgm" and s.bgm:  # H3 配乐压到 BGM 下（对白处分不开，保留）
                g = g * (keep + (1 - keep) * np.float32(10 ** (-s.h3_music_duck_db / 20)))
            elif h3_bed and s.h3_music == "dialogue_only":   # K-EDT-820（项目方 13:1x：镜头级配乐断层）：H3 轨只留画内对白句，其余交给集级 BGM + 场景底噪
                g = g * keep
            return g.astype(np.float32)

        duck = {  # role -> (dB under TTS dialogue, dB under voice-over)
            "h3": (s.gate_db, s.vo_duck_db), "vocals": (s.gate_db, s.vo_duck_db),
            "scene": (s.scene_duck_dialogue_db, s.scene_duck_vo_db), "foley": (s.foley_duck_db, s.foley_duck_db),
        }
        layers: dict[tuple, np.ndarray] = {}  # (role, dB under TTS dialogue, dB under voice-over) -> track
        for i, row in enumerate(srcs):
            k0 = round(starts[i] * sr)
            k1 = min(n_total, k0 + round(durations[i] * sr))
            stems, err = load_stems(data / "renders_gpu3" / name / f"S{row['index']}")
            if err:
                warnings.append(f"S{row['index']}: {err}")
            head = float(src_list[i].get("trim_head_s") or 0.0)
            if stems:
                used = []
                for st in stems:
                    rel = float(st["offset_s"]) - head     # FIX：开头定住段剪掉后分轨同步前移
                    a = read_audio(ff, st["path"], sr, seconds=durations[i] + min(0.0, rel), work=work,
                                   start_s=max(0.0, -rel))
                    a = _fade_edges(a, sr, s.edge_fade_s) * np.float32(10 ** (st["gain_db"] / 20))
                    off = round(max(0.0, rel) * sr)
                    a0 = k0 + max(0, off)
                    a = a[max(0, -off):][: max(0, k1 - a0)]
                    role = st["role"] if st["role"] in duck else "foley"
                    d_dlg, d_vo = duck[role]
                    if role != "vocals" and st.get("duck_under_voice_db") is not None:  # vocals stay gated
                        d_dlg = abs(float(st["duck_under_voice_db"]))
                    if st.get("duck_under_vo_db") is not None:
                        d_vo = abs(float(st["duck_under_vo_db"]))
                    key = (role, d_dlg, d_vo)
                    layers.setdefault(key, np.zeros((n_total, 2), dtype=np.float32))[a0:a0 + len(a)] += a
                    used.append({"name": st["name"], "file": _rel(st["path"]), "gain_db": st["gain_db"],
                                 "offset_s": st["offset_s"], "role": role, "duck_under_dialogue_db": d_dlg,
                                 "duck_under_vo_db": d_vo})
                row["audio"], row["stems"] = "stems", used
            elif row["h3_audio"]:
                a_path = src_list[i]["path"]
                if vc_cfg and h3_report:   # VOX：post.voice_conversion（默认关）只换该角色那句的时间段
                    rep = next((r for r in h3_report if r["shot"] == row["index"]), None) or {}
                    keys = {p.line.key for p in placements if p.source == "h3" and p.line.shot == i + 1
                            and p.line.speaker == vc_cfg["speaker"]}
                    spans = [(ln["t0"] + head, ln["t1"] + head) for ln in rep.get("lines", [])   # FIX：剪头后换回原片时间
                             if ln.get("key") in keys and ln.get("t0") is not None and ln.get("t1") is not None]
                    if spans:
                        from .voice_convert import assembler_audio
                        conv, why = assembler_audio(Path(a_path), spans, vc_cfg, work / f"vc{row['index']}")
                        if conv:
                            a_path = conv
                            row["voice_conversion"] = {"speaker": vc_cfg["speaker"], "reference": vc_cfg["reference"],
                                                       "spans_s": spans, "file": _rel(conv)}
                        elif why:
                            warnings.append(f"S{row['index']}: 变声未做（{why}），用 H3 原声")
                # FIX（DIR 切点跳变）：切点交叉淡化 edge_fade_s——本镜音轨前后各多读半个淡化长度，与相邻镜重叠、互补斜坡
                xf = s.edge_fade_s
                pre = min(xf / 2, head) if i > 0 else 0.0
                # V2 J-cut（项目方“transition 非常生硬”）：下一镜的声音提前 jl_cut_s 进入（用剪掉的开头里的原声），
                # 上一镜最后 jl_cut_s 内没有对白 / 独白时才做，不盖住上一镜的话
                jl = DIRP.jcut_s(dplan, int(row["index"]), float(_rule(s, "jl_cut_s") or 0))   # K-EDT-814
                if i > 0 and jl > pre and head > pre:
                    cut_t = starts[i]
                    clash = any(p.start_s < cut_t and p.end_s > cut_t - jl for p in placements
                                if p.line.shot == i)   # 上一镜（shot 号 = i）的句子
                    if not clash:
                        pre = min(jl, head)
                        row["j_cut_s"] = round(pre, 3)
                post = xf / 2 if i < len(srcs) - 1 else 0.0
                a = read_audio(ff, a_path, sr, seconds=durations[i] + pre + post, work=work, start_s=head - pre)
                a = _fade_edges(a, sr, xf)
                if pre > xf:   # J-cut：淡入拉长到整段提前量
                    m = min(len(a), round(pre * sr))
                    a[:m] *= np.linspace(0.0, 1.0, m, dtype=np.float32)[:, None]
                a = a * np.float32(10 ** (s.h3_gain_db / 20))
                a0 = max(0, k0 - round(pre * sr))
                a = a[: max(0, n_total - a0)]
                layers.setdefault(("h3", *duck["h3"]), np.zeros((n_total, 2), dtype=np.float32))[a0:a0 + len(a)] += a
                row["audio"] = "h3_track"
            else:
                row["audio"] = "none"
        mix = np.zeros((n_total, 2), dtype=np.float32)
        for (_role, d_dlg, d_vo), x in layers.items():
            if dialogue_source == "h3" and _role in ("h3", "vocals"):  # AUD：只在 TTS 兜底句处压 H3 人声
                mix += x * env(d_dlg, d_vo, gate=dlg_tts, h3_bed=_role == "h3")
            else:
                mix += x * env(d_dlg, d_vo)
        for p in placements:
            if p.source == "h3":  # AUD：H3 原声句不叠 TTS
                continue
            g = 10 ** (s.chorus_gain_db / 20) if len(p.files) > 1 else 1.0
            for f in p.files:
                a = read_audio(ff, f, sr, channels=1, tempo=p.tempo, work=work)[:, 0]
                if p.cut_s is not None:
                    a = a[: round(p.cut_s * sr)]
                    k = min(len(a), round(0.08 * sr))
                    if k:
                        a[-k:] *= np.linspace(1, 0, k, dtype=np.float32)
                k0 = round(p.start_s * sr)
                a = a[: max(0, n_total - k0)]
                mix[k0:k0 + len(a)] += (a * g)[:, None]

        # BGM (gpu3 T40 file + cue timeline; skipped with a warning when absent)
        if not s.bgm:
            bgm_info = {"used": False, "reason": "disabled"}
        else:
            mdir = (Path(s.music_dir) if Path(s.music_dir).is_absolute() else ROOT / s.music_dir) if s.music_dir \
                else data / "music"
            music_file, music_meta = find_music(ep, mdir)
            if bgm_edit.get("file"):   # CUTEDIT：成片页换曲
                bf = Path(str(bgm_edit["file"]))
                bf = bf if bf.is_absolute() else ROOT / bf
                if bf.is_file():
                    music_file = bf
                    try:
                        music_meta = json.loads(bf.with_suffix(".json").read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        music_meta = {}
                else:
                    warnings.append(f"剪辑工程里的配乐 {bgm_edit['file']} 找不到，用本集默认配乐")
            if music_file is None:
                warnings.append(f"no BGM: {_rel(mdir)}/{name}.(wav|flac|mp3) not found — music layer skipped")
                bgm_info = {"used": False, "reason": "no file"}
            else:
                edits = {int(x["index"]): read_edit_music(data / "renders", ep, int(x["index"])) for x in shots}
                edits = {k: v for k, v in edits.items() if v}
                cues = resolve_cues(shots, starts, music_meta, edits)
                bgm_regen = []
                if s.h3_music in ("duck_under_bgm", "dialogue_only"):   # 项目方 2026-10-31“整集统一 BGM”：按镜“撤到静默（H3 已带配乐）”不再生效
                    cues, bgm_regen = bgm_cues_for_duck_mode(cues, music_meta, starts, durations)
                director_cues = []
                if DIRP.enabled():   # K-EDT-804：配乐强度跟剧本情绪节拍（时间轴保留时间和描述）
                    cues, director_cues = DIRP.merge_cues(cues, shots, starts)
                    for r in bgm_regen:
                        warnings.append(f"BGM 需要重新生成整集版（data/music/{name}.wav）：{r}")
                stale = stale_timeline(music_meta, len(shots))
                if stale:   # 封板修复：旧版剧本的提示时间轴不再按镜号套用
                    warnings.append(f"BGM {name}.json is stale ({stale}): its cue timeline is ignored and the script "
                                    f"【配乐】/ edits are used; regenerate the BGM on gpu3 (T40)")
                for k, v in ignored_edits(music_meta, edits, len(shots)).items():
                    warnings.append(f"S{k}: owner music edit '{v}' not applied — {name}.json carries a cue "
                                    f"timeline; regenerate the BGM on gpu3 (T40) to include it")
                m = normalise_rms(read_audio(ff, music_file, sr, work=work))
                if bgm_edit:   # CUTEDIT：从曲中第几秒开始
                    m = m[int(round(max(0.0, float(bgm_edit.get("offset_s") or 0)) * sr)):]
                g = level_curve(cues, n_total, sr, total)[:, None]
                if len(m) < n_total and float(g[len(m):].max()) < 1e-3:  # cues are silent past its end: pad
                    m, looped = np.concatenate([m, np.zeros((n_total - len(m), 2), np.float32)]), False
                else:
                    m, looped = fit_length(m, n_total, sr)
                if looped:
                    warnings.append(f"BGM {music_file.name} is shorter than the episode: looped")
                if bgm_edit:   # CUTEDIT：整体音量 + 淡入淡出
                    m = m * bgm_edit_envelope(n_total, sr, bgm_edit)[:, None]
                shot_duck = np.zeros(n_total, dtype=np.float32)
                for c in cues:
                    if c.duck_db and c.shot:
                        a0 = round(starts[c.shot - 1] * sr)
                        shot_duck[a0:a0 + round(durations[c.shot - 1] * sr)] = c.duck_db
                mduck = music_meta.get("duck") if isinstance(music_meta.get("duck"), dict) else {}
                ducked = env(float(mduck.get("dialogue_db", s.bgm_duck_dialogue_db)), float(mduck.get("vo_db", s.bgm_duck_vo_db)))
                # gpu3 duck_spans: H3 dialogue / vocal activity (absolute seconds) — duck there too
                h3_spans = []
                for d in music_meta.get("duck_spans") or []:
                    try:
                        h3_spans.append((float(d["start_s"]) - s.gate_pad_s, float(d["end_s"]) + s.gate_pad_s,
                                         abs(float(d.get("duck_db", s.bgm_duck_h3_db)))))
                    except (KeyError, TypeError, ValueError):
                        continue
                if h3_spans:
                    ducked = np.minimum(ducked, gain_envelope(n_total, sr, h3_spans, s.gate_fade_s)[:, None])
                # X-4 / E8: the H3 clip already has its own music → mute the BGM over that shot
                h3_music = h3_music_shots(shots, episode, edits, read_edit_music(data / "renders", ep)) \
                    if s.bgm_skip_h3_music and s.h3_music != "duck_under_bgm" else {}  # AUD：压 H3 配乐时 BGM 全程
                pos = {int(x["index"]): k for k, x in enumerate(shots)}
                mute = [(starts[pos[i]], starts[pos[i]] + durations[pos[i]], 90.0) for i in sorted(h3_music)]
                if mute:
                    ducked = np.minimum(ducked, gain_envelope(n_total, sr, mute, s.gate_fade_s)[:, None])
                speaking = ducked[:, 0] < 0.999
                extra = np.where(speaking, 10 ** (-shot_duck / 20), 1.0).astype(np.float32)[:, None]
                mix += m * g * ducked * extra
                bgm_info = {"used": True, "file": _rel(music_file),
                            "meta": {k: music_meta.get(k) for k in ("bpm", "model", "seed", "sha256", "prompt")
                                     if k in music_meta},
                            "episode_edit_music": read_edit_music(data / "renders", ep),
                            "script_music": episode.get("music_bgm") or episode.get("music") or None,
                            "cues": [c.to_dict() for c in cues], "looped": looped, "director_cues": director_cues,
                            "cue_source": "music_json" if music_meta.get("cues") else
                            ("script/edit" if any(c.source in ("script", "edit") for c in cues) else "default"),
                            "duck_spans": len(h3_spans), "h3_music_mode": s.h3_music,
                            "regenerate_needed": bgm_regen,
                            **({"cut_edit": bgm_edit} if bgm_edit else {}),
                            "h3_music_shots": {f"S{k}": v for k, v in sorted(h3_music.items())}}
        raw = work / "mix.f32"
        mix.astype(np.float32).tofile(raw)
        wav = work / "mix.wav"
        loud_passes = loudnorm(ff, raw, wav, s)
        room_info = None
        if _rule(s, "room_tone"):   # FIX：不允许数字静音——响度归一后，低于底噪的窗口补环境底噪
            room_info = _room_tone_wav(wav, float(_rule(s, "room_tone_floor_dbfs")),
                                       float(_rule(s, "room_tone_window_s")))
        loud_passes["corrections"] = converge_loudness(ff, wav, s)
        timings["sound_s"] = round(time.perf_counter() - t, 2)

        # 6. mux (+ 4K delivery with the same sound)
        t = time.perf_counter()
        want_4k = s.deliver_4k == "on" or (s.deliver_4k == "auto" and any(x.get("path_4k") for x in src_list))
        # 有占位镜头（无 gpu3 视频）或 gpu3 QA 未通过时绝不叫 final：项目方会把预览当成片（gpu3 4K 只从 _final 取音频）
        qa_ok, qa_reasons = qa_gate(data / "renders_gpu3" / name / "qa.json", srcs, src_list)
        # FIX：缺镜 / 对白独白溢出 → 合成判不通过（只出预览）
        gate_missing = [{"shot": f"S{x['index']}", "reason": x.get("missing_reason")} for x in srcs if x.get("missing")]
        gate_overflow = [{"key": p.line.key, "shot": f"S{p.line.shot}", "kind": p.line.kind, "overflow_s": p.overflow_s}
                         for p in placements if p.overflow_s > 0]
        preview = any(x.get("placeholder") for x in srcs) or not qa_ok or bool(gate_missing) or bool(gate_overflow)
        warnings.extend(f"QA 闸门：{r}" for r in qa_reasons)
        stem = f"{name}_PREVIEW_未完成" if preview else f"{name}_final"
        if variant:  # AUD：对照版另存，不覆盖原版
            stem = f"{stem}_{variant}"
        final = out_dir / f"{stem}.mp4"
        if preview:
            # fredw-92/D1：写明真实原因（ep02 全部 11 镜都是视频、只差 gpu3 qa.json 时，以前也说“有占位镜头”）
            why = ("缺镜（没有合格 take）" if gate_missing else "有占位镜头" if any(x.get("placeholder") for x in srcs)
                   else "对白 / 独白溢出镜头" if gate_overflow else "gpu3 QA 未通过 / 没有 qa.json")
            warnings.append(f"PREVIEW：{why}，输出为预览（不是成片），不生成 epXX_final / epXX 别名")
        outputs = [("1080p", picture, final)]
        if want_4k:
            segs4 = []
            for i, src in enumerate(src_list):
                seg = work / f"seg{srcs[i]['index']}_4k.mp4"
                render_segment(ff, src, srcs[i]["picture_s"], seg, s, uhd=True)
                segs4.append(seg)
                srcs[i]["picture_4k"] = "4k_clip" if src.get("path_4k") else (
                    "upscaled_lanczos" if src["kind"] != "black" else "black")
            outputs.append(("4k", _concat(ff, segs4, work / "picture_4k.mp4", work), out_dir / f"{stem}_4k.mp4"))
            missing = [x["index"] for x in srcs if x.get("picture_4k") != "4k_clip"]
            if missing:
                warnings.append(f"4K delivery: no 4K clip for S{', S'.join(map(str, missing))} — "
                                f"upscaled (lanczos) from the lower-resolution source")
        expected = sum(round(x["picture_s"] * s.fps) for x in srcs)
        for label, pic, out in outputs:
            ff.run(["-i", pic, "-i", wav, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
                    "-b:a", "192k", "-ar", str(sr), "-ac", "2", "-movflags", "+faststart", "-shortest", out])
            n_frames = ff.count_frames(out)
            if n_frames != expected:
                warnings.append(f"{label}: frame count {n_frames} != expected {expected}")
            inf = ff.info(out)
            deliveries[label] = {"file": _rel(out), "size": [inf["width"], inf["height"]], "frames": n_frames,
                                 "duration_s": round(inf["duration"], 3), "video_codec": inf["video_codec"],
                                 "audio_codec": inf["audio_codec"]}
        if not preview and not variant:
            alias = out_dir / f"{name}.mp4"
            alias.unlink(missing_ok=True)
            try:
                os.link(final, alias)
            except OSError:
                shutil.copyfile(final, alias)
            deliveries["1080p"]["alias"] = _rel(alias)
        loud = measure_loudness(ff, outputs[0][2])
        info = ff.info(outputs[0][2])
        frames = deliveries["1080p"]["frames"]
        timings["mux_s"] = round(time.perf_counter() - t, 2)
    finally:
        if not keep_work:
            shutil.rmtree(work, ignore_errors=True)
    timings["total_s"] = round(time.perf_counter() - t_start, 2)

    speech = []
    for i, p in enumerate(placements):
        ln = p.line
        speech.append({"key": f"{name}-{ln.key}", "kind": ln.kind, "shot": ln.shot, "speaker": ln.speaker,
                       "speaker_name": cfg["characters"].get(ln.speaker, {}).get("name", ln.speaker),
                       "voices": p.voices, "text": ln.text, "note": ln.note, "resolved_by": ln.how,
                       "start_s": p.start_s, "end_s": round(p.end_s, 3),
                       "tts_s": p.clip_s if p.source == "tts" else round(max(wav_duration(f) for f in p.files), 3),
                       "tempo": p.tempo, "cut_s": p.cut_s, "files": [_rel(f) for f in p.files],
                       "asr": asr_rows.get(i)})
        if dialogue_source == "h3":  # AUD：这句实际播的是谁、H3 回听结果
            speech[-1]["source"] = p.source
            speech[-1]["h3_asr"] = next((dict(r) for row in h3_report or [] for r in row["lines"]
                                         if r["key"] == ln.key), None)
        if ln.kind == "voiceover":  # AUD：独白放不放得下本镜、压到了哪几句对白
            k = ln.shot - 1
            shot_end = starts[k] + durations[k]
            hits = [q.line.key for q in placements if q.line.kind == "dialogue"
                    and q.start_s < p.end_s and p.start_s < q.end_s]
            speech[-1]["vo_fit"] = {"fits_shot": p.end_s <= shot_end + 1e-3,
                                    "overflow_s": round(max(0.0, p.end_s - shot_end), 3),
                                    "overlaps_dialogue": hits}
    timeline = {
        "episode": ep, "title": episode.get("title"), "output": _rel(final), "preview": preview,
        "duration_s": round(info["duration"], 3), "frames": frames, "fps": s.fps,
        "size": [s.width, s.height], "video_codec": info["video_codec"], "audio_codec": info["audio_codec"],
        "subtitles": False, "tail_hold_s": tail,
        **({"cut_edit": cut_edit} if cut_edit else {}),
        "deliveries": deliveries,
        "loudness": {"target_lufs": s.target_lufs, "measured": loud, "loudnorm": loud_passes},
        "bgm": bgm_info,
        "tts": {"backend": getattr(tts, "name", type(tts).__name__), "model": cfg.get("tts_model", {}).get("id"),
                "asr": "sensevoice" if asr is not None and type(asr).__name__ == "SenseVoiceAsr"
                else (type(asr).__name__ if asr else None)},
        "shots": srcs, "speech": speech, "warnings": warnings, "timings": timings, "settings": {**s.to_dict(), "color_match": color_match, "dialogue_source": dialogue_source},
        # FIX（ep02_iter v0）：合成硬规则的判定（verdict fail = 不能当成片）
        "gates": {"verdict": "fail" if (gate_missing or gate_overflow) else "pass",
                  "missing_shots": gate_missing, "overflow": gate_overflow},
        "room_tone": room_info, "color_tags": "bt709", "pace": pace_report, "voiceover_dropped": dropped,
        "director": {"enabled": DIRP.enabled(), "cfg": "config/post/director.json", "plan": {str(k): {"transition_in": v["transition_in"], "j_cut_s": v["j_cut_s"], "l_cut_s": v["l_cut_s"], "cut_target_s": v["cut"]["target_s"], "camera": v["camera"]["move"]} for k, v in dplan.items()}},
    }
    if h3_report is not None:
        timeline["h3_dialogue"] = h3_report
    (out_dir / (f"{stem}.timeline.json" if variant else f"{name}.timeline.json")).write_text(json.dumps(timeline, ensure_ascii=False, indent=1),
                                                   encoding="utf-8")
    for w in warnings:
        log(f"WARN {w}")
    return timeline
