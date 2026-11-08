"""Shot Spec v2：每镜唯一事实来源（ARCH 2026-10-31）。

对齐：
- docs/ep02_iter/refactor_proposal.md §2（数据模型）、§7.1（官方六列映射）、§7.2（自检六项）、§7.6（P1 增补字段）
- 官方 shot-table-spec（data/ab/owner_workflows/minimax_h3_official_skills/3d-animation-short-generator/
  references/shot-table-spec.md）：Continuity Handoff、Reference Anchors（Fixed Landmarks / Character Positions /
  Exited Character Status / Lighting Baseline）、Per-Second Directives（五要素）、Audio & Dialogue Track（时间段、
  narrator-mouth-closed）。

本模块只定义结构和**格式 / 可实现性**校验，不评价内容，不生成内容。字段内容全部来自写作模型的剧本
（解析器 services/recipes/ns_recipes/spec_import.py），或资产注册表（身份参考图、场景底图）。
缺字段 → missing_fields() 列给写作方补（docs/ep02_iter/writer_rules_addendum.md 对应小节），Claude 不代写。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, Field

SCHEMA = "nsfs.shot_spec/2"

SIZES = ("极近特写", "大特写", "特写", "中近景", "近景", "中景", "中全景", "全景", "远景", "大远景")
CLOSE_SIZES = ("极近特写", "大特写", "特写", "中近景", "近景")
WIDE_SIZES = ("中全景", "全景", "远景", "大远景")
# 官方 base-en §4.3 运镜词表（小写，单数动词原形）
CAMERA_MOVES = ("zoom in", "zoom out", "push in", "pull out", "pan left", "pan right", "truck left", "truck right",
                "tilt up", "tilt down", "pedestal up", "pedestal down", "arc shot", "tracking shot", "static shot",
                "shake slightly", "shake strongly", "pov", "roll clockwise", "roll counterclockwise")
SCREEN_X = ("左", "中", "右")
DEPTHS = ("前", "中", "后")
FACINGS = ("镜头", "左", "右", "背对", "侧左", "侧右", "侧")
LINKS = ("接力", "同场换机位", "换场", "接上一集")


class Source(BaseModel):
    script_file: str | None = None
    script_sha: str | None = None
    line: int | None = None
    imported_at: str | None = None   # 不进 spec_hash
    edited_by: str | None = None
    rules_version: str | None = None  # 写作规则版本（config/script_chat.json default_rules + 附录 sysimpl-vN，COMP #15）；不进 spec_hash


class IdentityRef(BaseModel):
    """身份参考（资产注册表，不由写作方写）"""
    path: str
    sha: str | None = None           # 文件 sha256 前 16 位
    version: str | None = None       # 资产 rev（assets/characters/<id>/*.provenance.json）


class Camera(BaseModel):
    size: str | None = None          # SIZES 之一（img 行的景别词）
    angle: str | None = None
    move: str | None = None          # CAMERA_MOVES 之一
    amplitude: Literal["small", "medium", "large"] | None = None
    speed: Literal["slow", "normal", "fast"] | None = None
    lens: str | None = None


class Landmark(BaseModel):
    name: str
    screen_pos: str | None = None    # 例：右三分之一 / 画面下方居中


class Lighting(BaseModel):
    key: str | None = None
    fill: str | None = None
    rim: str | None = None
    modifier: str | None = None
    baseline_text: str | None = None  # 场景表“光线”原文（继承基线）


class Scene(BaseModel):
    id: str | None = None
    plate: IdentityRef | None = None
    time_of_day: str | None = None
    detail: str | None = None
    landmarks: list[Landmark] = Field(default_factory=list)
    lighting: Lighting | None = None

    @property
    def is_night(self) -> bool:
        t = (self.time_of_day or "") + " " + ((self.lighting.baseline_text if self.lighting else "") or "")
        return any(k in t for k in ("夜", "深夜", "凌晨", "night", "Night"))


class OnScreenCharacter(BaseModel):
    id: str                           # 角色 id（lin_wan …；未登记时 = 中文名）
    name: str                         # 中文名（剧本原文）
    presence: Literal["在画", "局部"] = "在画"
    screen_x: str | None = None       # 左 / 中 / 右
    depth: str | None = None          # 前 / 中 / 后
    part: str | None = None           # 局部时的部位
    facing: str | None = None         # FACINGS
    pose: str | None = None           # 起始姿势（原文）
    face_visible: bool | None = None
    look: str | None = None           # 造型 EN（本集造型行）
    focus: Literal["primary", "secondary"] = "secondary"
    en_phrase: str | None = None      # 人物表 EN外貌短语
    voice_phrase: str | None = None   # 人物表 VOICE声线短语
    identity_ref: IdentityRef | None = None


class Exited(BaseModel):
    name: str
    where: str | None = None
    why: str | None = None


class Beat(BaseModel):
    """约 2 秒一拍（refactor_proposal §7.1 改造后采用；官方 Per-Second Directives 五要素）"""
    t0: float
    t1: float
    action: str = ""
    camera: str | None = None
    position: str | None = None
    audio: str | None = None
    handoff: str | None = None
    gaze: str | None = None           # sysimpl-v2：视线落点
    expression: str | None = None     # sysimpl-v2：表情变化
    reaction: str | None = None       # sysimpl-v2：另一在画角色的可见反应（单人镜写 —）


class Motion(BaseModel):
    text: str = ""                    # 剧本 motion 原文
    beats: list[Beat] = Field(default_factory=list)
    end_state: str | None = None      # motion 最后一个“——”之后（= 尾帧样子）


class SpeechSeg(BaseModel):
    speaker: str                      # 中文名
    speaker_id: str | None = None
    text: str
    tone: str | None = None
    t0: float | None = None
    t1: float | None = None
    t_source: Literal["script", "estimate"] | None = None
    off_screen: bool = False


class Audio(BaseModel):
    dialogue: list[SpeechSeg] = Field(default_factory=list)
    voiceover: SpeechSeg | None = None
    soundscape_en: str | None = None
    soundscape_zh: str | None = None
    music_cue: str | None = None      # 【配乐】原文
    music_mode: Literal["episode_bgm", "h3"] | None = None
    mouth: dict[str, Literal["open", "closed"]] = Field(default_factory=dict)   # 角色名 → 嘴型
    sfx: list[dict] = Field(default_factory=list)


class Continuity(BaseModel):
    link: str | None = None           # LINKS
    from_prev: str | None = None      # 承：延续项
    to_next: str | None = None        # 下镜衔接
    hard_cut: bool = False


class FrameIntent(BaseModel):
    text: str = ""
    must: list[str] = Field(default_factory=list)
    exposure_intent: Literal["normal", "dark", "black"] = "normal"


class H3Text(BaseModel):
    mode: Literal["fl2va", "i2va", "ref2va", "t2va", "l2va"] | None = None   # 写作方原文的格式
    text: str | None = None


class Rating(BaseModel):
    level: int | None = None
    explicit: bool | None = None      # 导入时由现有编译器写入（剧本原值），不由本模块判定
    source: str = "script"


class ShotSpec(BaseModel):
    schema_: str = Field(SCHEMA, alias="schema")
    id: str
    episode: int
    index: int
    rev: int = 1
    source: Source = Field(default_factory=Source)
    duration_s: float
    duration_source: str = "script"
    scene: Scene = Field(default_factory=Scene)
    camera: Camera = Field(default_factory=Camera)
    characters_on_screen: list[OnScreenCharacter] = Field(default_factory=list)
    mentioned_only: list[str] = Field(default_factory=list)
    exited: list[Exited] = Field(default_factory=list)
    first_frame: FrameIntent = Field(default_factory=FrameIntent)
    end_frame: FrameIntent = Field(default_factory=FrameIntent)
    motion: Motion = Field(default_factory=Motion)
    audio: Audio = Field(default_factory=Audio)
    continuity: Continuity = Field(default_factory=Continuity)
    rating: Rating = Field(default_factory=Rating)
    h3: H3Text = Field(default_factory=H3Text)
    recipe_hint: str | None = None    # **镜型** 行（可选，写作方显式指定镜头类型）
    keyframes: dict[str, IdentityRef] = Field(default_factory=dict)   # first / last 定稿关键帧（资产侧）
    extras: dict[str, str] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}

    # ---- 派生 ----
    @property
    def visible(self) -> list[OnScreenCharacter]:
        return [c for c in self.characters_on_screen if c.presence == "在画"]

    @property
    def faces(self) -> list[OnScreenCharacter]:
        return [c for c in self.visible if c.face_visible is not False]

    @property
    def on_screen_speakers(self) -> list[str]:
        names = {c.name for c in self.visible}
        return sorted({d.speaker for d in self.audio.dialogue if d.speaker in names and not d.off_screen})

    @property
    def frames(self) -> int:
        return frames_for_seconds(self.duration_s)

    def canonical(self) -> dict:
        d = self.model_dump(mode="json", by_alias=True)
        d.get("source", {}).pop("imported_at", None)
        d.get("source", {}).pop("rules_version", None)
        d.pop("rev", None)
        return d

    def spec_hash(self) -> str:
        return sha_json(self.canonical())


def sha_json(obj) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def frames_for_seconds(seconds: float, fps: int = 24) -> int:
    """官方模板 ComfyMathExpression：max(5, round(a*24)) + (5 - (… % 17)) % 17 → 17k+5"""
    n = max(5, round(float(seconds) * fps))
    return n + (5 - n % 17) % 17


# ---------------------------------------------------------------------------------------------
# 格式 / 可实现性校验（只看字段齐不齐、能不能被系统准确实现；不评内容）
# ---------------------------------------------------------------------------------------------
class MissingField(BaseModel):
    shot: str
    field: str
    rule: str                         # 规则号（SS-*）
    severity: Literal["block", "warn", "asset", "info"]
    owner: Literal["writer", "asset", "system"] = "writer"
    message: str
    addendum: str | None = None       # writer_rules_addendum.md 小节


SPEECH_MARGIN_S = 0.3   # 对白 / 独白结束距镜尾的最小余量（refactor_proposal §3.6；LEARNED_SPECS C 独白溢出）


def _m(spec, field, rule, sev, msg, sec=None, owner="writer"):
    return MissingField(shot=spec.id, field=field, rule=rule, severity=sev, owner=owner, message=msg, addendum=sec)


def missing_fields(spec: ShotSpec) -> list[MissingField]:
    out: list[MissingField] = []
    d = spec.duration_s
    if spec.duration_source != "script" or not 4 <= d <= 15:
        out.append(_m(spec, "duration_s", "SS-D1", "block", f"时长须是剧本写明的 4–15 秒整数（现 {d}，来源 {spec.duration_source}）", "§2"))
    if not spec.scene.id:
        out.append(_m(spec, "scene.id", "SS-S1", "block", "缺 **场景** 行的场景 id", "§3"))
    if spec.scene.id and spec.scene.lighting is None:
        out.append(_m(spec, "scene.lighting", "SS-S2", "warn", "缺光线基线（**光线** 行：主光 / 补光 / 轮廓光 / 本镜修正）", "§3.2"))
    if spec.scene.id and not spec.scene.landmarks:
        out.append(_m(spec, "scene.landmarks", "SS-S3", "warn", "缺固定地标及屏幕位置（**地标** 行）", "§3.1"))
    if not spec.camera.size:
        out.append(_m(spec, "camera.size", "SS-C1", "block", "img 行没有可识别的景别词", "§4"))
    if not spec.camera.move:
        out.append(_m(spec, "camera.move", "SS-C2", "block", "motion 行没有官方运镜词（push in / static shot …）", "§4"))
    for c in spec.characters_on_screen:
        f = f"characters_on_screen[{c.name}]"
        if c.presence == "在画":
            if not c.screen_x:
                out.append(_m(spec, f + ".screen_x", "SS-P1", "block", f"{c.name} 缺屏幕位置（左/中/右）", "§5"))
            if not c.facing:
                out.append(_m(spec, f + ".facing", "SS-P2", "warn", f"{c.name} 缺朝向（**站位** 行）", "§5"))
            if not c.pose:
                out.append(_m(spec, f + ".pose", "SS-P3", "warn", f"{c.name} 缺起始姿势（**站位** 行）", "§5"))
            if not c.en_phrase:
                out.append(_m(spec, f + ".en_phrase", "SS-P4", "block", f"{c.name} 人物表缺 EN外貌短语", "§5"))
            if c.face_visible is not False and c.identity_ref is None:
                out.append(_m(spec, f + ".identity_ref", "SS-P5", "asset",
                              f"{c.name} 没有身份参考图（assets/characters/<id>/）", None, owner="asset"))
    speakers = {s.speaker for s in spec.audio.dialogue} | ({spec.audio.voiceover.speaker} if spec.audio.voiceover else set())
    by_name = {c.name: c for c in spec.characters_on_screen}
    for sp in sorted(speakers):
        c = by_name.get(sp)
        if c is not None and not c.voice_phrase:
            out.append(_m(spec, f"voice[{sp}]", "SS-A1", "block", f"说话人 {sp} 缺 VOICE声线短语", "§7"))
    # 对白 / 独白时间段与溢出
    for i, s in enumerate(spec.audio.dialogue, 1):
        if s.t0 is None or s.t1 is None:
            out.append(_m(spec, f"audio.dialogue[{i}].t", "SS-A2", "warn", f"第 {i} 句对白没有时间段（**台词时段** 行）", "§7.1"))
        elif s.t1 > d - SPEECH_MARGIN_S:
            sev = "block" if s.t_source == "script" else "warn"
            out.append(_m(spec, f"audio.dialogue[{i}].t1", "SS-A3", sev,
                          f"第 {i} 句对白结束于 {s.t1:.1f}s，超过镜长 {d}s − {SPEECH_MARGIN_S}s（对白溢出）"
                          + ("" if s.t_source == "script" else "（按字数估算）"), "§7.1"))
    vo = spec.audio.voiceover
    if vo is not None and vo.t1 is not None and vo.t1 > d - SPEECH_MARGIN_S:
        sev = "block" if vo.t_source == "script" else "warn"
        out.append(_m(spec, "audio.voiceover.t1", "SS-A4", sev,
                      f"独白约 {vo.t1:.1f}s，超过镜长 {d}s（独白溢出会盖住后面的镜头）"
                      + ("" if vo.t_source == "script" else "（按字数估算）"), "§7.2"))
    if not spec.audio.soundscape_en:
        out.append(_m(spec, "audio.soundscape_en", "SS-A5", "block", "缺 【H3 audio】 英文环境声（H3 不会处理静音）", "§7.3"))
    # 分拍覆盖（sysimpl-v2 起每镜必写，每拍 1.5–2 秒，视线 / 表情 / 双人反应必填）
    v2 = rules_at_least(spec, 2)
    if d >= 6 or v2:
        if not spec.motion.beats:
            out.append(_m(spec, "motion.beats", "SS-M1", "block" if v2 else "warn",
                          f"{d}s 镜头没有分拍（**分拍** 行，每 1.5–2 秒一段）", "§6 / sysimpl-v2"))
        else:
            out += [_m(spec, "motion.beats", "SS-M2", "block", g, "§6") for g in beat_gaps(spec.motion.beats, d)]
    out += beat_slots(spec, v2)
    if not spec.motion.end_state:
        out.append(_m(spec, "motion.end_state", "SS-M3", "warn", "motion 行缺“——”之后的结束状态", "§6"))
    if not spec.continuity.link:
        out.append(_m(spec, "continuity.link", "SS-L1", "block", "缺 **衔接** 行", "§8"))
    elif spec.continuity.link == "接力" and not spec.continuity.from_prev:
        out.append(_m(spec, "continuity.from_prev", "SS-L2", "block", "接力镜头缺“承：”延续项", "§8"))
    if not spec.h3.text:
        out.append(_m(spec, "h3.text", "SS-H1", "block", "缺 **H3提示词**（系统不代写，交写作方）", "§9"))
    if len([c for c in spec.visible]) > 3:
        out.append(_m(spec, "characters_on_screen", "SS-P6", "block", "在画重要角色超过 3 人（官方自检 3）", "§5"))
    return out


BEAT_MAX_S = 2.5   # sysimpl-v2：每拍 1.5–2 秒，最长 2.5 秒
ALIGN_TOL_S = 0.4  # TIMELINE-O：H3 分拍时间码与 **分拍** 起点允许的偏差（台词拍按 **台词时段** 起点写，常差 0.2–0.3 秒）


TIMING_LINT_CFG = __import__("pathlib").Path(__file__).resolve().parents[3] / "config" / "recipes" / "timing_lint.json"


def timing_lint_level(episode: int | None = None) -> str:
    """config/recipes/timing_lint.json：默认 warn；error 或本集在 episodes_error 里 → error"""
    try:
        c = json.loads(TIMING_LINT_CFG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "warn"
    if episode is not None and episode in (c.get("episodes_error") or []):
        return "error"
    return "error" if c.get("timing_lint_level") == "error" else "warn"


import contextlib as _contextlib  # noqa: E402
import contextvars as _contextvars  # noqa: E402

# TIMELINE-O 11-03（项目方：“MiniMax 的提示词，运动……是需要时间轴的”）：V2 剧及所有新剧，稿件盖了 sysimpl-v2 及以上的章 →
# 时间轴一律 strict（不再看 timing_lint.json 的 warn 开关）。只有 legacy 剧（timing_lint.json legacy_runs，= series10_v1）
# 和认不出剧的稿件（测试临时文件）仍按开关走。剧的来源：spec.source.script_file 路径（data/{pipeline,import}/runs/<run>/），
# 认不出时用 timeline_run(<run>) 设的当前剧（p4_adapter.check_p4 的临时文件，按 autoflow run_scope）。
_TL_RUN: "_contextvars.ContextVar[str | None]" = _contextvars.ContextVar("shot_spec_timeline_run", default=None)
_RUN_PATH_RE = re.compile(r"data[\\/](?:pipeline|import)[\\/]runs[\\/]([^\\/]+)[\\/]")


@_contextlib.contextmanager
def timeline_run(run: str | None):
    tok = _TL_RUN.set(run)
    try:
        yield
    finally:
        _TL_RUN.reset(tok)


def spec_run(spec: ShotSpec) -> str | None:
    m = _RUN_PATH_RE.search(spec.source.script_file or "")
    return m.group(1) if m else _TL_RUN.get()


def run_strict(run: str | None) -> bool:
    """这部剧的时间轴是否一律 strict：认得出剧、且不在 timing_lint.json legacy_runs 里 → True"""
    if not run:
        return False
    try:
        legacy = json.loads(TIMING_LINT_CFG.read_text(encoding="utf-8")).get("legacy_runs")
    except (OSError, ValueError):
        legacy = None   # 配置读不了 = 认不准是不是 legacy 剧 → 不强判 strict，回到 timing_lint_level 开关（同“认不出剧”口径）
    return isinstance(legacy, list) and run not in legacy


def rules_at_least(spec: ShotSpec, n: int) -> bool:
    """写作规则 ≥ sysimpl-vN，且（本剧一律 strict：V2 / 新剧；或开关为 error）时按 v2 严格要求"""
    m = re.search(r"sysimpl-v(\d+)", spec.source.rules_version or "")
    if run_strict(spec_run(spec)):
        # V2 / 新剧只按 ≥ sysimpl-v2 写过：没盖章（补丁环只拿镜头区校验，章在文件头）也按 strict；只有明确盖了更低版本的章才放过
        return not (m and int(m.group(1)) < n)
    if not (m and int(m.group(1)) >= n):
        return False
    return timing_lint_level(spec.episode) == "error"


def beat_slots(spec: ShotSpec, strict: bool) -> list[MissingField]:
    """SS-M4 拍长、SS-M5 视线 / 表情、SS-M6 双人反应、SS-M7 下镜（结尾留给下一镜的动作）。strict = 写作规则 ≥ sysimpl-v2"""
    out: list[MissingField] = []
    sev = "block" if strict else "warn"
    two = len(spec.faces) >= 2
    for b in spec.motion.beats:
        tag = f"分拍 {b.t0:g}–{b.t1:g}秒"
        if b.t1 - b.t0 > BEAT_MAX_S + 1e-6:
            out.append(_m(spec, "motion.beats", "SS-M4", "warn", f"{tag} 长 {b.t1 - b.t0:.1f}s，超过 {BEAT_MAX_S}s", "sysimpl-v2"))
        if strict and not (b.camera or "").strip():   # TIMELINE-O：运镜也按拍写（运镜=固定 / 小幅推近 / 继续推近…）
            out.append(_m(spec, "motion.beats", "SS-M8", sev, f"{tag} 缺 运镜=", "sysimpl-v2"))
        if not strict and not (b.gaze or b.expression or b.reaction):
            continue   # v1 分拍没有这些槽位：只在整拍都缺时不重复报
        if not spec.faces:
            continue   # 手部 / 局部 / 空镜：没有露脸角色，不要求视线和表情
        if not (b.gaze or "").strip():
            out.append(_m(spec, "motion.beats", "SS-M5", sev, f"{tag} 缺 视线=", "sysimpl-v2"))
        if not (b.expression or "").strip():
            out.append(_m(spec, "motion.beats", "SS-M5", sev, f"{tag} 缺 表情=", "sysimpl-v2"))
        if two and (b.reaction or "").strip() in ("", "—", "-", "无"):
            out.append(_m(spec, "motion.beats", "SS-M6", sev, f"{tag} 双人镜缺 反应=（另一人的可见反应）", "sysimpl-v2"))
    if strict and not spec.continuity.to_next:
        # K-WRT-I01（总调度 2026-10-31 确认）：接力 / 同场换机位镜头缺 **下镜** = block；其它衔接仍是 warn
        sev7 = "block" if spec.continuity.link in ("接力", "同场换机位") else "warn"
        out.append(_m(spec, "continuity.to_next", "SS-M7", sev7, "缺 **下镜** 行（结尾仍在进行、给下一镜接的动作）", "sysimpl-v2"))
    return out


def beat_gaps(beats: list[Beat], duration: float, tol: float = 0.25) -> list[str]:
    """分拍必须从 0 覆盖到镜长、无空隙（官方自检 5）"""
    errs = []
    bs = sorted(beats, key=lambda b: b.t0)
    t = 0.0
    for b in bs:
        if b.t1 <= b.t0:
            errs.append(f"分拍 {b.t0}–{b.t1}s 起止颠倒")
        if b.t0 > t + tol:
            errs.append(f"分拍在 {t:.1f}–{b.t0:.1f}s 有空隙")
        t = max(t, b.t1)
    if t < duration - tol:
        errs.append(f"分拍只写到 {t:.1f}s，镜长 {duration}s")
    return errs


def check_sequence(specs: list[ShotSpec]) -> list[MissingField]:
    """跨镜自检（官方 5.5 第 4、6 项）：同场连续镜头的地标 / 光线基线要继承或写连续性说明；衔接链不能断。"""
    out: list[MissingField] = []
    for prev, cur in zip(specs, specs[1:]):
        same_scene = prev.scene.id and prev.scene.id == cur.scene.id
        if same_scene and not cur.continuity.hard_cut:
            pl = {(l.name, l.screen_pos) for l in prev.scene.landmarks}
            cl = {(l.name, l.screen_pos) for l in cur.scene.landmarks}
            if pl and cl and pl != cl and cur.continuity.link not in ("同场换机位",):
                out.append(_m(cur, "scene.landmarks", "SS-X1", "warn",
                              f"与 {prev.id} 同场，地标位置变了但衔接不是“同场换机位”也没标 HARD CUT", "§3.1"))
            pk = prev.scene.lighting.model_dump() if prev.scene.lighting else None
            ck = cur.scene.lighting.model_dump() if cur.scene.lighting else None
            if pk and ck and {k: pk[k] for k in ("key", "fill", "rim")} != {k: ck[k] for k in ("key", "fill", "rim")} \
                    and not ck.get("modifier"):
                out.append(_m(cur, "scene.lighting", "SS-X2", "warn",
                              f"与 {prev.id} 同场，光线基线变了但没写本镜修正", "§3.2"))
        if cur.continuity.link == "接力" and not same_scene:
            out.append(_m(cur, "continuity.link", "SS-X3", "warn", f"标“接力”但场景与 {prev.id} 不同", "§8"))
        # 左右互换（ep02 S6→S7）
        if same_scene and cur.continuity.link == "接力" and not cur.continuity.hard_cut:
            px = {c.name: c.screen_x for c in prev.visible if c.screen_x}
            for c in cur.visible:
                if c.screen_x and px.get(c.name) and px[c.name] != c.screen_x and px[c.name] != "中" and c.screen_x != "中":
                    out.append(_m(cur, f"characters_on_screen[{c.name}].screen_x", "SS-X4", "warn",
                                  f"{c.name} 从 {prev.id} 的{px[c.name]}侧换到{c.screen_x}侧（接力镜头），要么改站位要么标 HARD CUT", "§8"))
    return out
