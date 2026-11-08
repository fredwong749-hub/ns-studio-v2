"""画布里的人工修改。只存差异，剧本原文不动；为空的字段表示沿用编译结果。"""
from pydantic import BaseModel, Field, field_validator


class Delivery(BaseModel):
    """送达 gpu3（经桥）的结果：ok=False 时画布显示「未送达 gpu3」+ 重试"""
    ok: bool
    at: str
    code: int | None = None          # 桥的 HTTP 状态码；None = 不可达。409 = gpu3 侧 base_sha 已过期
    error: str | None = None
    response: dict | None = None     # 桥的返回（h3_edit：saved + lint；override：request + requests）


class H3PromptOverride(BaseModel):
    """edits.json → h3_prompt_override（与 gpu3 约定的嵌套格式：text / by / at / base_sha / note）"""
    text: str                        # 改完正文 / 环境声后的完整四块文本
    by: str = "owner"
    at: str
    base_sha: str | None = None      # 修改时画布显示的提示词 sha256（UTF-8 原文；= gpu3 effective_sha256）
    script_sha: str | None = None    # 修改时剧本 **H3提示词** 的 sha256（剧本没有该字段为 null）；变了 = stale
    note: str                        # 项目方确认原话（只留在笔记本这份；桥的请求体没有 note）
    status: str = "draft"            # 笔记本 lint_h3 结果：renderable | draft（draft 不出视频，S4 仍阻塞）
    failed: list[str] = Field(default_factory=list)
    delivery: Delivery | None = None


class AuditOverrideRequest(BaseModel):
    """edits.json → audit_override_requests[]：项目方对某条审计规则的豁免请求（须原话）"""
    rule_id: str
    owner_quote: str
    by: str = "owner"
    at: str
    delivery: Delivery | None = None


PART_SLOTS: tuple[str, ...] = ("outfit", "shoes", "makeup", "nails", "hair", "accessory")


class CharRefOverride(BaseModel):
    """一个人物的参考覆盖（canvas-ui-spec-v3 §6）：整套形象 look（data/looks 的 look_id）+ 单件 parts（槽位 → 衣橱单品 id）。
    只记录与上一层不同的部分；只能选已有资产（API 校验）。"""
    look: str | None = None
    parts: dict[str, str] = Field(default_factory=dict)

    @field_validator("parts")
    @classmethod
    def _slots(cls, v: dict[str, str]):
        bad = [k for k in v if k not in PART_SLOTS]
        if bad:
            raise ValueError(f"parts 只能是 {PART_SLOTS}，收到 {bad}")
        return v

    def is_empty(self) -> bool:
        return not self.look and not self.parts


class RefOverrides(BaseModel):
    """本镜 / 本场参考覆盖（三层继承：集默认 → 场景 → 镜头）。scene = 场景底图 plate_id（assets/scenes 下相对路径）或 "none"。"""
    characters: dict[str, CharRefOverride] = Field(default_factory=dict)
    scene: str | None = None

    def is_empty(self) -> bool:
        return not self.scene and all(c.is_empty() for c in self.characters.values())


class ShotEdit(BaseModel):
    image_prompt: str | None = None     # 整段替换首帧提示词
    image_negative: str | None = None
    video_prompt: str | None = None     # 旧版视频提示词整段替换（只影响 legacy_video_prompt，不进 H3）
    camera: str | None = None           # 运镜，如 "camera push in 缓慢推近"；放在视频提示词最前
    expression: str | None = None       # 表情关键词，如 "睫毛颤动，咬唇"
    image_seed: int | None = None
    video_seed: int | None = None
    last_frame: str | None = None       # 目标尾帧：图片路径（相对 data/），或 "next" = 下一镜的首帧
    # 尾帧独立资产（canvas-ui-spec-v2 §3.5.1）
    last_prompt: str | None = None      # 整段替换尾帧提示词（生成模式）
    last_seed: int | None = None
    last_source: str | None = None      # gen | next | upload | video；None = 按 last_frame / 已有版本推断
    # 导演加强（项目方亲自写，原样追加到首帧/尾帧提示词，放在镜头内容之后、风格后缀之前；为空 = 不加任何东西）
    boost_first: str | None = None
    boost_last: str | None = None
    # H3 声音（E8，官方提示词指南 §4.6/§4.7）：替换视频提示词末尾的 overall_soundscape / non_diegetic_music 段
    soundscape: str | None = None
    h3_body: str | None = None          # 已停用（2026-10-30）：H3 修改只走下面的项目方覆盖；保留字段兼容旧 edits.json
    # H3 提示词的项目方覆盖（SOP §9A）：只能经 PUT /api/shots/{id}/h3-override 由项目方写，普通 PUT /edit 不能改；
    # 同一内容经 gpu3 桥写到 gpu3 的 renders_gpu3/epXX/SN/edits.json（本地这份用于显示）
    h3_prompt_override: "H3PromptOverride | None" = None
    # 项目方对 gpu3 审计规则的豁免请求（gpu3 合并进 status.json audit.overrides / invalid_overrides）
    audit_override_requests: list["AuditOverrideRequest"] = Field(default_factory=list)
    music: str | None = None
    # 本镜参考覆盖（§6，W5）：只能经 POST /api/shots/{id}/refs/apply 写，普通 PUT /edit 不改；空 = 沿用场景 / 集默认
    ref_overrides: RefOverrides = Field(default_factory=RefOverrides)
    note: str = ""
    updated_at: str | None = None

    @field_validator("ref_overrides", mode="before")
    @classmethod
    def _null_refs(cls, v):
        return RefOverrides() if v is None else v

    @field_validator("h3_prompt_override", mode="before")
    @classmethod
    def _old_flat_override(cls, v):
        return None if isinstance(v, str) else v    # 旧的扁平字符串格式（未上线）不认


class EpisodeEdit(BaseModel):
    """整集的导演加强（data/renders/epXX/episode_edit.json），先于镜头级加强追加"""
    boost_first: str | None = None
    boost_last: str | None = None
    music: str | None = None            # 整集默认背景音乐（non_diegetic_music），镜头没写时用
    # 场景层参考覆盖（§6，W5）：scene_id（state_timeline.group_scenes）→ 覆盖；只经 /refs/apply 写
    scene_overrides: dict[str, RefOverrides] = Field(default_factory=dict)
    updated_at: str | None = None

    @field_validator("scene_overrides", mode="before")
    @classmethod
    def _null_scenes(cls, v):
        return {} if v is None else v


class Take(BaseModel):
    """一次渲染结果"""
    n: int
    stage: str                          # image | last | video
    file: str                           # 相对 data/ 的路径
    seed: int
    prompt: str
    negative: str = ""
    seconds: float | None = None
    frames: int | None = None
    last_frame: str | None = None
    created_at: str
    workflow: str | None = None         # qwen_t2i | qwen_edit | h3_fl2va
    reference: str | None = None        # 锁脸参考图（相对项目根目录）
    qc: dict | None = None              # 自动质检结果，如 {"nudity": false, "hits": [], "clip_bare": 0.1}
    raw_file: str | None = None         # 调色前的原图（相对 data/）；file 为调色后的版本
    grade: dict | None = None           # 调色参数，如 {"method": "reinhard_lab", "strength": 0.6, "dark": false}
    score: dict | None = None           # 多候选评分（C5）：{"total", "parts": {...}, "missing": [...], "reasons": [...]}
    round: int | None = None            # 第几轮候选（1 起）
    references: list[str] | None = None  # 全部参考图（相对项目根目录，按 图1、图2… 顺序）
    look_hash: str | None = None        # 渲染时本镜造型解析哈希（API 在任务完成后补写），变了 = 本镜输入变化
    source: str | None = None           # 尾帧来源 gen | next | upload | video；首帧上传为 upload
    bible_version: str | None = None    # T06：出图时的规则版本（ns_agent.compiler.bible_version），信息用
    inputs_sha: str | None = None       # T51：出图时本镜输入哈希（state_timeline.json），与当前不同 = 过期
    # UX5-B P0（docs/ux_round5/B_revision.md）：出图 / 出视频时所用资产版本 {依赖键: rev}，如 identity:lin_wan、
    # preset:leica_sstar、vbase:official_base；当前 rev 不同 → “依赖已更新”（只标记，不进 stale，不触发整集重出）
    deps: dict[str, str] | None = None
    # fredw-92/F4：过期判定的提示词基准（出图时编译器给的本镜提示词）。prompt 不是编译提示词时才记：
    # 指令改图 / 换参考（prompt = 原话，继承父版本的基准）、画布一次性整段替换；"" = 无可比（父版本是上传 / 引用）
    basis_prompt: str | None = None
    # 精修循环（api_v1_contract §C，W2）：版本挂父版本形成修改链；改法 / 原话 / 幅度 / 参考都记在这一版上
    parent: int | None = None           # 同 stage 的上一版 n（regen 可为空）
    mode: str | None = None             # regen | instruct | refs（frame-jobs）；旧版本为空
    instruction: str | None = None      # 指令改图 / 换参考的原话（原样传给 Image 2.1）
    strength: str | None = None         # small | medium | large（对应 denoise，见 pipeline.EDIT_DENOISE）
    refs: list[dict] | None = None      # [{asset_id|path, role, file}]，按 图2、图3… 顺序（图1 = 父版本）
    locked: bool | None = None          # 定稿（takes.json locked[stage] == n；GET 时由 API 补全）
    from_first: int | None = None       # 尾帧 / 视频：由哪一版首帧得到（定稿首帧变了 → 过期）
    gpu: int | str | None = None        # 出图 / 视频用的道：gpu33 的卡号 int；其他服务器 "gpu36:2"（fredw-92/LS）
    job_id: str | None = None
    # 视频（api_v1_contract §D，W3）：本版渲染参数 {seed, steps, seconds, frames, lora_strength, prompt_source,
    # prompt_override, first:{n,sha}, last:{n,sha}, model:{…}}；首尾帧 n/sha 与当前定稿不同 = 过期
    params: dict | None = None
    # fredw-92/F1：下载时记下的 gpu3 端输出 {instance, base, gpu, subfolder, filename, type, sha256}；
    # 同一 ComfyUI 之后要用这张图时直接 LoadImage "subfolder/filename [output]"，不再经慢隧道上传
    remote: dict | None = None
    # fredw-92/S-back（S3）：首帧带上的场景底图 {scene_id, plate_id, path, sha256, source, slot}；
    # 底图文件换了版本（sha 不同）→ 本版过期「场景底图已改」
    scene_plate: dict | None = None
    # fredw-92/UXF1：坏图（ns_render.image_check：短边 < 256 px、文件 < 10 KB、尺寸与请求差 > 10 %）→ 原因；
    # 坏图不设为当前、不能定稿
    bad: str | None = None
    # S2.1：出图时的签名 {script_sig, prompt_sha, rule_fp, v}（tools/autopilot/take_sig.py）；与当前编译不符 = 过期，producer 按开关换代
    sig: dict | None = None
    quarantined: str | None = None      # 已挪进 takes/_quarantine/ 的时间（tools/quarantine_bad_takes.py）
