"""剧本导入结果的数据结构。导入器只做确定性解析，原文原样保留，代称消解等交给 Agent。"""
from typing import Literal

from pydantic import BaseModel, Field


class CharacterLock(BaseModel):
    name: str
    description: str


class LoraTrigger(BaseModel):
    trigger: str
    kind: Literal["image", "video"]
    description: str
    file: str | None = None
    strength: tuple[float, float] | None = None
    starts: str | None = None


class ReferenceImage(BaseModel):
    id: str
    description: str
    usage: str | None = None


class DecorRange(BaseModel):
    first_ep: int
    last_ep: int
    state: str
    note: str = ""
    overrides: dict[int, str] = Field(default_factory=dict)


class SceneConflict(BaseModel):
    """同一场景 id 在不同场景表里写了不同的字段值（只记结构，不评内容）"""
    field: str                      # 名称 | 空间描述 | 时段 | 光线 | 固定陈设
    first: str                      # 第一次定义的值
    other: str                      # 冲突的那次定义的值
    episode: int | None = None      # 冲突定义所在集（None = 全剧场景表）
    source_line: int = 0


class SceneEntry(BaseModel):
    """场景表的一行（S1，docs/qwen_writing_contract.md §1.2）：全剧场景表 / 本集场景表，原文逐字"""
    scene_id: str
    name: str = ""                  # 名称
    space: str = ""                 # 空间描述
    time_of_day: str = ""           # 时段
    light: str = ""                 # 光线
    fixtures: str = ""              # 固定陈设
    defined_in: list[int | None] = Field(default_factory=list)   # 定义出现处：None = 全剧场景表，N = 第 N 集本集场景表
    conflicts: list[SceneConflict] = Field(default_factory=list)
    source_line: int = 0


class CharacterConflict(BaseModel):
    """同一角色在不同人物表里写了不同的格（QW-P5，同 SceneConflict 的机制）"""
    field: str                      # 角色id | 中文外貌句 | EN外貌短语 | VOICE声线短语
    first: str
    other: str
    episode: int | None = None      # 冲突定义所在集（None = 全剧人物表）
    source_line: int = 0


class CharacterEntry(BaseModel):
    """人物表的一行（production_rules_v3 §1.1，fredw-92/R3）：全剧人物表 / 本集人物表，原文逐字。
    角色的固定外貌（中文）、H3 英文身份短语、H3 英文声线短语的唯一存放处"""
    name: str                       # 角色（中文名）
    char_id: str = ""               # 角色id：ns_schema.looks.CHARACTERS 的 slug 或 ch_<…>
    zh: str = ""                    # 中文外貌句（六项，全角｜分隔）或“系统锁”
    en: str = ""                    # EN外貌短语（只写身份，不写服装）
    voice: str = ""                 # VOICE声线短语
    defined_in: list[int | None] = Field(default_factory=list)   # None = 全剧人物表，N = 第 N 集本集人物表
    conflicts: list[CharacterConflict] = Field(default_factory=list)
    source_line: int = 0


class ScriptGlobals(BaseModel):
    character_locks: list[CharacterLock] = Field(default_factory=list)
    motion_suffix: str = ""
    image_suffix: str = ""  # 剧本里的“每条img统一后缀”（全局画面风格）
    base_negative: str = ""
    lora_triggers: list[LoraTrigger] = Field(default_factory=list)
    references: list[ReferenceImage] = Field(default_factory=list)
    decor_schedule: list[DecorRange] = Field(default_factory=list)
    # gpu3 渲染器读导入 JSON（总调度 2026-10-30）：首帧提示词要用的两段原文，与 gpu3 md 解析器逐字一致
    daily_outfit: str = ""          # **林晚日常穿搭（…）**：整行原文（项目方 2026-10-30）
    decor_tables: dict[str, str] = Field(default_factory=dict)   # {"11": "集：…；乳环：…"} 每批“装饰连续性自查表”逐集行
    # S1（fredw-92/S-back）：全剧场景表 + 各集本集场景表合并（按场景 id；第一次定义为准，冲突记在 conflicts）。
    # 旧剧本没有场景表 = 空，场景分组退回 state_timeline.group_scenes 的推断
    scene_library: list[SceneEntry] = Field(default_factory=list)
    # R3（production_rules_v3 §1.1）：全剧人物表 + 各集本集人物表合并（按角色名；第一次定义为准，冲突记 conflicts）。
    # 旧剧本没有人物表 = 空
    character_registry: list[CharacterEntry] = Field(default_factory=list)


class DialogueLine(BaseModel):
    speaker_note: str | None  # 紧挨台词的最后一个括号，如 "顾沉低声"、"她，很小声"
    text: str
    stage: str | None = None  # 更前面的括号（舞台提示），如 "接通"
    speaker: str | None = None  # R3：speaker_note 的第一个词（v3 要求以角色名开头，QW-D1）；旧剧本可能是“她”等


class Voiceover(BaseModel):
    note: str | None  # 如 "镜1尾入"、"全集收尾独白"
    text: str
    speaker: str | None = None  # R3：**🎙（<角色名>，<说明>）** 括号的第一个词（QW-D2）


class Extra(BaseModel):
    label: str
    text: str


class CastEntry(BaseModel):
    """**人物** 行的一项（R3，QW-CH1）：<名>（在画｜<左/中/右>｜<前/中/后>）/（局部｜<部位>｜<左/中/右>）/（画外）"""
    name: str
    presence: str = ""              # 在画 | 局部 | 画外（原文；格式不对时校验器报 QW-CH1）
    x: str | None = None            # 左 / 中 / 右
    depth: str | None = None        # 前 / 中 / 后（在画）
    part: str | None = None         # 部位（局部）
    raw: str = ""                   # 这一项原文

    @property
    def visible(self) -> bool:
        return self.presence in ("在画", "局部")


class LookLine(BaseModel):
    """集头 **造型｜<角色名>**：/ **造型｜<角色名>｜镜<N>起**：（R3，QW-E6/E7）。槽位按原文中文名保存"""
    character: str
    from_shot: int | None = None    # None = 本集起始造型；N = 镜N 起换装（只写变了的槽位）
    slots: dict[str, str] = Field(default_factory=dict)   # {"服装": "…", "鞋": "…", …}（不含 EN）
    en: str = ""                    # EN= 英文服装短语（进 H3 锚点句，H3-X1）
    raw: str = ""
    source_line: int = 0


class Shot(BaseModel):
    episode: int
    index: int
    img_note: str | None = None
    img_prompt: str = ""
    img_triggers: list[str] = Field(default_factory=list)
    motion_prompt: str = ""
    motion_triggers: list[str] = Field(default_factory=list)
    sound: str = ""                 # **sound** 整行原文
    sound_intimate: str = ""        # v3：sound 行里的【亲密声】…段
    sound_h3: str = ""              # v3：sound 行里的【H3 audio】…段（给 H3 的英文声音描述）
    music_cue: str = ""             # v3：sound 行里的【配乐】…段（<动作>，强度<0–3>，<避让>；动作 ∈ 进入/延续/渐强/推到高点/撤到静默/落下/重音，QW-M1）
    h3_prompt: str | None = None    # v3：**H3提示词** 原文（SOP §9A.1 四块官方 FL2VA 全文，多行，逐字保留；S1-W7–W9）
    h3_check: str | None = None     # v3：**H3校验** 原文，如“暂缓（露骨内容）”= 露骨红标，项目方自行处理
    voiceover: Voiceover | None = None
    dialogue_raw: str | None = None
    dialogue: list[DialogueLine] = Field(default_factory=list)
    dialogue_stage: list[str] = Field(default_factory=list)  # **对白** 里没有引号的段（舞台提示/缺引号），原文；不是台词（T04）
    extras: list[Extra] = Field(default_factory=list)
    decor_state: str | None = None
    scene_id: str | None = None     # S1：**场景**：<场景id>｜<本镜场景细节> 的场景 id（旧剧本没有 = None，退回推断分组）
    scene_detail: str | None = None  # S1：同一行 ｜ 之后的本镜场景细节（原文）
    # R3（production_rules_v3 §1.3）：**人物** 行 / **衔接** 行。None = 没写（旧剧本），校验器在 v3 模式下报 QW-CH1 / QW-LK1
    cast: list[CastEntry] | None = None
    cast_raw: str | None = None
    link: str | None = None          # 接力 | 同场换机位 | 换场 | 接上一集（原文）
    link_carry: str | None = None    # “承：”之后的延续项（原文）
    # 秒；剧本 **时长**：N秒（K4：4–15 整数，必须写）。没写时仍记 10 + duration_source="default"（旧剧本兼容；
    # 校验器 S1-W2 对 default 报 block）。production_rules_v3 §5 1.1 要求改成 None——下游 `or 10` 兜底去掉之后再改
    duration: int = 10
    duration_source: str = "default"   # script | default
    adult: bool | None = None       # 导入时由编译器分类写入（两边共用同一判定：关键词 + config/adult_overrides.json）
    adult_reasons: list[str] = Field(default_factory=list)
    source_line: int

    @property
    def id(self) -> str:
        return f"ep{self.episode:02d}-S{self.index}"


class Episode(BaseModel):
    number: int
    title: str
    high_intensity: bool = False  # 标题带 🔴
    tag: str = ""
    synopsis: str = ""
    music: str = ""                 # v3：**配乐** 整行原文（本集背景音乐，后期用）
    music_bgm: str = ""             # v3：配乐行里的【BGM】…（给音乐模型的英文提示）
    negative_extra: str = ""        # 旧标签 **负面追加**（K3 作废；旧剧本仍认）
    # R3（production_rules_v3 §1.2）：集头 **出场人物** / **用到的场景** / **造型｜…**。None = 没写（旧剧本）
    cast: list[str] | None = None
    scene_ids: list[str] | None = None
    looks: list[LookLine] = Field(default_factory=list)
    shots: list[Shot] = Field(default_factory=list)
    extras: list[Extra] = Field(default_factory=list)
    source_line: int


class SourceFile(BaseModel):
    path: str
    sha256: str
    # 合并导入（单集 / 部分导入）时记录：这个源文件提供了哪些集、是第几版剧本（S1 闸门按集判定版本 ≥ v3）
    version: str | None = None
    episodes: list[int] = Field(default_factory=list)


class ScriptImport(BaseModel):
    sources: list[SourceFile]
    globals: ScriptGlobals
    episodes: list[Episode]
    warnings: list[str] = Field(default_factory=list)
