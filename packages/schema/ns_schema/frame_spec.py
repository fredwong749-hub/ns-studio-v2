"""
Frame Spec：剧本镜头 → 首帧/尾帧的“画面说明层”（docs/IMAGE_PROMPT_METHOD.md + docs/frame_spec_compiler.md 合并格式）。

一份 spec 对应一个镜头，写到 data/renders/epXX/SN/visual_spec.json（gpu3 写 data/renders_gpu3/... 同格式）。
剧本原文只放在 script_ref 里，永不修改；其余字段是“图片模型能执行的画面说明”，facts 是质检逐条核对的清单。
纯 pydantic，无项目路径、无 ComfyUI 依赖，可被其他渲染器直接导入。
"""
from typing import Literal

from pydantic import BaseModel, Field

SPEC_VERSION = "frame_spec/1"

Facing = Literal["front", "three_quarter", "profile", "back", "unknown"]
Clarity = Literal["sharp", "soft", "silhouette", "bokeh"]
RefRole = Literal["identity_face", "look", "item", "scene", "prop"]
TechniqueKind = Literal["none", "double_exposure", "split_makeup_mirror", "silhouette", "reflection", "timelapse",
                        "pov", "montage", "freeze", "screen_text", "slow_motion"]
FactKind = Literal["person", "noun", "number", "colour", "place", "time", "light", "framing", "action", "text",
                   "boost", "rule", "other"]


class ScriptRef(BaseModel):
    img: str                                  # 剧本 img 原文（含触发词，一字不改）
    motion: str = ""                          # 剧本 motion 原文
    sha: str = ""                             # sha256(img + "\n" + motion)，原文变了 spec 即过期


class FrameState(BaseModel):
    """一个瞬间：首帧 = motion 起点，尾帧 = motion 终点（同场景同人物同服装）"""
    description: str = ""                     # 该瞬间的画面描述（原文短语拼成）
    pose: str = ""
    action: str = ""
    expression: str = ""
    gaze: str = ""
    hands: str = ""


class Adornment(BaseModel):
    item: str
    count: int | None = None                  # 数量一律写成整数（来自原文或装饰码）
    placement: str = ""


class Expression(BaseModel):
    start: str = ""
    end: str = ""


class Reference(BaseModel):
    role: RefRole
    source: str                               # 参考图路径（相对项目根目录）
    note: str = ""


GazeTarget = Literal["camera", "other", "down", "up", "away", "object", "closed", "unknown"]
Source = Literal["text", "timeline", "guess", "same", "none"]
Framing = Literal["extreme_close", "close", "medium", "full", ""]
# 焦点部位（T08）：eye | lips | hand | nails | shoes | foot | face | legs | back | torso | screen | object | other | ""
FocusPart = Literal["eye", "lips", "hand", "nails", "shoes", "foot", "face", "legs", "back", "torso", "screen", "object",
                    "other", ""]


class Gaze(BaseModel):
    """某角色某瞬间看哪里（T08，gpu3 请求）。target：camera 看镜头 | other 看另一角色（at = 角色 id）| down 低头 |
    up 抬眼 | away 看别处/背对 | object 看物件（at = 原文物件词）| closed 闭眼 | unknown"""
    target: GazeTarget = "unknown"
    at: str = ""                              # other → 角色 id（lin_wan/gu_chen/chen_yu/extra）；object → 原文物件词
    cue: str = ""                             # 命中的原文线索（“看向镜头”“低头”…；guess 时为空）
    source: Source = "guess"                  # text = 原文写了 | same = 尾帧沿用首帧 | guess = 规则默认


class PropState(BaseModel):
    """道具状态（直接取 data/state_timeline.json 本镜 props，不重新推导；见 docs/state_timeline.md §七.1）"""
    id: str
    name: str = ""
    state: str | None = None                  # worn | hanging | held | on_table
    state_source: str | None = None           # text | inherited | guess
    state_end: str | None = None
    state_end_source: str | None = None
    holder: str | None = None
    mention: str = ""
    trigger: str | None = None
    trigger_conflict: bool = False            # 触发词会画成“穿着”但 state ≠ worn → 按 state 处理


class CharacterSpec(BaseModel):
    id: str                                   # lin_wan / gu_chen / chen_yu / extra
    name: str = ""                            # 林晚 / 顾沉 / 陈宇 / 原文代称（“成年男性路人”）
    mentions: list[str] = Field(default_factory=list)   # 原文里指代此人的词（她/蓝发女性/男经理…）
    look_id: str | None = None
    plate: str | None = None                  # 身份底板路径（相对项目根目录）
    items: list[str] = Field(default_factory=list)       # 造型单品 id（data/looks）
    facing: Facing = "unknown"
    clarity: Clarity = "sharp"
    screen_share: str = ""                    # “画面下2/3” 或 "0.6"
    visible_body: list[str] = Field(default_factory=list)  # 可见部位：face/hands/feet/legs/full_body/back…
    adornment: list[Adornment] = Field(default_factory=list)
    heels: str = ""                           # 仅脚入镜时
    nails: str = ""                           # 仅手/脚入镜时
    makeup: str = ""                          # 妆容底板编号 M0..M6
    expression: Expression = Field(default_factory=Expression)   # 只放表情词（T08：服装/动作/运镜/视线词由 sanitize 去掉）
    references: list[Reference] = Field(default_factory=list)
    # ---- T08（gpu3 请求，全部只增）
    screen_share_value: float | None = None   # 该角色占画面比例 0–1（主角 = 景别表；次要人物 ×0.6；焦外 0.1）
    screen_share_source: Source = "guess"     # text = 原文写了占比/位置 | guess = 按景别估
    screen_position: str = ""                 # 原文位置词：left | right | center | foreground | background | top | bottom
    face_visible: bool | None = None          # 来自时间线 faces_visible（没有时间线时按同一规则现算）
    gaze_start: Gaze = Field(default_factory=Gaze)
    gaze_end: Gaze = Field(default_factory=Gaze)
    changes_within_shot: list[dict] = Field(default_factory=list)   # 时间线原样（镜内换装：脱下/解开/穿上…）
    items_end: dict[str, list[str]] = Field(default_factory=dict)   # 时间线原样：尾帧穿戴（槽位 → 单品 id）
    visibility_end: str | None = None         # 时间线原样


class SceneSpec(BaseModel):
    id: str = ""                              # 场景锁 id（SK-ELEVATOR…），同场景镜头共用
    plate: str | None = None                  # 场景底板（无人物）路径
    place: str = ""
    time_of_day: str = ""                     # 清晨/白天/黄昏/夜晚/深夜
    props: list[str] = Field(default_factory=list)


class Lighting(BaseModel):
    sources: list[str] = Field(default_factory=list)    # 剧本写的光源（优先于全局暖调）
    grade: str = ""                           # 色调：全局浓郁暖调，暗场/夜景按剧本光源
    dark: bool = False                        # 暗场：调色不提亮


class Technique(BaseModel):
    kind: TechniqueKind = "none"
    recipe: str = ""                          # 专门配方（合成/局部重绘/两张叠化…）


class Fact(BaseModel):
    text: str                                 # 原文短语（名词/数字/颜色一字不改）
    kind: FactKind = "other"                  # boost = 项目方导演加强；rule = 项目方全局材质规则（B7/B8）
    must: bool = True                         # 必须在首帧中可见（false = 只在尾帧或画外）
    frame: Literal["first", "last", "both"] = "both"


class Line(BaseModel):
    speaker: str = ""
    text: str


class ModelPrompt(BaseModel):
    qwen: str = ""                            # 首帧（中文长句，Qwen Image 2.1）
    qwen_last: str = ""                       # 尾帧编辑提示词（图1 = 首帧）
    klein: str = ""                           # 英文短句备选（未实现时为空）


class FrameSpec(BaseModel):
    version: str = SPEC_VERSION
    shot_id: str
    episode: int = 0
    script_ref: ScriptRef
    adult: bool = False
    shot_size: str = ""                       # 极近特写/特写/近景/中景/全景/远景
    angle: str = ""                           # 平视/仰拍/俯拍/过肩/桌下视角…
    lens: str = ""                            # 85mm f/1.2（全局）
    camera_move: str = ""                     # motion 行的运镜（push in / static …）
    focus_part: str = ""                      # 焦点部位（取值见 FocusPart；"" = 无单一焦点，中景/全景）
    focus_label: str = ""                     # 中文：眼睛/嘴唇/手/美甲/高跟鞋/脚/脸/腿/背/身体/屏幕/<物件词>
    focus_object: str = ""                    # 焦点是物件/鞋/屏幕时的原文词（“高跟鞋”“手机”…）
    focus_part_source: Source = "none"        # text = 原文“特写X/X特写/镜头推向X” | guess = 首个描述分句 | none
    framing: Framing = ""                     # 时间线 framing（extreme_close | close | medium | full），没有时间线时同规则现算
    framing_source: Source = "none"           # 时间线的 framing_source（text | guess）
    faces_visible: list[str] = Field(default_factory=list)       # 时间线 faces_visible（角色 id）
    faces_visible_source: Source = "none"
    prop_states: list[PropState] = Field(default_factory=list)   # 时间线 props（含 state / state_end）
    timeline_inputs_sha: str | None = None    # 用到的时间线条目的 inputs_sha（None = 没有读时间线，字段为现算）
    risks: list[str] = Field(default_factory=list)                # H3 风险（SOP §8：远景小脸 …）
    expression_stripped: list[str] = Field(default_factory=list)  # 表情 sanitize 去掉的词（“词｜原因”），审计用
    first_frame: FrameState = Field(default_factory=FrameState)
    last_frame: FrameState = Field(default_factory=FrameState)
    characters: list[CharacterSpec] = Field(default_factory=list)
    scene: SceneSpec = Field(default_factory=SceneSpec)
    lighting: Lighting = Field(default_factory=Lighting)
    composition: str = ""                     # 9:16 构图、主体位置、前后景、人数
    technique: Technique = Field(default_factory=Technique)
    on_screen_text: list[str] = Field(default_factory=list)   # 唯一允许出现的文字
    decoration_code: str | None = None        # 本集装饰码 D0–D10
    look_id: str | None = None                # 本集主角造型
    dialogue: list[Line] = Field(default_factory=list)        # 只给视频侧
    voiceover: str = ""                       # 只给视频侧
    notes: list[str] = Field(default_factory=list)            # 导演意图等（不进提示词）
    not_in_image: list[str] = Field(default_factory=list)     # 比喻/声音/内心（不进提示词）
    facts: list[Fact] = Field(default_factory=list)
    boost_first: str = ""                     # 项目方“导演加强”（整集 + 镜头，原样），也作为 must 事实核对
    boost_last: str = ""
    model_prompt: ModelPrompt = Field(default_factory=ModelPrompt)
    generator: str = "rules"                  # rules | llm:<model>（LLM 补全过的字段）
    validation: list[str] = Field(default_factory=list)       # 校验问题；为空 = 通过
