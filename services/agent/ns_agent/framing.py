"""出图提示词按景别取舍造型部件（PROMPT 2026-10-31，knowledge K-REN-101）。

问题（方案 C 预览 S1 实测）：导演稿镜1 是“手部特写起手 → 上摇到侧脸近景”，出图提示词却把整套造型（12cm 透明细跟、漆皮短裙、
长腿）都拼了进去，Qwen-Image 2.1 出成了全身坐姿（data/pipeline/runs/ep02_claude_director_v1/preview/_wrong_framing/S1_1_fullbody.png）。
规则：出图提示词只写该景别下画面里看得见的造型部件；首句先写景别与构图。
- 特写 / 大特写 / 极近特写：只留头脸、手、上身领口一带；去掉下装、腿、鞋、脚
- 近景 / 中近景：去掉下装、腿、鞋、脚
- 中景：去掉鞋、脚（膝上构图）
- 中全景及更远：全套
只做格式取舍（删掉看不见的部件描述），不改写作方的 img 原文。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

SIZES = ("极近特写", "大特写", "特写", "中近景", "近景", "中景", "中全景", "全身", "全景", "远景", "大远景")
LOWER = ("裙", "裤", "腿", "膝", "臀", "鞋", "高跟", "细跟", "鞋跟", "袜", "脚", "足弓", "赤足", "趾",
         "skirt", "miniskirt", "trouser", "pants", "leg", "heel", "stiletto", "shoe", "sneaker", "boot", "sock", "feet", "foot", "toe")
FEET = ("鞋", "高跟", "细跟", "鞋跟", "袜", "脚", "足弓", "赤足", "趾", "heel", "stiletto", "shoe", "sneaker", "boot", "sock", "feet", "foot", "toe")
HIDE = {"极近特写": LOWER, "大特写": LOWER, "特写": LOWER, "中近景": LOWER, "近景": LOWER, "中景": FEET}
LEAD = {"极近特写": "极近特写构图", "大特写": "大特写构图", "特写": "特写构图", "中近景": "中近景构图（胸部以上）",
        "近景": "近景构图（胸部以上）", "中景": "中景构图（膝盖以上）", "中全景": "中全景构图", "全身": "全身构图",
        "全景": "全景构图", "远景": "远景构图", "大远景": "大远景构图"}
CFG = Path(__file__).resolve().parents[3] / "config" / "recipes" / "image_framing.json"


REPO = Path(__file__).resolve().parents[3]


def _cfg() -> dict:
    try:
        return json.loads(CFG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _locked_image(root: Path, episode: int, index: int) -> bool:
    """该镜首帧已定稿（data/renders/epNN/SN/takes.json locked.image 有值）→ 不改它的出图提示词，免得触发重出"""
    from . import run_paths as RP                         # I-14
    p = RP.renders_dir(Path(root)) / f"ep{episode:02d}" / f"S{index}" / "takes.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool((d.get("locked") or {}).get("image")) if isinstance(d, dict) else False


def enabled(episode: int | None = None, index: int | None = None, root: str | Path | None = None,
            project: str | None = None) -> bool:
    """config/recipes/image_framing.json（总调度 2026-10-31 定）：
    default（新剧 / 新集默认开）→ per_project[<项目>]：off_episodes 整集关（ep02：方案 A/B 正用已定稿首帧跑片）；
    keep_locked_shots：已定稿首帧的镜保持旧提示词。项目键：默认项目（本仓库根）= "__default__"，其它 = 项目根目录名。
    旧格式 {"enabled": bool} 仍认。"""
    c = _cfg()
    if "default" not in c:
        return bool(c.get("enabled"))
    on = bool(c.get("default"))
    r = Path(root or os.environ.get("NS_PROJECT_ROOT") or REPO)
    key = project or ("__default__" if r.resolve() == REPO.resolve() else r.name)
    pc = (c.get("per_project") or {}).get(key) or {}
    if project:                                  # 显式项目键（流水线新剧）：没有本仓库的 renders，不查已定稿镜
        pc = {k: v for k, v in pc.items() if k != "keep_locked_shots"}
    if "default" in pc:
        on = bool(pc["default"])
    if episode is not None and episode in (pc.get("off_episodes") or []):
        return False
    if episode is not None and episode in (pc.get("on_episodes") or []):
        on = True
    if on and pc.get("keep_locked_shots") and episode is not None and index is not None and _locked_image(r, episode, index):
        return False
    return on


def size_of(text: str) -> str | None:
    """img 原文里第一个景别词（规则 §6A：紧跟触发词写，系统取第一个）"""
    best = None
    for s in SIZES:
        i = (text or "").find(s)
        if i >= 0 and (best is None or i < best[0] or (i == best[0] and len(s) > len(best[1]))):
            best = (i, s)
    return best[1] if best else None


def hidden_words(size: str | None) -> tuple[str, ...]:
    return HIDE.get(size or "", ())


def visible(fragment: str, size: str | None) -> bool:
    """这段造型 / 规格描述在该景别下看得见吗（含任一隐藏部件词 = 看不见）"""
    low = (fragment or "").lower()
    for w in hidden_words(size):
        if w.isascii():
            if re.search(rf"\b{w}s?\b", low):        # 英文按词边界（leg 不匹配 elegant）
                return False
        elif w in low:
            return False
    return True


# ISS-1103-024205-8856（MERGE-IMG-O 11-03，L-137）：近景 / 特写里“看得见的上身件”不能因为同一件衣服名里带“裙 / 袜”被整句删掉。
# 例：V2 ep01-S8 近景“祁念穿黑色漆皮抹胸紧身短裙、黑色薄透丝袜”——抹胸（上身）在胸部以上构图里看得见，K-REN-707 原先整句删，
# 首帧提示词一个服装词都没有。规则：分句里有上身件词 → 这一句按单品拆开（+ / 配 / 和 / 与 / 及），只删看不见的单品，保留上身件。
# 强上身词：出现即算上身入画（抹胸裙 / 吊带裙 / 连衣裙的上身在胸像里看得见）；弱上身词：后面紧跟 裙 / 裤 / 袜 时是下装（西装裙、针织裤）
UPPER_STRONG = ("抹胸", "吊带", "连衣", "礼服", "旗袍", "胸衣", "文胸", "上衣", "衬衫", "背心", "外套", "夹克", "毛衣", "T恤", "卫衣",
                "风衣", "大衣", "马甲", "领口", "衣领", "开衫", "披肩", "斗篷", "浴袍", "睡袍")
UPPER_WEAK = ("西装", "针织", "皮衣")
NOT_UPPER = ("吊带袜", "连裤袜", "连体袜")
ITEM_SPLIT_RE = re.compile(r"(\s*(?:\+|＋|搭配|配|和|与|及)\s*)")
WEAR_PREFIX_RE = re.compile(r"^(.*?(?:穿着|穿|换上|换成|外罩))")


def keep_upper_enabled() -> bool:
    """config/recipes/image_framing.json close_keep_upper（缺省 true；false = 回退 K-REN-707 旧行为：带裙 / 袜字的整句删）"""
    return bool(_cfg().get("close_keep_upper", True))


def upper_garment(fragment: str) -> bool:
    """分句里有看得见的上身件（抹胸 / 衬衫 / 外套 …）"""
    t = fragment or ""
    for w in NOT_UPPER:
        t = t.replace(w, "")
    if any(w in t for w in UPPER_STRONG):
        return True
    return any(re.search(re.escape(w) + r"(?![裙裤袜])", t) for w in UPPER_WEAK)


def visible_items(fragment: str, size: str | None) -> str:
    """把一句并列造型按单品拆开，只留该景别看得见的单品与上身件（保留“穿 / 换上”前缀）；一件都不剩 → ""。
    “黑色漆皮抹胸紧身短裙 + 黑色薄透丝袜 + 透明尖头细高跟”近景 → “黑色漆皮抹胸紧身短裙”"""
    toks = ITEM_SPLIT_RE.split(fragment or "")
    items, seps = toks[0::2], [""] + toks[1::2]
    out, prefix = "", ""
    for i, (it, sep) in enumerate(zip(items, seps)):
        if not it.strip():
            continue
        if visible(it, size) or upper_garment(it):
            if not out and prefix and not WEAR_PREFIX_RE.match(it):
                it = prefix + it.lstrip()
            out += (sep if out else "") + it
        elif i == 0:
            m = WEAR_PREFIX_RE.match(it)
            prefix = m.group(1) if m else ""
    return out


def visible_garment(fragment: str, size: str | None) -> bool:
    """filter_parts 的保留判定：看得见，或（开关开时）含看得见的上身件"""
    return visible(fragment, size) or (keep_upper_enabled() and upper_garment(fragment))


def keep_subject_enabled() -> bool:
    """K-REN-901（IMGLAB 11-02，ep06-S2 v6 顾沉长出林晚的蓝色长发）：删句保主语开关。
    config/recipes/image_framing.json keep_subject（缺省 true；false = 回退旧行为：整句删，主语跟着丢）"""
    return bool(_cfg().get("keep_subject", True))


GAZE_CLAUSE_RE = re.compile(r"(?:视线|目光)(?:落在|停在|移向|转向|看向|投向|锁在|从)")
GENERIC_CLAUSE = ("真实皮肤质感", "可见细小毛孔", "自然的轻微不对称", "眼睛为真实人眼大小", "细软绒毛")   # 写作方通用质感句，不需要主语


def _keep_with_subject(parts: list[str], keep_pred, subjects=()) -> list[str]:
    """按 keep_pred 留分句；被删的分句里有在画角色名 → 名字作为单独一句挪到下一个保留分句前面（K-REN-901）。
    下一个保留分句本身以人名开头（主语已在）就不补；被删句之后再没有保留句就不补。
    没有删掉带名字的分句时，结果与旧写法逐字相同（不改未受影响镜头的提示词，L-087）。"""
    names = [n for n in (subjects or ()) if n] if keep_subject_enabled() else []
    out, pending = [], None
    for p in parts:
        if names and GAZE_CLAUSE_RE.search(p) and not keep_pred(p):
            keep_this = True       # “林晚的视线落在椅背的裙子上”：视线落点里的部件词不是画面可见部件，视线句不删（ep04-S6 / ep09-S7）
        else:
            keep_this = keep_pred(p)
        if keep_this:
            if pending and not any(p.lstrip().startswith(n) for n in names) and not any(w in p for w in GENERIC_CLAUSE):
                out.append(pending)
            pending = None
            out.append(p)
        elif names:
            # 只认主语位置的名字（分句以人名开头）：“站在林晚侧后方半步”里的林晚是宾语，主语仍是前面的人（ep08-S2）
            lead = next((n for n in names if p.lstrip().startswith(n)), None)
            if lead:
                pending = lead
    return out


def filter_parts(text: str, size: str | None, sep: str = "，", subjects=()) -> str:
    """按分隔符切开，去掉看不见的部件分句（用于角色锁 / 造型段这类并列描述）。
    subjects：在画角色名（用于正文；被删分句带走的主语按 K-REN-901 补回）"""
    if not hidden_words(size):
        return text
    m = re.match(r"^(【[^】]*】)", text or "")          # “【林晚造型】超短裙，…”：标签头保留
    head = m.group(1) if m else ""
    body = (text or "")[len(head):]
    parts = [p for p in re.split(r"[，,、；;]", body) if p.strip()]
    if keep_upper_enabled():      # ISS-1103-024205-8856：含上身件的分句只删其中看不见的单品
        parts = [(visible_items(p, size) or p) if (not visible(p, size) and upper_garment(p)) else p for p in parts]
    keep = _keep_with_subject(parts, lambda p: visible_garment(p, size), subjects)
    return head + sep.join(keep) if keep else ""


FOCUS_TRIGGERS = ("hh12", "ptxheel", "feet_", "foot_")   # 鞋 / 脚 LoRA 触发词：画面主体就是下半身
# 景别词后面紧跟的机位分句（“低机位”“平视”）不是画面主体，判主体时跳过（K-REN-123）
CAMERA = ("机位", "平视", "俯拍", "仰拍", "俯视", "仰视", "低角度", "高角度", "侧拍", "跟拍", "固定镜头", "手持", "过肩")
FACE_WORDS = ("脸", "眼", "唇", "嘴", "眉", "视线", "表情", "目光", "睫", "鼻", "下巴", "面部", "神情", "笑", "额")
PART_WORDS = ("手", "指", "甲", "腰", "腹", "锁骨", "肩", "背", "颈", "腿", "膝", "脚", "足", "鞋", "跟", "踝", "臀", "胸")
# 脸部特写里会把画面拉成全身 / 半身的分句（K-REN-123：特写常退成中景，原因之一是正文同时写了站姿 / 全身镜）
FULLBODY = ("全身", "站定", "站立", "站在", "站着", "侧身站", "跪坐", "跪在", "坐在", "坐着", "躺在", "走向", "走进", "迈步",
            "从头到脚", "整个人")
CLOSE = ("极近特写", "大特写", "特写")


def content_clauses(text: str) -> list[str]:
    """景别词之后的分句，去掉机位分句"""
    t = text or ""
    size = size_of(t)
    rest = t[t.find(size) + len(size):] if size else t
    return [p for p in re.split(r"[，,；;。]", rest) if p.strip() and not any(c in p for c in CAMERA)]


CLOSE_NO_LOWER = ("极近特写", "大特写", "特写", "中近景", "近景")


def close_no_lower() -> bool:
    """K-REN-707（IMGFIX 11-02，总调度 P1）：特写 / 近景 / 中近景不注入下半身描述（角色锁“长腿”、造型补句“露出长腿 / 裙 / 鞋”），
    并修 lower_focus 把“穿 A、B 裙与 C 外套”整句当成下半身主体的误判。开关 config/recipes/image_framing.json close_no_lower
    （缺省 false = 旧行为；总调度看过期数字后决定上线时间）"""
    return bool(_cfg().get("close_no_lower", False))


def lower_focus(img_raw: str, kept_triggers=None) -> bool:
    """img 原文（含触发词）以下半身为主体 → 不按景别删下半身部件（例：hh12clear 极近特写 = 鞋的特写）。
    判定：有鞋 / 脚触发词（kept_triggers 给出时，只认编译器真正保留的触发词——LoRA 不可用被剥掉的触发词不算证据，
    K-REN-123：写作方习惯性带 hh12clear，导致特写不删高跟 / 短裙，出图退成中景），或景别词后第一个非机位分句就写下半身部件"""
    t = img_raw or ""
    size = size_of(t)
    if size and closeup_subject(t, size) == "face":   # 特写正文写了脸 / 视线 → 脸部特写：鞋触发词也不改主体（REFLOCK 11-01：
        return False                                  # 以前触发词先判 True，资产规划与编译器对 ep02-S1 的主体判断不一致）
    trig = [k for k in FOCUS_TRIGGERS if k in t.lower()]
    if trig and (kept_triggers is None or any(k in str(x).lower() for x in kept_triggers for k in trig)):
        return True
    if not size:
        return False
    first = next(iter(content_clauses(t)), "")
    if close_no_lower():
        # 首句是并列造型（“林晚穿米白衬衫、及膝铅笔裙与短款西装外套”）时，只有每一项都看不见才算下半身主体
        # “穿 / 换上 …”是造型描述，不是画面主体（ep02-S9“林晚穿雾蓝针织长裙”、ep08-S9“…束腰配…短裙”）
        # 只有整段正文都只写下半身（“极近特写，林晚穿黑色漆皮超短裙”）才算下半身主体；后面还写了手 / 脸 / 动作的不算
        if re.search(r"穿|换上|换成", first):
            return all(not visible(c, size) for c in content_clauses(t))
        items = [x for x in re.split(r"[、与和及配]", first) if x.strip()]
        return bool(items) and all(not visible(x, size) for x in items)
    return not visible(first, size)


# W-PROD 11-07（ep03-S4 K-REN-708 误报）：“X 的视线从画外…落在…”= 看的人在画外（插入镜只露手），不代表画面里有脸；
# rubric specificity#1 又要求每镜 img 写“视线”，两条规则只能靠这句话同时满足 → 判脸前去掉这类分句
OFFSCREEN_GAZE = re.compile(r"[^，,；;。]*(?:视线|目光)从画外[^，,；;。]*")


def strip_offscreen_gaze(text: str) -> str:
    return OFFSCREEN_GAZE.sub("", text or "")


def closeup_subject(img_body: str, size: str | None) -> str | None:
    """特写类镜头的主体：face（写了脸 / 眼 / 视线…）、part（只写手 / 腰 / 鞋…等局部）、object（都没写）；非特写 → None"""
    if size not in CLOSE:
        return None
    t = strip_offscreen_gaze(img_body).replace("眼睛为真实人眼大小", "")      # 写作方的通用质感句 / 画外视线，不代表画面里有脸
    if any(w in t for w in FACE_WORDS):
        return "face"
    if any(w in t for w in PART_WORDS):
        return "part"
    return "object"


HAND_WORDS = ("手", "指", "甲", "腕")
NECK_WORDS = ("锁骨", "颈", "肩", "胸")


def part_name(img_body: str) -> str:
    """局部特写的局部名：正文里最先写到的局部词。
    W5（总调度 11-01，ep05-S3/S5 手部大特写 8 次出成带肩颈 / 脸的近景）：局部是手、而动作句同时写到肩 / 颈 / 锁骨 / 胸口
    （例：手套按在林晚肩头、记号笔点在胸口）→ 局部范围扩大为“手与肩颈”，构图句与之一致，不再写“只有手”和动作句打架"""
    t = img_body or ""
    hits = sorted(((t.find(w), w) for w in PART_WORDS if w in t))
    first = hits[0][1] if hits else "局部"
    if first in HAND_WORDS and any(w in t for w in NECK_WORDS):
        return "手与肩颈"
    return first


def strip_fullbody(text: str, subjects=()) -> str:
    """去掉写站姿 / 全身的分句（只用于脸部特写）。subjects：在画角色名，被删分句里的主语按 K-REN-901 补回"""
    parts = [p for p in re.split(r"[，,；;]", text or "") if p.strip()]
    keep = _keep_with_subject(parts, lambda p: not any(w in p for w in FULLBODY), subjects)
    return "，".join(keep)


def partial_only_enabled() -> bool:
    """K-REN-903（IMGLAB 11-02，ep05-S3/S6/S7 82 版出整张脸）：在画的人全是局部（cast 局部、不露脸）时按局部特写编译。
    config/recipes/image_framing.json partial_only（缺省 true；false = 旧行为）"""
    return bool(_cfg().get("partial_only", True))


def partial_only(shot, names: list[str]) -> bool:
    """露脸的人 0 个、局部的人 ≥1 个（cast 判定，同 faced_people）"""
    if not partial_only_enabled():
        return False
    faced, partial = faced_people(shot, names)
    return not faced and bool(partial)


def narrow_lock(lock: str, part: str) -> str:
    """K-REN-903：局部人物的角色锁收窄——成年句只留“成年女性 / 成年男性”（HC-07 仍在，去掉年龄数字与国籍），
    发色 / 刘海句只在局部含“发 / 头”时保留（W5 b 改写：肩颈局部不再带发色句，发色句会把画面拉回人像）"""
    m = re.match(r"(【[^】]+】)(.*)", lock or "", re.S)
    if not m:
        return lock
    out = []
    for c in (x for x in re.split(r"[，,；;、]", m.group(2)) if x.strip()):
        if "成年" in c:
            a = re.search(r"成年(?:女性|男性|人)", c)
            out.append(a.group(0) if a else "成年")
        elif (HAIR_COLOR_RE.search(c) or "刘海" in c) and not any(w in (part or "") for w in ("发", "头")):
            continue
        else:
            out.append(c)
    return (m.group(1) + "，".join(dict.fromkeys(out))) if out else ""


PARTIAL_TOP = "，画面上沿在下颌以下（frame top below the chin）"


def closeup_lead(size: str | None, subject: str | None, img_body: str = "", partial: bool = False) -> str:
    """K-REN-123：特写首句加构图约束（中英各一句，Qwen-Image 2.1 对英文构图词更敏感）。
    partial=True（K-REN-903 全员局部）：非特写景别也用局部写法，并写明画面上沿在下颌以下"""
    if not subject or (size not in CLOSE and not (partial and subject == "part")):
        return lead(size)
    if subject == "face":
        if size == "特写":
            return ("特写构图：只拍头部与肩颈，脸部占画面约六成，头顶到下巴完整入画，胸口以下不入画"
                    "（tight close-up portrait, the face fills about 60% of the frame）")
        return ("大特写构图：脸部充满画面，只见眉眼到下巴（extreme close-up, the face fills the whole frame）")
    part = part_name(img_body) if subject == "part" else "物件"
    # 肯定句写法（BAKEOFF R11-12 A3：cfg=1 下否定句不生效，“不出现脸”反而把“脸”写进了提示词）
    return (f"{size}构图：画面里只有{part}这一局部，{part}占画面大半，画框紧贴{part}裁切"
            f"（tight close-up crop on the {PART_EN.get(part, 'detail')} only, filling the frame）") + (PARTIAL_TOP if partial else "")


def two_face_close_mode() -> str:
    """K-REN-902（IMGLAB 11-02）：脸部特写 + ≥2 人露脸 = 构图自相矛盾（“脸占六成”与“两张脸都清晰入画”在 9:16 里不能同时成立，
    ep06-S2 v6 退成双人中景）。config/recipes/image_framing.json two_face_close：
    "two_shot_mcu"（缺省）= 首句改成双人近景构图（两张脸各约三成，景别档差 1 仍在 Q3 容差内）；"off" = 旧行为"""
    v = str(_cfg().get("two_face_close", "two_shot_mcu"))
    return v if v in ("two_shot_mcu", "off") else "two_shot_mcu"


def two_face_close_conflict(size: str | None, subject: str | None, n_faced: int) -> bool:
    """写作 lint / preflight 用：特写类 + 脸部主体 + 露脸人数 ≥2（K-REN-902）"""
    return size in CLOSE and subject == "face" and n_faced >= 2


TWO_FACE_CLOSE_LEAD = ("双人近景构图：两人头部与肩颈入画，两张脸各占画面约三成，胸口以下不入画"
                       "（two-shot medium close-up, both heads and shoulders in frame, each face about a third of the frame height）")
CLOSEUP_FACE_LEAD_MARK = "脸部占画面约六成"
TWO_FACES_MARK = "两张脸都清晰入画"


def framing_clash(prompt: str) -> bool:
    """preflight I6（K-REN-902）：同一提示词里既写“脸占六成”又写“两张脸都清晰入画”= 构图矛盾"""
    p = prompt or ""
    return CLOSEUP_FACE_LEAD_MARK in p and TWO_FACES_MARK in p


PART_EN = {"手与肩颈": "hands and the shoulder-collarbone area", "手": "hands", "脚": "feet", "鞋": "shoes", "腰": "waist", "腿": "legs", "胸": "chest", "肩": "shoulder",
           "锁骨": "collarbone", "物件": "object"}


# 局部特写：角色锁只留与局部相关的分句（REFLOCK 11-01）；纠正器里写脸的条目不加
PART_LOCK_WORDS = {"手": ("手", "指", "甲", "腕", "戒", "钻"), "脚": ("脚", "足", "趾", "鞋", "踝"), "鞋": ("脚", "足", "趾", "鞋", "踝", "跟"),
                   "腰": ("腰", "束腰"), "腿": ("腿",), "胸": ("胸",), "肩": ("肩",), "锁骨": ("锁骨",)}
FACE_TERM_RE = re.compile(r"面部|五官|脸")
HAIR_COLOR_RE = re.compile(r"发色|(?:蓝|冰蓝|黑|金|棕|栗|红|银|白|粉|紫|绿|灰|亚麻)[^，,；;]{0,3}发")


def part_lock(lock: str, part: str) -> str:
    """【名】描述 → 只留与 part 相关的分句；没有就返回空串（整条不放）"""
    m = re.match(r"(【[^】]+】)(.*)", lock or "", re.S)
    if not m:
        return ""
    part = part or ""
    if part == "手与肩颈":
        words = PART_LOCK_WORDS["手"] + PART_LOCK_WORDS["肩"] + PART_LOCK_WORDS["锁骨"] + PART_LOCK_WORDS["胸"]
    else:
        words = next((v for k, v in PART_LOCK_WORDS.items() if k in part), ("甲",))
    # IMGFIX 11-02（K-REN-705 b）：人物卡用顿号串外貌（“蓝发及腰、齐刘海、冷白皮肤”），按顿号也切开，发色句不再连带刘海 / 肤色
    cl = [c for c in re.split(r"[，,；;、]", m.group(2)) if c.strip()]
    keep = [c for c in cl if any(w in c for w in words)]
    # W5 b（总调度 11-01）：局部范围到了肩 / 颈 / 锁骨 / 胸（头发会入画）→ 保留发色句；
    # ep05-S3/S5 去掉发色句后林晚的发梢被画成黑色。纯手 / 脚局部头发不入画，不加（加了会把构图拉回人像）
    if any(w in part for w in NECK_WORDS + ("背",)):
        keep += [c for c in cl if HAIR_COLOR_RE.search(c) and c not in keep]
    adult = [c for c in cl if "成年" in c and c not in keep]   # 成年人锁定（HC-07）永远保留（局部里没有这个人的部件时也留年龄句）
    return (m.group(1) + "，".join(adult + keep)) if keep or adult else ""


def faced_people(shot, names: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """K-REN-705：→（露脸的人, [(局部的人, 部位原文)]）。露脸 = cast 在画，或局部但 img 原文写到这个人的脸 / 头发
    （asset_refs.face_described，与挂身份脸同一判断）。没有 cast 行的旧剧本：names 全算露脸"""
    cast = getattr(shot, "cast", None) if not isinstance(shot, dict) else shot.get("cast")
    if cast is None:
        return list(names), []
    from ns_agent import asset_refs as _AR
    img = (getattr(shot, "img_prompt", None) if not isinstance(shot, dict) else shot.get("img_prompt")) or ""
    gv = (lambda c, k: c.get(k) if isinstance(c, dict) else getattr(c, k, None))
    faced, partial = [], []
    for c in cast:
        n, pr = gv(c, "name"), gv(c, "presence")
        if n not in names:
            continue
        if pr == "在画" and _AR.back_view(shot, n):        # K-REN-957：在画但本人分句写背影 → 不算露脸（按背影局部处理）
            partial.append((n, "背影"))
        elif pr == "局部" and _AR.back_part(shot, n):      # L-204 ①：过肩前景 / 背影的局部人物不露脸（正文写到头发也不算）
            partial.append((n, (gv(c, "part") or "").strip() or "背影"))
        elif pr == "在画" or (pr == "局部" and _AR.partial_face_visible(n, img, [gv(x, "name") for x in cast if gv(x, "presence") in ("在画", "局部")])):
            faced.append(n)
        elif (pr == "局部" and not _AR.back_part(shot, n) and _AR.pf_on("face_exposure_guard") and not _AR.crop_guaranteed(shot)
              and _AR.face_exposure_conflict(*_AR.visible_zones(img)[:2])):
            faced.append(n)                               # K-REN-950：和 resolve_shot 同口径（送了脸就不能再写“不露脸”）
        elif pr == "局部":
            partial.append((n, (gv(c, "part") or "").strip()))
    return faced, partial


PARTIAL_CLAUSE = "{name}只露出{part}，不露脸"
PARTIAL_RE = re.compile(r"[^，。、；]{1,12}只露出[^，。]{1,30}，不露脸")


def partial_clause(partial: list[tuple[str, str]], subject: str | None, extra: dict | None = None) -> str:
    """局部的人（剧本 cast：局部｜部位）：写清只露出该部位、不露脸；部位没写就只写“不露脸”。局部 / 物件特写不加（构图句已限定局部）。
    extra：{名字: 附加句}（背影局部人物“背面也穿着…”，asset_refs.back_outer_clause）"""
    if subject in ("part", "object"):
        return ""
    out = [(PARTIAL_CLAUSE.format(name=n, part=p) if p else f"{n}不露脸") + (f"，{(extra or {})[n]}" if (extra or {}).get(n) else "")
           for n, p in partial]
    return "，".join(out)


# L-200 / FIX-PRE 11-06（REVIEW-PRE E）：视线动词再加 钉在 / 停在 / 投向 / 移向 / 转向 / 锁在 / 盯着；没有“视线”主语的
# “抬眼看向画面右前方”“看向画面正前方”也改（只认看 / 望 / 盯类动词，“手停在画面右前方”不动）
_FRONT_GAZE = re.compile(r"((?:视线|目光|眼神|眼睛)[^，。；]{0,3}(?:落在|看向|望向|朝向|看着|望着|盯着|钉在|停在|投向|移向|转向|锁在)"
                         r"|(?:抬眼|垂眼|回头|转头|侧头)?(?:看向|望向|看着|望着|盯着|瞥向))画面(右|左|正)?前方")


def off_axis_gaze(text: str) -> str:
    """C-07 / W38（11-04，ep01 S8 首尾帧 10 版里 7 版真看镜头）：静帧里“视线落在画面右前方 / 左前方 / 正前方”≈ 朝镜头（本剧环形灯架
    就在相机位）→ 改成“画面右侧 / 左侧远处（与镜头方向错开）”；只改视线落点这一处，落点后面的物件原文保留"""
    def _sub(m):
        side = {"右": "右", "左": "左"}.get(m.group(2) or "", "右")
        return f"{m.group(1)}画面{side}侧远处（与镜头方向错开）"
    return _FRONT_GAZE.sub(_sub, text or "")


def has_own_gaze(body: str, name: str) -> bool:
    """剧本 img 原文给这个人写了视线落点（“X的视线落在 / 目光落在 / 眼睛看着 / 看向 …”）"""
    return bool(re.search(re.escape(name) + r"[^，。；]{0,6}(视线|目光|眼睛|眼神)[^，。；]{0,12}(落在|看着|看向|盯着|望向|朝向)", body or ""))


def two_shot_clause(n_people: int, subject: str | None, body: str | None = None, names: list | None = None) -> str:
    """K-REV-122 / 总调度第二轮：双人镜第二个人常不出脸 → 写清两人朝向（局部特写不加）。
    SCRIPT-RSI 11-01（K-REN-703 / D-SCR-07，项目方 18:2x“所有图片不要看镜头”）：原句“各自至少四分之三侧脸朝向镜头（… toward the camera）”
    让两人都对镜头（VIDEO-RCA：首帧看镜头 → 片头看镜头 49/54）。改成正向视线落点；Qwen Image cfg 1 负面不生效，不写“不看镜头”。
    视线具体落在谁 / 什么上由剧本 img 写（QW-A7），这里只给朝向和“有落点”。
    ⤴ VIDEO-RCA 11-01 19:2x（总调度 r2 决定：按 G0 有实测证据的优先，“只写正向句”在此按证据例外）：换成实测句
    “互相看着对方……眼睛不看镜头”——同参考同 seed 2 个 seed，4 张脸看镜头 3 → 0（data/ab/video_rca/r1_img_gaze/faces.jpg，
    K-REN-703）。上面 SCRIPT-RSI 的正向句（TWO_SHOT_POSITIVE）还没实测；r2 做 3-seed A/B，若同样有效就改回正向句。
    ⤴ VIDEO-RCA r2（11-01 20:5x）A/B 完成（data/ab/video_rca/r5_img_twoshot/faces_01S5.jpg、faces_03S4.jpg，ep01-S5 / ep03-S4 × 3 seed，人工看脸）：
    旧句看镜头 8/10 张脸；正向句 0–1/10（1 张存疑）；实测句 0/12 → 两句同样有效，按总调度决定改回正向句（K-REN-703）。
    ⤴ VIDEO-RCA r3（11-01 22:5x，总调度亲审）：生产首批反例 ep01-S5 v5（data/renders/ep01/S5/takes/ 第 5 版，正向句）林晚、顾沉都正对镜头
    → 按 G0 有实测证据的优先，切回实测句 TWO_SHOT_TESTED（A/B 0/12）。"""
    if n_people < 2 or subject in ("part", "object"):
        return ""
    # W38 / 总调度 11-04 03:3x 定 B：剧本已给每个露脸的人写了视线落点 → 不再加“互相看着对方”（与剧本“卓琳看刻度、祁念看灯架”
    # 自相矛盾，ep01 S4 Q10 每版 action 不符）；保留实测有效的侧脸朝向 + 不看镜头（防对镜，K-REN-703），视线交给剧本原文
    if body and names and len(names) >= 2 and all(has_own_gaze(body, n) for n in names):
        return TWO_SHOT_OWN_GAZE
    return TWO_SHOT_TESTED


TWO_SHOT_TESTED = ("两人同框，两张脸都清晰入画，两人以侧脸到四分之三侧脸互相看着对方，目光落在对方脸上，眼睛不看镜头"
                   "（both faces visible in profile to three-quarter view, each looking at the other person, eyes off the lens）")
TWO_SHOT_OWN_GAZE = ("两人同框，两张脸都清晰入画，两人以侧脸到四分之三侧脸入画，各自的视线落在下面写的落点上，眼睛不看镜头"
                     "（both faces visible in profile to three-quarter view, each gaze on its own target described below, eyes off the lens）")
TWO_SHOT_POSITIVE = ("两人同框，两张脸都清晰入画，侧脸到四分之三侧脸，各自的视线落在画面里的人或物上"
                     "（both faces visible in profile to three-quarter view, each gaze resting on a person or object in the scene）")


def lead(size: str | None) -> str:
    return LEAD.get(size or "", "")
