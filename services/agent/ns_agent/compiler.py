"""
提示词编译器（确定性）：把导入后的镜头编成首帧提示词和视频提示词。

规则来自《image提示词》v2：
- 首帧：触发词放最前 + 角色锁 + img 原文；LoRA 文件不存在时去掉触发词（原文描述保留）。
- 视频：§3.2 公式，motion 原文 + 声音 + 结尾固定句（保持首帧一致）。
- 透传：不加戏、不删词、不改数字。只追加全局画质后缀。
- 负面词里的“未成年人外观”是硬规则，任何配置都不能去掉。
- T06：规则从 config/visual_bible.toml 读（ns_agent.bible）；下面的常量只是 toml 缺键时的兜底值。
  编译结果带 bible_version（规则版本短哈希），渲染出的每个 take 都记录它。
"""
import json
import re
from dataclasses import dataclass, field
import os
from pathlib import Path

from ns_schema.script import Episode, ScriptGlobals, Shot

from . import framing as FR
from . import h3_prompt as H3
from .bible import Bible, load_bible
from .bible import bible_version as _bible_version

MINOR_GUARD = "未成年人外观"

# 全局画面风格（项目方 2026-10-30）：不要字幕；专业镜头画面感、干净；色彩丰富艳丽；大光圈。
# 调色参考 assets/style/color_reference_01.png：高调柔光、逆光发丝光、奶油般焦外光斑、粉蓝高饱和配色。
# 只描述摄影质感，不涉及画面内容；夜景/暗场/剪影镜头保留剧本光线，只要求色彩饱满。
DEFAULT_IMAGE_STYLE = (
    "专业电影摄影机实拍剧照，Leica Summilux 大光圈 f/1.4 浅景深，主体对焦锐利，背景奶油般柔和焦外与光斑，"
    "柔和逆光勾勒发丝，通透明亮的肤色，自然皮肤纹理，色彩丰富艳丽、饱和度高而干净，"
    "在剧本光线基础上色彩饱满不发灰，画面干净无任何文字，9:16竖构图"
)
VIDEO_STYLE = "专业电影镜头画面感，大光圈浅景深，色彩丰富艳丽，画面干净，无字幕无文字"
# 剧本的 motion 统一后缀已含“真人写实/9:16/单一运镜”，这里只补 §3.2 结尾固定句的剩余部分
VIDEO_TAIL = "动作流畅自然，保持首帧人物面部与装扮完全一致"
VIDEO_NEGATIVE = "人物变形，面部跳变，肢体崩坏，动作过快，画风变为动漫，出现多余人物，塑料皮肤，过度磨皮"
# 全部镜头追加的负面词：不要字幕/文字，不要灰暗低饱和
STYLE_NEGATIVE = "字幕、文字、水印、logo、标题字、画面发灰、低饱和、暗沉脏色、过曝死白"

# 成人镜头判定：命中任一关键词、人工指定，或整集标了 🔴（adult_overrides "per_shot": true 时改看本镜【亲密声】）
ADULT_KEYWORDS = (
    "乳环", "阴环", "铁珠", "珠串", "珠链", "阴唇", "乳头", "乳晕", "下体", "私处", "裸", "胴体",
    "做爱", "性爱", "交合", "插入", "抽插", "高潮", "射精", "内射", "精液", "口交", "舔", "三穴", "同填",
    "后入", "骑乘", "呻吟", "喘息", "丁字裤", "NSFW", "nprng2", "lbrng9",
    # 2026-10-30 补充：QC 证实这些场景即使剧本没写裸露也会出裸体
    "浴巾", "共浴", "淋浴", "浴缸", "阴阜", "腹股沟", "左乳", "右乳", "乳下", "乳房", "体内", "润滑",
    "塞入", "胸口睡", "舌钉",
)

# “裸”按语境判断（总调度 11-02）：妆容 / 颜色类词里的“裸”不算成人；裸体 / 裸露 / 全裸 等照样命中；其余单字“裸”仍判成人（失败关闭）
NUDE_BENIGN = ("裸妆", "裸色", "裸粉", "裸感", "裸粉美甲", "裸色丝袜")


def _keyword_hit(k: str, text: str) -> bool:
    if k == "裸":
        for w in sorted(NUDE_BENIGN, key=len, reverse=True):
            text = text.replace(w, "")
    return k in text


# 人工强制判为成人的镜头（QC 反复检出裸露、关键词没覆盖到的），见 config/adult_overrides.json
ADULT_OVERRIDES_PATH = Path(os.environ.get("NS_PROJECT_ROOT") or Path(__file__).resolve().parents[3]) / "config" / "adult_overrides.json"  # T42


def _forced_adult(bible: Bible | None = None) -> dict[str, str]:
    """人工指定成人：visual_bible [classification].overrides（默认 config/adult_overrides.json，按项目根目录）"""
    return (bible or load_bible()).forced_adult()


def bible_version(project_root=None) -> str:
    """规则版本短哈希（gpu3 也用它给 take 盖章）：visual_bible.toml + script_v2.json + data/looks/*.json +
    shot_ownership.json + 编译器规则代码版本。见 docs/state_timeline.md §一"""
    return _bible_version(project_root)

# 《3提示词纠正器》v2 的调参条目（按镜头特征追加，原文措辞照抄）
CLOSEUP_WORDS = ("特写", "极近", "近景", "大特写")
CLOSEUP_TERM = "面部清晰对焦，五官锐利"                                   # 1.1 特写镜脸型变形
DARK_WORDS = ("暗", "夜", "剪影", "黑暗", "深夜", "昏")
DARK_TERM = "暗部保留细节，面部可辨，画面光比1:4，非纯黑"                    # 1.2 暗调镜一片死黑
# 1.3 NSFW 模型自动加戏（安全镜）。B10（2026-10-30）：林晚日常穿着偏性感，不写“保守/职业装”，只防裸露
SAFE_SHOT_TERM = "衣装完整、无裸露"
SAFE_SHOT_NEGATIVE = "裸露、走光、低俗姿势"

# 项目方材质规则（对全部镜头生效，像纠正器条目一样追加在镜头内容之后、风格后缀之前；已有则不重复）
PATENT_WORDS = ("漆皮", "ptxtrench", "ptxcorset", "ptxharness", "hh12patent")
PATENT_TERM = "漆皮为像抹了油一样的高亮镜面漆皮，表面湿润光泽，镜面反射清晰，高光锐利"          # B8
HEEL_WORDS = ("高跟", "细跟", "hh12clear", "hh12patent")
HEEL_TERM = "鞋跟直径约6毫米、细如一根筷子的针形细跟，跟高12厘米"                              # B7


TOENAIL_WORDS = ("赤脚", "光脚", "脚趾", "露趾", "透明高跟", "hh12clear", "足弓", "脚背")
TOENAIL_TERM = "脚趾甲为2厘米长的性感美甲"     # B5：从林晚 DNA 锁移出，只在脚入镜时追加（避免无故赤脚）


def material_terms(text: str, lin_wan: bool = False, present: set[str] | None = None,
                   bible: Bible | None = None, already: str | None = None) -> list[str]:
    """text = 镜头原文（img，含触发词）。返回需要追加的材质条目（visual_bible [[materials]] 顺序；原文已含则跳过）。
    带 character 的条目（脚趾甲 → 林晚）只在该角色入镜时考虑：present 为入镜角色名集合；lin_wan=True 等同林晚入镜。
    already：去重对照文本（默认 = text）。尾帧按首帧原文 + 结束状态匹配，但只和尾帧自己的文字去重"""
    b = bible or load_bible()
    present = set(present or ()) | ({"林晚"} if lin_wan else set())
    seen = text if already is None else already
    out = []
    for m in b.materials:
        if m.character and m.character not in present:
            continue
        if any(w in text for w in m.match) and m.term not in seen and m.term not in out:
            out.append(m.term)
    return out
# 纠正器 1.1 的“网红脸”负面与 1.2 的蓝发色号锚定不启用：项目方要求以 assets/linwan 参考图为准（浅冰蓝长发、精致妆容）


# A2 Leica 锐度（research v6 §6）：写光学特征而不是质量形容词；与剧本后缀的焦段/光圈保持一致，
# 后缀里已有 Leica 时不再追加；大光圈焦外、浓郁色调由剧本后缀负责，这里不重复。
LENS_RE = re.compile(r"(\d{2,3})\s*mm[^，,]*?f/\s*([\d.]+)")
LENS_TEMPLATE = "Leica Summilux-SL {mm}mm f/{f} 级别的锐利解析力与微反差，焦平面上眼睛与睫毛清晰锐利，皮肤毛孔与发丝细节真实"


def lens_term(style: str, bible: Bible | None = None) -> str:
    """visual_bible [look] lens_template / lens_default / lens_skip_if；焦段光圈取自风格后缀"""
    b = bible or load_bible()
    if any(w in style for w in b.lens_skip_if):
        return ""
    from . import cast_table as _CT
    from . import asset_refs as _AR
    _p = _CT.current_project()
    if _p and _AR.manifest_rel(_p):     # PROMPTFIX 20:4x（L-126）：V2 不补 V1 visual_bible 的 Leica 镜头句（风格只按剧取 run_style.json）
        return ""
    m = LENS_RE.search(style)
    mm, f = (m.group(1), m.group(2)) if m else b.lens_default
    return b.scrub(b.lens_template.format(mm=mm, f=f))


# 尾帧（C3, research v6 §5）：motion 原文最后一个“——”之后的句子，没有破折号时取最后两个分句
CAMERA_PREFIX_RE = re.compile(r"^(?:camera|static|handheld|slow motion|time-lapse)[^，,—]*[，,]", re.I)
CAMERA_WORDS_RE = re.compile(r"^(?:camera|static|handheld|slow motion|time-lapse)[A-Za-z0-9 \-/.]*", re.I)


def split_camera(motion_body: str) -> tuple[str, str]:
    """motion 开头的运镜指令 → (运镜, 余下文字)。有逗号时到第一个逗号为止（含中文运镜描述，如“缓慢推近，”）；
    QA-05：没有逗号时只去掉英文运镜词（“camera orbit 缓慢环绕…”），不能把后面的动作吞掉"""
    text = motion_body.strip()
    m = CAMERA_PREFIX_RE.match(text) or CAMERA_WORDS_RE.match(text)
    if not m:
        return "", text
    return m.group(0).strip("，, "), text[m.end():].strip("，, ")


def split_dash(text: str) -> list[str]:
    """按“——”切分，跳过括号（…）里的破折号（QA-05：否则留下未闭合的“（”）"""
    segs, buf, depth, i = [], [], 0, 0
    while i < len(text):
        ch = text[i]
        if ch in "（(":
            depth += 1
        elif ch in "）)":
            depth = max(0, depth - 1)
        if depth == 0 and text.startswith("——", i):
            segs.append("".join(buf))
            buf = []
            i += 2
            continue
        buf.append(ch)
        i += 1
    segs.append("".join(buf))
    return [x for x in segs if x.strip()]
END_LEADING = ("然后", "随即", "接着", "之后")
SIMILE = ("像", "仿佛", "好像", "如同")   # “——像在问…”是比喻，不是动作，取前一段
LAST_FRAME_KEEP = "图1是本镜头的首帧画面。保持图1的场景、机位、景别、光线、人物身份、发型与服装完全不变（同一场景同一人物同一服装），只把画面改为这个镜头动作结束时的瞬间："


def end_state(motion_body: str) -> str:
    text = split_camera(motion_body)[1]
    segs = split_dash(text)
    while len(segs) > 1 and segs[-1].strip().startswith(SIMILE):
        segs.pop()
    if len(segs) > 1:
        tail = segs[-1]
    else:
        parts = [p for p in re.split(r"[，,；;]", segs[0] if segs else text) if p.strip()]
        tail = "，".join(parts[-2:])
    tail = tail.strip("，,。；; ")
    for w in END_LEADING:
        if tail.startswith(w):
            tail = tail[len(w):].lstrip("，, ")
    return tail


# FIX（ep02_iter P0，chain_audit A2/A3/C9）：系统后缀不得覆盖本镜原文的明确要求；cfg=1 不往正向词里塞否定句。
# 参数在 config/render_quality.json "prompt_guard"（缺键用这里的兜底值，值与配置一致）。
DEFAULT_PROMPT_GUARD = {
    # 本镜原文写了“脸被遮住” → 后缀里带“面部 / 五官”的子句不加（S11：蓝发遮住部分脸部，后缀却补“面部清晰对焦、面部可辨”）
    "face_hidden_words": ["遮住部分脸", "遮住脸", "遮住面部", "遮住她的脸", "遮住他的脸", "挡住脸", "挡住面部",
                          "遮脸", "不露脸", "看不到脸", "看不见脸", "只露出下巴"],
    "face_terms_drop": ["面部", "五官"],
    # 本镜原文 / 结束状态写了“全黑” → 后缀里的“非纯黑 / 面部可辨”不加（S11 尾帧“画面陷入全黑”，C9 视频变黑又被拉回）
    "black_words": ["全黑", "陷入黑暗", "一片漆黑", "漆黑一片", "黑屏", "纯黑"],
    "black_terms_drop": ["非纯黑", "面部可辨"],
    # cfg=1（Qwen-Image Turbo / H3 全部是 cfg 1）：负面词无效，正向词里的“无 X / 不 X / 非 X”反而把 X 注入画面
    # （S5 视频出现“冷”字）→ 系统后缀只保留正面子句；有改写的按 negation_rewrites 换成正面说法
    "cfg_one_positive_only": True,
    "negation_prefixes": ["无", "不", "非", "没有", "勿", "禁止", "避免"],
    "negation_rewrites": {"非纯黑": "暗部仍有可见层次"},
    # 所有格：“X 的 Y”里 Y 是身体部位 / 背影 → X 在画；其他（西装衣领、外套、家、声音…）只是提到（S8 顾沉的西装衣领）
    "possessive_body_words": ["手", "指", "脸", "面", "眼", "睫", "眉", "唇", "嘴", "鼻", "耳", "颈", "脖", "肩", "胸",
                              "背影", "背", "腰", "腿", "膝", "脚", "足", "臂", "胳膊", "掌", "怀", "身", "头", "额",
                              "下巴", "侧脸", "侧影", "剪影", "轮廓", "身影", "吻", "目光", "视线", "呼吸", "体温", "气息"],
    # 视频身份 LoRA（C2/E3）：按“在画角色”确定性挂载，不看编译时 GPU 道上有没有该文件（缺了在派单 / 出片时拒绝）。
    # 触发词 → 角色；角色在画且不是背对 → 挂；背对 / 不在画 → 不挂（S9/S10 陈宇主镜挂了林晚 LoRA）
    "video_identity_triggers": {"lnwan01": "林晚"},
    # 视频身份 LoRA 总开关：lin_wan_h3_v1（lnwan01）在 comfy/models.lock 仍是 status=training、gpu36 三条道都没有
    # （RR 10-31 因此整批不挂）。关 = 全部镜头确定性不挂（记原因）；开 = 在画且不背对的镜头必挂，道上没有就不派
    "video_identity_enabled": False,
    # 出图身份 LoRA（lin_wan_qi，config/auto_lora_triggers.json）同样按“在画”挂：不在画 → 不挂（剧本 img 触发词里写了也去掉）；
    # 背对（S9/S10 林晚背对、主体是陈宇）→ 不挂（model-only LoRA 整张图生效，会把她的脸套到主体上）
    "image_identity_skip_back_facing": True,
    "identity_skip_back_facing": True,
    "back_facing_patterns": ["{name}[^，。；,]{{0,4}}背对", "{name}的背影"],
}
_GUARD_CACHE: dict = {}


def prompt_guard(root=None) -> dict:
    """config/render_quality.json "prompt_guard"（按 mtime 缓存）合并到 DEFAULT_PROMPT_GUARD 上；读不到 → 兜底值"""
    from .bible import project_root
    p = project_root(root) / "config" / "render_quality.json"
    try:
        key = (str(p), p.stat().st_mtime_ns)
    except OSError:
        return dict(DEFAULT_PROMPT_GUARD)
    hit = _GUARD_CACHE.get(key)
    if hit is None:
        try:
            cfg = json.loads(p.read_text(encoding="utf-8")).get("prompt_guard") or {}
        except (OSError, ValueError, AttributeError):
            cfg = {}
        hit = {**DEFAULT_PROMPT_GUARD, **{k: v for k, v in cfg.items() if k in DEFAULT_PROMPT_GUARD}}
        _GUARD_CACHE.clear()
        _GUARD_CACHE[key] = hit
    return hit


def _split_clauses(term: str) -> list[tuple[str, str]]:
    """后缀条目 → [(子句, 后面的分隔符)]（按 ，/、 切）"""
    parts = re.split(r"([，、])", term)
    return [(parts[i], parts[i + 1] if i + 1 < len(parts) else "") for i in range(0, len(parts), 2)]


def guard_term(term: str, text: str, guard: dict | None = None) -> str:
    """一条系统后缀按本镜原文条件化：原文遮脸 → 去掉面部子句；原文全黑 → 去掉“非纯黑 / 面部可辨”；
    cfg=1 正向只留正面子句（否定子句按 negation_rewrites 改写，没有改写就去掉）。返回 "" = 整条不加"""
    g = guard or DEFAULT_PROMPT_GUARD
    face_hidden = any(w in text for w in g["face_hidden_words"])
    black = any(w in text for w in g["black_words"])
    keep: list[tuple[str, str]] = []
    for clause, sep in _split_clauses(term):
        c = clause.strip()
        if not c:
            continue
        if face_hidden and any(w in c for w in g["face_terms_drop"]):
            continue
        if black and any(w in c for w in g["black_terms_drop"]):
            continue
        if g.get("cfg_one_positive_only") and any(c.startswith(n) for n in g["negation_prefixes"]):
            c = (g.get("negation_rewrites") or {}).get(c, "")
            if not c:
                continue
        keep.append((c, sep or "，"))
    # 保留原分隔符（没有改动的条目原样返回）
    return "".join(c + (sep if i < len(keep) - 1 else "") for i, (c, sep) in enumerate(keep))


def corrector_terms(img_body: str, adult: bool, bible: Bible | None = None) -> list[str]:
    """visual_bible [[shot_rules]]（按顺序）+ 非成人镜头的 [shot_rules_safe].term。
    FIX（P0）：每条按本镜原文条件化（guard_term）——原文遮脸 / 全黑时不再补“面部可辨 / 非纯黑”，cfg=1 不加否定子句"""
    b = bible or load_bible()
    terms = [r.term for r in b.shot_rules if any(w in img_body for w in r.match)]
    if not adult:
        terms.append(b.safe_term)
    g = prompt_guard(getattr(b, "root", None))
    out = []
    for t in terms:
        gt = guard_term(t, img_body, g)
        if gt and gt not in out:
            out.append(gt)
    return out


# 编译后静态校验（refactor_proposal §3.1）：互斥词对、正向否定式、未在画角色的角色锁
CONFLICT_PAIRS = (("face_hidden_words", ("面部可辨", "面部清晰对焦", "五官锐利")), ("black_words", ("非纯黑",)))


def back_facing(name: str, text: str, guard: dict | None = None) -> bool:
    g = guard or DEFAULT_PROMPT_GUARD
    return any(re.search(p.format(name=re.escape(name)), text) for p in g["back_facing_patterns"])


def video_loras_for(motion_triggers: list[str], present: list[str], img_body: str, registry: dict,
                    guard: dict | None = None) -> tuple[list[tuple[str, float]], list[str], list[str]]:
    """FIX（P0，C2/E3）：视频 LoRA 是 (剧本触发词, 在画角色) 的纯函数，与 GPU 道无关。
    registry = {触发词: globals.lora_triggers 条目}。→ (loras, 用到的触发词, 去掉的触发词及原因)"""
    g = guard or DEFAULT_PROMPT_GUARD
    ident = dict(g.get("video_identity_triggers") or {})
    loras: list[tuple[str, float]] = []
    kept, dropped = [], []

    def add(t, info):
        strength = info.strength[1] if info.strength else 0.8
        if (info.file, strength) not in loras:
            loras.append((info.file, strength))
        kept.append(t)
    wanted = list(motion_triggers)
    for t, who in ident.items():   # 在画角色的身份触发词：剧本 motion 没写也补上（按角色确定）
        if who in present and t in registry and t not in wanted and getattr(registry[t], "kind", "video") == "video":
            wanted.append(t)
    for t in wanted:
        info = registry.get(t)
        if info is None:
            dropped.append(f"{t}（剧本 LoRA 表里没有）")
            continue
        who = ident.get(t)
        if who is not None:
            if not g.get("video_identity_enabled"):
                dropped.append(f"{t}（视频身份 LoRA 未启用：config prompt_guard.video_identity_enabled）")
                continue
            if who not in present:
                dropped.append(f"{t}（{who}不在画）")
                continue
            if g.get("identity_skip_back_facing") and back_facing(who, img_body, g):
                dropped.append(f"{t}（{who}背对镜头）")
                continue
        add(t, info)
    return loras, kept, dropped


def lint_prompt(prompt: str, own_text: str, guard: dict | None = None) -> list[str]:
    """own_text = 本镜原文（img / img+结束状态）。返回问题列表（空 = 通过）。只查系统该负责的部分：
    原文要求遮脸 / 全黑而提示词里仍有对立的系统词"""
    g = guard or DEFAULT_PROMPT_GUARD
    out = []
    for key, bad in CONFLICT_PAIRS:
        if any(w in own_text for w in g[key]):
            hit = [w for w in bad if w in prompt and w not in own_text]
            if hit:
                out.append(f"原文含「{next(w for w in g[key] if w in own_text)}」，提示词里却有系统词「{'、'.join(hit)}」")
    return out


def is_dark(img_body: str, bible: Bible | None = None) -> bool:
    return any(w in img_body for w in (bible or load_bible()).dark_words)


# 角色在首帧画面中出现的判定词
CHARACTER_HINTS = {"林晚": ("林晚", "蓝发"), "顾沉": ("顾沉",), "陈宇": ("陈宇",)}
# QA-03：“顾沉家客厅”“陈宇的公司”是地点，不是人物入镜
POSSESSIVE_PLACES = r"(?:的)?(?:家|公寓|公司|办公室|车|楼下|楼|别墅|卧室|浴室|客厅)"
POSSESSIVE_RE = re.compile(r"(林晚|顾沉|陈宇)" + POSSESSIVE_PLACES)
_POSS_CACHE: dict = {}


def possessive_re(bible: Bible | None = None) -> re.Pattern:
    """人名取自 visual_bible [characters.*].name（缺省 = 林晚/顾沉/陈宇）"""
    names = tuple(c.name for c in (bible or load_bible()).characters) or ("林晚", "顾沉", "陈宇")
    if names not in _POSS_CACHE:
        _POSS_CACHE[names] = re.compile("(" + "|".join(map(re.escape, names)) + ")" + POSSESSIVE_PLACES)
    return _POSS_CACHE[names]


def without_possessive_places(text: str, bible: Bible | None = None) -> tuple[str, list[str]]:
    """去掉所有格地点，返回 (文本, 被去掉的人名)"""
    rx = possessive_re(bible)
    names = [m.group(1) for m in rx.finditer(text)]
    return rx.sub("", text), names


POSSESSIVE_NOT_BODY = ("手机", "手表", "手提", "手帕", "手套", "身份", "面试", "头像", "背包", "脚本")


def without_possessive_mentions(text: str, bible: Bible | None = None) -> tuple[str, list[str], list[str]]:
    """FIX（P0 ⑦，chain_audit A4）：“提到名字” ≠ “人在画面”。去掉所有格地点（同 without_possessive_places），
    再去掉“X 的 <物件>”里的人名（“顾沉的西装衣领”只是道具归属）；“X 的 <身体部位 / 背影>”保留（人在画）。
    → (文本, 地点所有格的人名, 物件所有格的人名)"""
    b = bible or load_bible()
    text, places = without_possessive_places(text, b)
    names = tuple(c.name for c in b.characters) or ("林晚", "顾沉", "陈宇")
    body = tuple(prompt_guard(getattr(b, "root", None))["possessive_body_words"])
    rx = re.compile("(" + "|".join(map(re.escape, names)) + ")的")
    objects: list[str] = []

    def sub(m: re.Match) -> str:
        rest = text[m.end():m.end() + 6]
        if rest.startswith(body) and not rest.startswith(POSSESSIVE_NOT_BODY):
            return m.group(0)
        objects.append(m.group(1))
        return ""
    return rx.sub(sub, text), places, objects


@dataclass
class CompiledShot:
    shot_id: str
    adult: bool
    adult_reasons: list[str]
    image_prompt: str
    image_negative: str
    image_loras: list[tuple[str, float]]
    video_prompt: str
    video_negative: str
    video_loras: list[tuple[str, float]]
    dropped_triggers: list[str] = field(default_factory=list)
    # 尾帧（C3）：用定稿首帧做编辑，画面改为动作结束时的状态；end_state 是 motion 原文里的收尾动作
    last_prompt: str = ""
    end_state: str = ""
    image_body: str = ""        # 剧本 img 原文（去触发词），质检逐条核对用
    boost_first: str = ""       # 导演加强（整集 + 镜头），已含在 image_prompt / last_prompt 里
    boost_last: str = ""
    prompt_method: str = "script"   # script = 剧本原文透传；spec = 画面说明层拼装
    video_soundscape: str = ""       # 视频提示词末尾 overall_soundscape 段（E8）
    video_soundscape_source: str = ""   # edit | script_h3 | script
    video_music: str = "N/A"         # non_diegetic_music 段：一律 N/A（背景音乐只在后期加）
    video_music_source: str = "fixed"
    post_music: dict | None = None   # 后期配乐：episode_prompt / episode_bgm / shot_cue / source（edit|script|none）
    visual_spec: dict | None = None  # Frame Spec（ns_schema.frame_spec.FrameSpec 的 dict）
    dark: bool = False          # 暗场/夜景镜头：调色时不提亮
    bible_version: str = ""     # T06：编译时的规则版本（visual_bible + 剧本 + 造型 + 归属）
    inputs_sha: str = ""        # T51：本镜输入哈希（state_timeline.json；渲染流水线填写），变了 = 旧 take 过期
    look_fragments: list[str] = field(default_factory=list)   # [looks] inject 打开时注入的造型片段（P2）
    banned_hits: list[str] = field(default_factory=list)      # 剧本原文里出现的禁用词（原文不改，只提示）
    # 2026-10-30（SOP §9A）：video_prompt = H3 官方四块提示词（剧本 **H3提示词** 原样，或笔记本构建器草稿）；
    # 旧版中文拼接（motion + 后缀 + 风格 + 固定句）只留作历史，不进 H3；video_negative 只留在数据里（cfg 1 无负向）
    legacy_video_prompt: str = ""
    h3_prompt_source: str = ""      # script | laptop
    h3_frames: int = 0
    h3_missing: list[str] = field(default_factory=list)   # 笔记本草稿缺英文的块：body / soundscape
    h3_base_prompt: str = ""         # 覆盖前的 H3 提示词（剧本 / 笔记本草稿），项目方修改的起点
    h3_override: dict | None = None  # 项目方覆盖（ShotEdit.h3_prompt_override + 元数据），apply_edit 填写
    h3_norm: list[dict] = field(default_factory=list)   # DIRECTOR K-REN-801：编译层运镜句规范化的改动 [{rule, from, to}]；H3 正文除此之外只能经项目方覆盖改
    h3_template: str = ""            # 阶段 2.1：视频提示词模板（"" = 现行 v1；"v2" = 官方写法重编，改动在 h3_norm 里带 template）
    # FIX（ep02_iter P0）：视频 LoRA 按在画角色确定（与 GPU 道无关）；去掉的触发词及原因；只被提到（物件所有格）的角色；
    # 编译后静态校验（原文遮脸 / 全黑却仍有对立系统词）
    video_loras_dropped: list[str] = field(default_factory=list)
    image_loras_dropped: list[str] = field(default_factory=list)   # 出图身份 LoRA 没挂的原因（不在画 / 背对）
    # CHAIN F8：H3 全是 cfg 1（BasicGuider），video_negative 不参与采样、提交时 negative=""；只留作记录，不生效
    video_negative_status: str = "ignored_cfg1"
    video_music_warn: list[str] = field(default_factory=list)
    h3_lips_closed_added: bool = False   # 纯独白镜由编译器补了嘴唇闭合句（项目方 2026-10-31“加”）   # CHAIN F5：配乐过 H3-S3 的提示（warn，不改剧本）
    mentioned_only: list[str] = field(default_factory=list)
    prompt_lint: list[str] = field(default_factory=list)


def _strip_triggers(text: str, triggers: list[str]) -> str:
    for t in triggers:
        text = text.lstrip()   # QA-01：剧本里“a, b, c，…”逗号后带空格，第二个起要先去掉前导空格
        for sep in ("，", ","):
            if text.startswith(t + sep):
                text = text[len(t) + 1:]
                break
        else:
            if text.startswith(t):
                text = text[len(t):]
    return text.strip()


def _join(*parts: str) -> str:
    return "，".join(p.strip("，, ") for p in parts if p and p.strip("，, "))


def _negative(*parts: str, bible: Bible | None = None) -> str:
    text = "、".join(p.strip("、，, ") for p in parts if p and p.strip())
    guards = (MINOR_GUARD, *(bible.extra_minor_guards if bible else ()))   # 硬规则，不可覆盖
    for gd in guards:
        if gd not in text:
            text = f"{text}、{gd}" if text else gd
    return text


def auto_lora_triggers(root=None) -> dict[str, list[dict]]:
    """config/auto_lora_triggers.json 的 characters（与 tools/import_script.py 同一份；导入时已补进 img_triggers）"""
    from .bible import project_root
    try:
        d = json.loads((project_root(root) / "config" / "auto_lora_triggers.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    banned = banned_image_loras(root)
    return {name: [e for e in es if str(e.get("file") or "") not in banned and e.get("status") != "banned"]
            for name, es in (d.get("characters") or {}).items()}


def banned_image_loras(root=None) -> set[str]:
    """契约 I8（config/gen_contract.json lora_banned）：已证伪 / 项目方去掉的出图 LoRA，编译器一律不挂、触发词剥掉。
    lin_wan_qi_v1：同流程身份 0.926→0.823（RSI B6）+ 项目方 2026-10-31 决定“A. 去掉”"""
    from .bible import project_root
    try:
        p = project_root(root) / "config" / "gen_contract.json"
        return set((json.loads(p.read_text(encoding="utf-8")).get("lora_banned") or {}))
    except (OSError, ValueError):
        return set()


def identity_triggers(names, existing, bible: Bible | None = None) -> list[tuple[str, str, float]]:
    """入镜角色的系统自动补 image LoRA（config/auto_lora_triggers.json，项目方 2026-10-30“系统自动补”）
    → [(触发词, 文件, 强度)]，existing 里已有的跳过。导入器已经把它写进 img_triggers；编译器只保证它在首帧 / 尾帧
    触发词最前、且本机没有该 LoRA 文件时也不被剥掉（gpu3 按 LORA_REGISTRY accepted + 触发词加载）"""
    b = bible or load_bible()
    out = []
    for name, entries in auto_lora_triggers(b.root).items():
        if name not in names:
            continue
        for e in entries:
            t = str(e.get("trigger") or "")
            if t and e.get("kind", "image") == "image" and t not in existing and t not in [x[0] for x in out]:
                out.append((t, str(e.get("file") or ""), float(e.get("strength") or 1.0)))
    return out


def classify_adult(ep: Episode, shot: Shot, bible: Bible | None = None) -> list[str]:
    """默认（项目方 2026-10-30 定“恢复整集判定”）：集标题 🔴 → 本集每镜 adult。
    config/adult_overrides.json "per_shot": true 时改为逐镜：本镜【亲密声】非空、命中关键词、或人工指定"""
    b = bible or load_bible()
    reasons = []
    if not b.adult_per_shot():
        if ep.high_intensity:
            reasons.append("整集🔴")
    elif (getattr(shot, "sound_intimate", "") or "").strip():
        reasons.append("亲密声")
    text = " ".join(filter(None, [shot.img_prompt, shot.motion_prompt, shot.sound,
                                  shot.voiceover.text if shot.voiceover else "",
                                  *(e.text for e in shot.extras)]))
    reasons += [f"关键词:{k}" for k in ADULT_KEYWORDS if _keyword_hit(k, text)]
    if shot.id in (forced := _forced_adult(b)):
        reasons.append(f"人工指定:{forced[shot.id]}")
    return reasons


# E8 H3 声音（docs/h3_prompt_guide_base_en.txt §2.2/§4.6/§4.7）：视频提示词末尾两个带标签的段落
SPEECH_WORDS = ("“", "”", "\"", "语声", "说话", "台词", "对白", "独白", "旁白", "低语", "念", "喊", "唱")


def script_soundscape(sound: str) -> str:
    """剧本 sound 行 → overall_soundscape：去掉对白/人声语句（§4.6：对白属于 multimodal description，不在此重复）"""
    parts = [x.strip() for x in re.split(r"[，,；;、]", sound or "") if x.strip()]
    return "，".join(x for x in parts if not any(w in x for w in SPEECH_WORDS))


def soundscape_block(soundscape: str, music: str) -> str:
    return f"\n\noverall_soundscape: {soundscape or 'N/A'}\n\nnon_diegetic_music: {music or 'N/A'}"


def boost_text(*boosts: str | None) -> str:
    """导演加强：整集在前、镜头在后，原样拼接（不改写、不过滤）；全空返回空串"""
    return "，".join(b for b in boosts if b and b.strip())


def insert_before_style(prompt: str, extra: str, style: str, bible: Bible | None = None) -> str:
    """把 extra 插到提示词里的风格后缀（及其前面的 Leica 段）之前；找不到后缀就接在最后"""
    if not extra:
        return prompt
    for anchor in (lens_term(style, bible), style):
        if anchor and anchor in prompt:
            i = prompt.rfind(anchor)
            return f"{prompt[:i].rstrip('，, ')}，{extra}，{prompt[i:]}"
    return f"{prompt.rstrip('，, ')}，{extra}"


_ITEM_SUFFIX_RE = re.compile(r"（(?:产品|上身|平铺[^）]*|设计稿|正|侧 ?45°|后跟|手背)）$")
_RESOLVER_CACHE: dict = {}


def _resolver(root):
    """data/looks 解析器（按文件 mtime 缓存）；读不到返回 None"""
    from .bible import _stat_key
    from .looks import LookStore
    from . import run_paths as RP                         # I-14：按剧取路径（V2 不读 V1 的 data/looks / 导入稿 / 渲染目录）
    looks_dir, script = RP.looks_dir(root), RP.script_json(root)
    key = _stat_key([script, *(looks_dir / f"{n}.json" for n in ("wardrobe", "looks", "schedule")),
                     RP.renders_dir(root) / "ref_overrides.json"])   # W5：画布参考覆盖索引
    hit = _RESOLVER_CACHE.get(str(root))
    if hit and hit[0] == key:
        return hit[1]
    try:
        store = LookStore(looks_dir, script)
        res = store.resolver() if store.exists() else None
    except Exception:  # noqa: BLE001 — 造型数据坏了不影响编译（不注入）
        res = None
    _RESOLVER_CACHE[str(root)] = (key, res)
    return res


def item_text(item) -> str:
    """单品进提示词的文字：description，没有就用名称（去掉“（产品）”等资产图标注）"""
    return (item.description or _ITEM_SUFFIX_RE.sub("", item.name or "")).strip()


def episode_confirmed(res, episode: int, slug: str) -> bool:
    return res.episode_confirmed(slug, episode)    # 须原话 + 内容哈希未变（ns_agent.looks.Resolver）


def look_fragments(shot: Shot, present: list[str], img_text: str, locks: list[str], bible: Bible,
                   confirmed_only: bool = True, canvas_only: bool = False) -> list[str]:
    """P2 造型注入：每个入镜角色一段“【名字造型】服装，鞋，妆，发”。
    只取 [looks] slots 指定的槽位；剧本原文、角色锁、材质规则里已有的文字不重复；
    confirmed_only：只对项目方确认过的集计划（LookPatch.owner_confirmed_at）注入。
    画布参考覆盖（W5，§6）：项目方在画布上换过的槽位（场景 / 本镜覆盖）总是注入——集未确认或 [looks] inject 关闭时
    （canvas_only=True）只注入这些槽位。"""
    from ns_schema.looks import CHARACTER_SLUG, SLOTS

    from .looks import item_in_shot
    res = _resolver(bible.root)
    if res is None:
        return []
    rules_text = "".join(m.term for m in bible.materials)
    out = []
    for name in present:
        slug = CHARACTER_SLUG.get(name)
        if not slug:
            continue
        canvas = res.canvas_slots(slug, shot.episode, shot.id) if hasattr(res, "canvas_slots") else []
        full = not canvas_only and not (confirmed_only and not episode_confirmed(res, shot.episode, slug))
        if not full and not canvas:
            continue
        r = res.core(slug, shot.episode, shot.id)
        parts: list[str] = []
        use = set(bible.looks_inject_slots if full else ()) | set(canvas)
        for slot in [s_ for s_ in SLOTS if s_ in use]:
            for iid in (r.slots[slot].items if slot in r.slots else []):
                it = res.items.get(iid)
                if not it or (it.prompt_policy == "when_in_shot" and not item_in_shot(it, shot)):
                    continue
                txt = bible.scrub(item_text(it))
                if (not txt or txt in parts or txt in img_text or any(txt in lk for lk in locks)
                        or txt in rules_text):
                    continue
                parts.append(txt)
        if parts:
            out.append(f"【{name}造型】" + "，".join(parts))
    return out


SCENE_SEGMENT_FIELDS = (("名称", "name"), ("空间描述", "space"), ("时段", "time_of_day"), ("光线", "light"),
                        ("固定陈设", "fixtures"))


def _no_segment_marks(text: str | None) -> str | None:
    """L-130（ISS-1103-005241-bf53）：【幻想段】/【现实段】段落标记只给调色 / 元数据用（frame_job 用 style.segment_of 读剧本原字段判段），
    编译器产出的任何提示词里都不许出现。标记写法只有一个来源：gen_contract.style.strip_segment_marks（懒加载，避免循环导入）"""
    if not text:
        return text
    from .gen_contract import style as _ST
    return _ST.strip_segment_marks(text)


def scene_segment(shot: Shot, g: ScriptGlobals) -> str:
    """S3（fredw-92/S-back）：首帧提示词的【场景】段。冒号前是系统字段名，冒号后逐字取剧本场景表（ScriptGlobals.scene_library）
    与本镜 **场景** 行的细节，不加任何词；剧本没写场景 → ""（旧剧本提示词不变）"""
    sid = getattr(shot, "scene_id", None)
    detail = (_no_segment_marks(getattr(shot, "scene_detail", None)) or "").strip()   # L-130：段落标记不进出图提示词
    if not sid:
        return ""
    entry = next((e for e in getattr(g, "scene_library", None) or [] if e.scene_id == sid), None)
    parts = [f"{label}：{getattr(entry, attr)}" for label, attr in SCENE_SEGMENT_FIELDS
             if entry is not None and getattr(entry, attr)]
    if detail:
        parts.append(f"本镜场景：{detail}")
    return "【场景】" + "；".join(parts) if parts else ""


def compile_shot(ep: Episode, shot: Shot, g: ScriptGlobals, available_loras: set[str],
                 image_style: str | None = None, boost_first: str = "", boost_last: str = "",
                 soundscape: str | None = None, music: str | None = None, episode_music: str | None = None,
                 bible: Bible | None = None, inject_looks: bool | None = None,
                 inject_confirmed_only: bool = True, framing_project: str | None = None) -> CompiledShot:
    """framing_project：景别取舍开关按哪个项目键查（config/recipes/image_framing.json per_project；不给 = 按 bible.root 推断。
    流水线跑出来、尚未成为独立项目的新剧（如 series10_v1）用它，避免被当成本仓库默认项目的同号集）。
    boost_first / boost_last：项目方的“导演加强”（整集 + 镜头，已按顺序拼好），原样放在镜头内容之后、风格后缀之前。
    bible：规则（默认读当前项目 config/visual_bible.toml）；inject_looks：None = 按 [looks] inject；
    inject_confirmed_only=False 只给 bible_diff --inject-preview 预览用（正式编译只注入项目方确认过的集）"""
    b = bible or load_bible()
    # 画面风格以剧本里的“每条img统一后缀”为准，剧本没有时才用 visual_bible [look].fallback_suffix，避免重复
    if image_style is None:
        image_style = g.image_suffix or b.scrub(b.image_style_fallback)
    by_trigger = {t.trigger: t for t in g.lora_triggers}
    available_loras = set(available_loras) - banned_image_loras(b.root)     # 契约 I8：禁用 LoRA 不挂、触发词剥掉

    def loras_for(triggers: list[str], dropped: list[str]) -> tuple[list[str], list[tuple[str, float]]]:
        kept, loras = [], []
        for t in triggers:
            info = by_trigger.get(t)
            if info and info.file in available_loras:
                kept.append(t)
                strength = info.strength[1] if info.strength else 0.8
                if (info.file, strength) not in loras:
                    loras.append((info.file, strength))
            else:
                dropped.append(t)
        return kept, loras

    dropped: list[str] = []
    img_kept, img_loras = loras_for(shot.img_triggers, dropped)
    # 只给 legacy_video_prompt（死字段，不进 H3）用，保持旧口径
    vid_kept = [t for t in shot.motion_triggers if t in by_trigger and by_trigger[t].file in available_loras]

    img_body = _no_segment_marks(_strip_triggers(shot.img_prompt, shot.img_triggers)) or ""      # L-130：标记不进正文
    img_body = FR.off_axis_gaze(img_body)                  # C-07 / W38：静帧“视线落在画面右前方”≈ 看镜头 → 改向画面一侧
    reasons = classify_adult(ep, shot, b)
    lock_names = [c.name for c in g.character_locks]
    names = present_characters(shot, img_body, lock_names + [c.name for c in b.characters if c.name not in lock_names], b)
    guard = prompt_guard(b.root)
    # FIX P0（C2/E3）：视频 LoRA 只看剧本触发词 + 在画角色（+ 是否背对），不看 available_loras
    vid_loras, _vid_used, vid_dropped = video_loras_for(shot.motion_triggers, names, img_body, by_trigger, guard)
    mentioned = sorted({n for n in without_possessive_mentions(img_body, b)[2] if n not in names})
    rules = {c.name: c for c in b.characters}
    # 出图身份 LoRA 触发词放在所有触发词最前（M10：pose_* 仍在其它触发词之后）。触发词总是保留（gpu3 按
    # LORA_REGISTRY accepted + 触发词加载）；本机有该文件时才进 image_loras
    ident = identity_triggers(names, (), b)                # 导入已补 / 剧本已写 / 本机缺文件被剥掉的，统一放最前
    first = [t for t, _f, _s in ident]
    img_kept = first + [t for t in img_kept if t not in first]
    dropped = [t for t in dropped if t not in img_kept]
    files = {f for _t, f, _s in ident}
    img_loras = [(f, s) for _t, f, s in ident if f and f in available_loras] + \
        [x for x in img_loras if x[0] not in files]
    # FIX（ep02_iter，chain_audit C2）：出图身份 LoRA 按“在画且不背对”挂（触发词也一并去掉）
    img_id_dropped = []
    id_trig = {str(e.get("trigger")): (name, str(e.get("file") or "")) for name, es in auto_lora_triggers(b.root).items()
               for e in es if e.get("trigger") and e.get("kind", "image") == "image"}
    for t in list(img_kept):
        if t not in id_trig:
            continue
        who, f = id_trig[t]
        why = (f"{who}不在画" if who not in names else
               f"{who}背对镜头" if guard.get("image_identity_skip_back_facing") and back_facing(who, img_body, guard)
               else None)
        if why:
            img_kept.remove(t)
            img_loras = [x for x in img_loras if x[0] != f]
            img_id_dropped.append(f"{t}（{why}）")

    def lock(c) -> str:
        extra = b.scrub(rules[c.name].identity_extra) if c.name in rules and rules[c.name].identity_extra else ""
        return f"【{c.name}】{c.description}" + (f"，{extra}" if extra and extra not in c.description else "")

    # 《image提示词》§1.2：触发词最前，角色 DNA 锁紧随其后（纠正器 1.1：DNA 段提到最前）
    locks = [lock(c) for c in g.character_locks if c.name in names]
    # K-REN-705 b（IMGFIX 11-02，AUDIT P2）：局部出场的人（剧本 cast：局部｜部位，原文没写他的脸）角色锁只留看得到的部位 + 成年句
    # （framing.part_lock，与局部特写同一套），去掉脸 / 五官 / 刘海等外貌词，免得模型又画出一张没有身份参考的脸
    _pmap = dict(FR.faced_people(shot, names)[1])
    if _pmap:
        def _pl(lk):
            m = re.match(r"【([^】]+)】", lk)
            return FR.part_lock(lk, _pmap[m.group(1)]) if m and m.group(1) in _pmap else lk
        locks = [x for x in map(_pl, locks) if x]
    inject = b.looks_inject if inject_looks is None else inject_looks
    # [looks] inject 关闭时仍注入画布参考覆盖换过的槽位（W5：项目方在画布上选的资产，canvas_only）
    frags = look_fragments(shot, [c.name for c in g.character_locks if c.name in names], shot.img_prompt, locks, b,
                           confirmed_only=inject_confirmed_only, canvas_only=not inject)
    material_text = shot.img_prompt + ("，" + "，".join(frags) if frags else "")
    materials = [m for m in material_terms(material_text, present=set(names), bible=b)
                 if not any(m in lk for lk in locks)]   # 锁里已有（旧剧本）就不重复
    # K-REN-101（PROMPT 2026-10-31）：按景别只写画面里看得见的造型部件，首句先写景别构图（config/recipes/image_framing.json 开关）
    frame_size = FR.size_of(img_body) if FR.enabled(shot.episode, shot.index, b.root, project=framing_project) else None
    framing_lead = FR.lead(frame_size)
    subject = None
    two_shot = ""
    if FR.hidden_words(frame_size) and not FR.lower_focus(shot.img_prompt, kept_triggers=img_kept):
        locks = [FR.filter_parts(lk, frame_size) for lk in locks]
        frags = [FR.filter_parts(fg, frame_size) for fg in frags]
        img_body = FR.filter_parts(img_body, frame_size, subjects=names)   # K-REN-901：删句保主语
    if frame_size:
        # K-REN-123（IMGGEN 第二轮）：特写按主体（脸 / 局部）写构图约束；脸部特写去掉站姿 / 全身分句
        subject = FR.closeup_subject(img_body, frame_size)
        # K-REN-903（IMGLAB 11-02，ep05-S3/S6/S7）：在画的人全是局部（不露脸）→ 按局部特写编译（局部首句、去脸部通用句、
        # 不写“不露脸”否定句、锁收窄），不论剧本景别词是不是特写
        _po_all = FR.partial_only(shot, names)
        _po = _po_all and subject not in ("part", "object")
        if _po:
            subject = "part"
        if subject == "face":
            img_body = FR.strip_fullbody(img_body, subjects=names)          # K-REN-901：删句保主语
        if subject in ("part", "object"):
            # REFLOCK 11-01（ep05-S3/S5、ep10-S8 局部大特写出成带脸的中景）：角色锁里的脸 / 发型 / 身材描写会把模型拉回人像，
            # 局部特写只留锁里与这个局部相关的分句（例：指甲）；锁一句都不剩就整条不放
            locks = [x for x in (FR.part_lock(lk, FR.part_name(img_body)) for lk in locks) if x]
            if _po_all:
                locks = [x for x in (FR.narrow_lock(lk, FR.part_name(img_body)) for lk in locks) if x]
        framing_lead = FR.closeup_lead(frame_size, subject, img_body, partial=_po)
        # K-REN-705（IMGFIX 11-02，项目方 00:05“很多人物face都出了问题”，ep02-S4）：双人句按“露脸的人数”触发——
        # 原先按 names（在画 + 局部）计数，21 镜第二个人是局部（背影肩线 / 西装下摆）也被写了“两张脸都清晰入画、互相看着”，
        # 模型画出一张没有身份参考的脸。局部的人按剧本 cast 的部位写“只露出 X，不露脸”（剧本原文）
        faced, partial = FR.faced_people(shot, names)
        # K-REN-902（IMGLAB 11-02）：脸部特写 + ≥2 人露脸 → 首句改双人近景构图（不再同时写“脸占六成”和“两张脸都入画”）
        if FR.two_face_close_conflict(frame_size, subject, len(faced)) and FR.two_face_close_mode() == "two_shot_mcu":
            framing_lead = FR.TWO_FACE_CLOSE_LEAD
        two_shot = FR.two_shot_clause(len(faced), subject, img_body, faced)
        if two_shot:
            framing_lead = framing_lead + "，" + two_shot
        from ns_agent import asset_refs as _AR
        hide = FR.partial_clause(partial, subject, {n: _AR.back_outer_clause(framing_project, ep, shot, n) for n, _p in partial})
        if hide:
            framing_lead = framing_lead + "，" + hide
        # 材质条目按“过滤后”的可见文字重新匹配（漆皮短裙不在画面里就不补漆皮条目）
        material_text = img_body + ("，" + "，".join(frags) if frags else "")
        materials = [m for m in material_terms(material_text, present=set(names), bible=b)
                     if not any(m in lk for lk in locks) and FR.visible(m, frame_size)]
    # 项目方 2026-10-30：规格词由系统自动补；写作者原文里已有同一条目就不再追加（去重）
    scene_seg = scene_segment(shot, g)      # S3：【场景】段（场景表 + 本镜场景细节原文；旧剧本为空）
    first_own = "，".join([*locks, *frags, img_body, scene_seg])
    image_prompt = _join("，".join(img_kept), framing_lead, *locks, *frags, img_body, scene_seg,
                         *(t for t in corrector_terms(img_body, adult=bool(reasons), bible=b)
                           if t not in first_own and not (subject in ("part", "object") and FR.FACE_TERM_RE.search(t))),
                         *materials, boost_first, lens_term(image_style, b), image_style)

    motion_body = _no_segment_marks(_strip_triggers(shot.motion_prompt, shot.motion_triggers)) or ""
    # 声音段：画布修改 > 剧本 v3 的 H3 英文声音 > 剧本 sound 行（去掉人声）；音乐：镜头修改 > 整集 > N/A
    if soundscape:
        v_sound, v_sound_src = soundscape, "edit"
    elif getattr(shot, "sound_h3", ""):
        v_sound, v_sound_src = shot.sound_h3, "script_h3"
    else:
        v_sound, v_sound_src = script_soundscape(shot.sound), "script"
    # 配乐（项目方 2026-10-30）：英文配乐进 H3 的 non_diegetic_music（本镜人工 > 剧本 H3 自带 > 本集默认 > N/A，
    # 见 H3.resolve_music）；中文提示（剧本【配乐】/ 配乐行原文）仍只给画布/后期
    post_music = {"episode_prompt": episode_music or getattr(ep, "music", "") or "",
                  "episode_bgm": getattr(ep, "music_bgm", "") or "",
                  "shot_cue": music or getattr(shot, "music_cue", "") or ""}
    post_music["source"] = ("edit" if music or episode_music else
                            "script" if any(post_music.values()) else "none")
    ep_music_default = episode_music or getattr(ep, "music_bgm", "") or ""
    # 剧本的 motion 后缀已含画面风格（无字幕等）时不再追加 [look.video].style
    video_style = "" if "无字幕" in g.motion_suffix else b.video_style
    # H3 提示词只用剧本（**H3提示词** / 【H3 audio】/ sound 行）；画布修改只能经项目方覆盖（ShotEdit.h3_prompt_override，
    # 由 apply_edit 换上），ShotEdit.soundscape 只影响旧版拼接；配乐例外：英文 ShotEdit.music / 本集配乐直接进 H3
    h3_sound = shot.sound_h3 if getattr(shot, "sound_h3", "") else script_soundscape(shot.sound)
    h3 = H3.for_shot(shot, h3_sound,
                     names=tuple(lock_names + [c.name for c in b.characters if c.name not in lock_names]),
                     music_edit=music, episode_music=ep_music_default)
    v_music, v_music_src = H3.resolve_music(music, H3.parse_h3(shot.h3_prompt)["fields"].get(H3.FIELDS[2])
                                            if getattr(shot, "h3_prompt", "") else None, ep_music_default,
                                            getattr(shot, "music_cue", "") or "")
    h3_text = h3.text
    from . import camera_norm as CN
    # 阶段 2.1 视频模板 v2（CODER-PT 11-05，PLAN_EP01_FINISH §9）：按剧开关 run_style.json runs.<剧>.prompt_template.video；
    # v2 = H3 正文按官方写法重编（短锚点句 + 运镜独立成句（词表 h3_official，enabled 仍按 camera_norm / L-129）+ 写作方分拍），
    # 整段改动记成一条带规则号的 h3_norm（denormalize 可倒回原文）；默认 v1 = 不进此分支
    h3_norm: list[dict] = []
    h3_template = ""
    if h3_text.startswith(H3.ALIGN_PREFIX):
        from . import prompt_template_v2 as PT2
        if PT2.video_on(framing_project):
            new, rec = PT2.video_h3_text(ep, shot, g, framing_project, h3_text)
            if new != h3_text:
                h3_norm.append({"rule": "K-REN-801", "from": rec["from_body"], "to": rec["to_body"], "move": rec.get("move_id"),
                                "why": "prompt_template v2：官方写法（运镜独立成句紧跟构图句）" + (f"；运镜退回：{rec['fallback_why']}" if rec.get("fallback_why") else ""),
                                "vocab": CN.vocab().get("version"), "template": PT2.TEMPLATE_ID})
                h3_text, h3_template = new, PT2.TEMPLATE_ID
    # 项目方 2026-10-31（独白镜加嘴唇闭合）“加”：纯独白镜、人在画 → 正文第一句补官方嘴唇闭合句（剧本文字不改）
    lips_added = False
    if names and H3.monologue_only(shot) and h3_text.startswith(H3.ALIGN_PREFIX):
        new = H3.add_lips_closed(h3_text)
        lips_added, h3_text = new != h3_text, new
    # DIRECTOR 11-02（总调度批复 r2 第 1 条，CONSENT → DIRECTOR）：运镜句确定性规范化到 Pika 稳定范围（K-REN-801），改动记进 h3_norm
    if not h3_template:                                     # v2 已按词表生成运镜句（is_enabled 同一口径），不再二次规范化
        h3_text, h3_norm = CN.normalize_video_prompt(h3_text, motion_body, run=framing_project,   # 显式传本镜所属的剧（K-REN-961）
                                                     n_people=len(names) if getattr(shot, "cast", None) is not None else None)   # L-192 多人镜白名单
    legacy_video_prompt = _join("，".join(vid_kept), motion_body, g.motion_suffix, video_style, b.video_tail) \
        + soundscape_block(v_sound, v_music)

    end = FR.off_axis_gaze(end_state(motion_body))         # C-07 / W38：尾帧结束状态同上（ep01 S8 尾帧 7 版看镜头）
    # 尾帧与首帧同一套规格词（G1，2026-10-30）：触发词、纠正器条目、材质条目按首帧原文 + 结束状态匹配，
    # 规则同首帧；否则尾帧的鞋跟 / 漆皮 / 脚趾甲 / 特写 / 暗场 / 安全镜句缺失，首尾帧造型对不上
    # 去重只对照尾帧自己的文字（结束状态 + 锁 + 造型）：首帧原文写过的条目尾帧照样要补，结束状态里写过的不再加
    last_own = "，".join([end, *locks, *frags])
    last_materials = [m for m in material_terms(material_text + "，" + end, present=set(names), bible=b,
                                                already=last_own)
                      if not any(m in lk for lk in locks)
                      and (FR.lower_focus(shot.img_prompt) or FR.visible(m, frame_size))]
    last_prompt = _join("，".join(img_kept), b.last_frame_keep + end, *locks, *frags,
                        *(t for t in corrector_terms(img_body + "，" + end, adult=bool(reasons), bible=b)
                          if t not in last_own and not (subject in ("part", "object") and FR.FACE_TERM_RE.search(t))),
                        *last_materials, boost_last, lens_term(image_style, b), image_style) \
        if end or boost_last else ""

    # L-130 单一出口：上面各段（场景表字段、导演加强等）万一带了段落标记，出口再清一次——首帧 / 尾帧 / 视频提示词一律不含
    image_prompt, last_prompt = _no_segment_marks(image_prompt) or "", _no_segment_marks(last_prompt) or ""
    h3_text, legacy_video_prompt = _no_segment_marks(h3_text) or "", _no_segment_marks(legacy_video_prompt) or ""
    safe_negative = "" if reasons else b.safe_negative
    return CompiledShot(
        shot_id=shot.id, adult=bool(reasons), adult_reasons=reasons,
        image_prompt=image_prompt,
        image_negative=_negative(g.base_negative, ep.negative_extra, b.style_negative, safe_negative, bible=b),
        image_loras=img_loras,
        video_prompt=h3_text, legacy_video_prompt=legacy_video_prompt, h3_prompt_source=h3.source,
        h3_lips_closed_added=lips_added,
        h3_norm=h3_norm, h3_template=h3_template,
        h3_frames=h3.frames, h3_missing=h3.missing,
        video_negative=_negative(b.video_negative, g.base_negative, b.style_negative, bible=b),
        video_loras=vid_loras, dropped_triggers=dropped,
        last_prompt=last_prompt, end_state=end, dark=is_dark(img_body, b),
        image_body=img_body, boost_first=boost_first, boost_last=boost_last,
        video_soundscape=v_sound or "N/A", video_soundscape_source=v_sound_src, video_music=v_music,
        video_music_source=v_music_src, post_music=post_music,
        bible_version=b.version, look_fragments=frags, banned_hits=b.banned_hits(shot.img_prompt),
        video_loras_dropped=vid_dropped, image_loras_dropped=img_id_dropped, mentioned_only=mentioned, video_music_warn=H3.music_warnings(v_music),
        prompt_lint=[f"首帧：{x}" for x in lint_prompt(image_prompt, img_body, guard)] +
        ([f"尾帧：{x}" for x in lint_prompt(last_prompt, img_body + "，" + end, guard)] if last_prompt else []),
    )


def present_characters(shot: Shot, img_body: str, names: list[str], bible: Bible | None = None) -> list[str]:
    """首帧画面里出现的角色（按 names 顺序）。规则：[characters.*].detect 命中；地点所有格的主人又以“他”出现；
    写“她”且 motion 带 detect_she_with_trigger（林晚 = lnwan01）。
    R3（production_rules_v3 §2.5 / 第五部分 4.1）：剧本写了 `**人物**` 行时直接用它（在画 + 局部），不再按关键词猜"""
    cast = getattr(shot, "cast", None)
    if cast is not None:
        vis = {c.name for c in cast if c.visible}
        return [n for n in names if n in vis]
    b = bible or load_bible()
    people_text, possessive, objects = without_possessive_mentions(img_body, b)   # FIX P0 ⑦：物件所有格只是提到
    possessive = possessive + objects    # 所有格的主人又以“他”出现在画面里 → 在画（同地点所有格规则）
    rules = {c.name: c for c in b.characters}

    def present(name: str) -> bool:
        cr = rules.get(name)
        if any(h in people_text for h in (cr.detect if cr else (name,))):
            return True
        # “顾沉家卧室，他……”：地点所有格的主人又以“他/她”出现在画面里（“她”角色走下面的触发词规则）
        if name in possessive and not (cr and cr.she_trigger) and "他" in people_text.replace("其他", ""):
            return True
        return bool(cr and cr.she_trigger and cr.she_trigger in shot.motion_triggers and "她" in img_body)
    return [n for n in names if present(n)]
