"""
每集角色造型（衣橱）：单品 WardrobeItem → 造型 Look → 三层排期 LookSchedule。

数据以 JSON 存在 data/looks/（人工可编辑）：
    wardrobe.json   Wardrobe：单品 + 装饰状态 D0–D10/DN
    looks.json      LookBook：命名造型（某角色某一身：每个槽位穿哪些单品，可选 D 状态）
    schedule.json   LookSchedule：三层解析
                    1. character_defaults  角色 → 默认造型 look_id
                    2. episodes            集 → 角色 → LookPatch（集造型计划，种子来自 decor_schedule）
                    3. shot_overrides      镜 → 角色 → LookPatch（本镜覆盖）

解析（优先级由低到高）：default → episode → shot。每层 LookPatch 里：
    look_id   换成另一套命名造型（该造型的全部槽位、以及它带的 D 状态都以本层为来源）
    slots     逐槽位替换（[] 表示该槽位明确为空，比如赤脚）
    d_state / visibility / present   单独覆盖
没写的字段沿用下层。解析结果每个值都带 source（default / episode / shot）。

约定：
- 角色键一律用 slug（lin_wan / gu_chen / …），中文名见 CHARACTERS；API 也接受中文名。
- candidate_items / evidence / notes / needs_review 是审核元数据，不参与解析、不进提示词。
"""
from typing import Literal

from pydantic import BaseModel, Field

Slot = Literal["outfit", "shoes", "makeup", "hair", "nails", "accessory", "other"]
SLOTS: tuple[str, ...] = ("outfit", "shoes", "makeup", "hair", "nails", "accessory", "other")
Layer = Literal["default", "episode", "scene", "shot"]   # scene = 画布场景覆盖（W5，canvas-ui-spec-v3 §6）
# 装饰可见性：visible=按镜头触发词决定；hidden=装饰在身但被遮住；in_box=可摘装饰收在匣里
Visibility = Literal["visible", "hidden", "in_box"]
# 何时把单品写进提示词：always=只要生效就写；when_in_shot=该镜 img 触发词/关键词命中时才写（身体装饰）
PromptPolicy = Literal["always", "when_in_shot"]

def _load_cast() -> dict[str, str]:
    """演员表 = 当前正式剧本的人物登记表（data/import/script_v2.json globals.character_registry，
    由写作链 P1_storyline 导入）。读不到时用 series10_v1 的 4 人兜底。
    RECALL 2026-11-01：旧 60 集的 Alex / Ryan / Kai / Leo 已撤下（项目方要求删掉外国人等旧内容）。"""
    import json
    import os
    from pathlib import Path
    fallback = {"lin_wan": "林晚", "gu_chen": "顾沉", "chen_yu": "陈宇", "ch_a_lan": "阿岚"}
    root = Path(os.environ.get("NS_ROOT") or Path(__file__).resolve().parents[3])
    try:
        g = json.loads((root / "data" / "import" / "script_v2.json").read_text(encoding="utf-8")).get("globals") or {}
        cast = {str(c["char_id"]): str(c["name"]) for c in g.get("character_registry") or []
                if isinstance(c, dict) and c.get("char_id") and c.get("name")}
    except (OSError, ValueError, TypeError):
        cast = {}
    return cast or fallback


CHARACTERS: dict[str, str] = _load_cast()
CHARACTER_SLUG: dict[str, str] = {v: k for k, v in CHARACTERS.items()}


class LoraRef(BaseModel):
    file: str
    strength: float


class ItemImage(BaseModel):
    """单品图片（canvas-ui-spec-v2 §3.5.5）。file 相对项目根目录，必须在 assets/ 下。"""
    file: str
    variant: str = "clean"                   # clean（特写）| worn（上身）| front | 3q | side | back
    version: int = 1
    sha256: str = ""
    source: str = "upload"                   # upload | manifest | generated
    created_at: str | None = None


class WardrobeItem(BaseModel):
    id: str
    slot: Slot
    name: str
    description: str = ""                    # 进提示词的文字
    character: str | None = None             # 专属角色 slug；None = 通用
    trigger: str | None = None               # LoRA 触发词，如 hh12clear
    lora_file: str | None = None
    lora_strength: tuple[float, float] | None = None
    keywords: list[str] = Field(default_factory=list)   # 剧本里指代该单品的原文短语（用于检测/冲突）
    reference_images: list[str] = Field(default_factory=list)  # 相对项目根目录的路径，或 REF-xx id
    prompt_policy: PromptPolicy = "always"
    removable: bool = True                   # 可摘（珠链）还是永久（穿孔环、纹身）；in_box 只影响可摘的
    source: str = "manual"                   # lora_trigger / character_lock / reference / keyword / manual
    needs_review: bool = False
    notes: list[str] = Field(default_factory=list)
    images: list[ItemImage] = Field(default_factory=list)     # 真实图片文件（替代 REF-xx 注册表 id）
    tags: list[str] = Field(default_factory=list)            # 如 12cm、透明、漆皮
    version: int = 1                                         # 每次上传新图 +1


class AdornmentState(BaseModel):
    """装饰锁 D0–D10/DN。剧本没有逐条定义，种子器只给出可推出的单品和证据。"""
    id: str
    description: str = ""
    items: list[str] = Field(default_factory=list)       # 该状态下身上有的装饰单品 id
    episodes: list[int] = Field(default_factory=list)
    needs_review: bool = True
    notes: list[str] = Field(default_factory=list)


class Wardrobe(BaseModel):
    version: int = 2
    items: list[WardrobeItem] = Field(default_factory=list)
    decor_states: list[AdornmentState] = Field(default_factory=list)


class Look(BaseModel):
    """命名造型，如“林晚 · 通勤”。"""
    id: str
    character: str                           # slug
    name: str = ""
    items: dict[str, list[str]] = Field(default_factory=dict)   # slot → 单品 id
    decor_state: str | None = None
    reference_images: list[str] = Field(default_factory=list)
    needs_review: bool = False
    notes: list[str] = Field(default_factory=list)
    label: str | None = None                 # 造型字母 A / B / C（画布显示用）
    preview_image: str | None = None         # looks/ 下的预览图，相对项目根目录
    version: int = 1


class LookBook(BaseModel):
    version: int = 2
    looks: list[Look] = Field(default_factory=list)


class LookPatch(BaseModel):
    """一层覆盖（集计划或本镜覆盖）。None / 缺省 = 沿用下层。"""
    look_id: str | None = None
    slots: dict[str, list[str]] | None = None
    d_state: str | None = None
    visibility: Visibility | None = None
    present: bool | None = None              # 仅镜头层有意义：强制该角色在 / 不在本镜
    # 审核元数据（不参与解析）
    reference_images: list[str] = Field(default_factory=list)
    candidate_items: dict[str, list[str]] = Field(default_factory=dict)
    candidate_references: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    needs_review: bool = False
    notes: list[str] = Field(default_factory=list)
    # 项目方确认（T51 / SOP）：只有项目方本人确认后才写；造型注入与 S2 就绪只认这里
    owner_confirmed_at: str | None = None
    owner_confirmed_ref: str | None = None   # 项目方原话 / 消息引用
    owner_confirmed_hash: str | None = None  # 确认时该集解析后造型内容的哈希（Resolver.core().hash）；内容变了确认作废


class LookSchedule(BaseModel):
    version: int = 2
    character_defaults: dict[str, str] = Field(default_factory=dict)          # 角色 → look_id
    episodes: dict[str, dict[str, LookPatch]] = Field(default_factory=dict)   # "17" → 角色 → patch
    shot_overrides: dict[str, dict[str, LookPatch]] = Field(default_factory=dict)  # "ep17-S3" → 角色 → patch
    shot_characters: dict[str, list[str]] = Field(default_factory=dict)       # 种子器检测的出场角色


# ---------------------------------------------------------------- 解析结果

class ResolvedSlot(BaseModel):
    items: list[str] = Field(default_factory=list)
    names: list[str] = Field(default_factory=list)
    source: Layer | None = None
    changed_from_prev: bool = False


class ResolvedField(BaseModel):
    value: str | None = None
    source: Layer | None = None
    changed_from_prev: bool = False


class LookConflict(BaseModel):
    kind: str          # script_item_not_in_look / script_item_unplanned / adornment_not_in_state /
                       # adornment_hidden / d_state_differs_from_script / missing_reference / unknown_ref
    severity: Literal["error", "warning", "info"]
    message: str
    slot: str | None = None
    item_id: str | None = None


class ResolvedLook(BaseModel):
    character: str
    name: str
    present_source: Literal["detected", "shot"]
    look_id: str | None = None               # 最具体那层给出的命名造型
    look_id_source: Layer | None = None
    look_name: str = ""
    label: str = ""                          # 造型条文字：“林晚 · 通勤 · D6”
    slots: dict[str, ResolvedSlot] = Field(default_factory=dict)
    d_state: ResolvedField = Field(default_factory=ResolvedField)
    visibility: ResolvedField = Field(default_factory=ResolvedField)
    reference_images: list[str] = Field(default_factory=list)
    has_override: bool = False
    prev_shot_id: str | None = None          # 该角色上一次出场的镜
    changed_from_prev: bool = False          # 换装点
    changed_fields: list[str] = Field(default_factory=list)
    conflicts: list[LookConflict] = Field(default_factory=list)
    needs_review: bool = False
    notes: list[str] = Field(default_factory=list)
    hash: str = ""                           # 解析内容哈希（含单品内容），变了 = 旧 take 过期


class CharacterLookPrompt(BaseModel):
    character: str
    name: str
    fragment: str
    triggers: list[str] = Field(default_factory=list)
    loras: list[LoraRef] = Field(default_factory=list)
    already_in_prompt: list[str] = Field(default_factory=list)   # 已在镜头原文/角色锁里的单品，不重复
    skipped_not_in_shot: list[str] = Field(default_factory=list)  # when_in_shot 单品本镜未出现 / 被隐藏


class LookPrompt(BaseModel):
    shot_id: str
    fragment: str
    triggers: list[str] = Field(default_factory=list)
    loras: list[LoraRef] = Field(default_factory=list)
    characters: list[CharacterLookPrompt] = Field(default_factory=list)


class ShotLooks(BaseModel):
    shot_id: str
    episode: int
    index: int
    script_decor_state: str | None = None
    look_hash: str                           # 本镜全部角色解析结果的哈希
    characters: list[ResolvedLook] = Field(default_factory=list)
    prompt: LookPrompt


class AffectedShots(BaseModel):
    affected_count: int
    affected_shots: list[str] = Field(default_factory=list)
    rendering_triggered: Literal[False] = False
