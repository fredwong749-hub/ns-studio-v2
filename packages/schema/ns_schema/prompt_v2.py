"""提示词模板 v2 的纯文本核心（阶段 2.1，PLAN_EP01_FINISH §4 2.1 / §8 ①–⑤ / §9；CODER-PT 11-05）。

只依赖标准库；出图侧由 services/agent/ns_agent/prompt_template_v2.py 调，视频侧由 ns_agent.compiler（画布 / 单镜）与
ns_recipes.prompt.build_base（生产 vidgen）两条路各自适配后调这里，保证两条路编出同一句。
出图（Qwen-Image 2.1，§8）：
  ① 首句 = 景别｜机位方位｜机位高度｜焦段光圈｜焦点人物与焦外层次（中英各一句），放参考槽标注之后、人物之前；
  ② 不写【角色】身份长句（脸 / 发交给参考图），人物一行：名（服装、发、妆）+ 站位 + 动作 + 视线；
  ③ 场景块 = 本镜陈设 + 一句布光（动机光源 / 负补光 / 色温 / 人物比背景亮几档），不写幻想段 / 现实段双段原文；
  ④ 否定句改正向状态，关键否定进负面词（只对 cfg>1 生效 = base35 档）；
  ⑤ 同框句按在画露脸人数生成（三人镜不再写“两人同框”）。
视频（MiniMax H3，§9 官方写法）：短结构化正文；运镜独立成句紧跟构图句（官方动词 + 幅度 + 速度）；分拍沿用写作方 L-141 时间轴；
推 / 拉幅度词降一档（基准实测 1.2–2.8× 目标）；bolt 用官方句；横移 / 环绕只作 disabled 候选（方向反写、中档幅度）。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- 景别 / 机位
SIZE_EN = {"大特写": "an extreme close-up", "极近特写": "an extreme close-up", "特写": "a close-up", "近景": "a medium close-up",
           "中近景": "a medium close-up", "中景": "a medium shot", "中全景": "a medium full shot", "全身": "a full shot",
           "全景": "a wide shot", "远景": "a wide shot", "大远景": "an extreme wide shot"}
SIZE_NOTE_ZH = {"中近景": "胸部以上", "近景": "胸部以上", "中景": "膝盖以上", "中全景": "大腿以上、带少量环境", "全景": "全身与环境"}
ZH_NUM = {1: "一", 2: "两", 3: "三", 4: "四", 5: "五", 6: "六"}
X_EN = {"左": "frame left", "中": "frame centre", "中偏左": "frame centre-left", "中偏右": "frame centre-right", "右": "frame right"}
DEPTH_EN = {"前": "in the foreground", "中": "in the midground", "后": "in the background"}
X_ZH = {"左": "画面左侧", "中": "画面中央", "右": "画面右侧", "中偏左": "画面中央偏左", "中偏右": "画面中央偏右"}
DEPTH_ZH = {"前": "前景", "后": "后景"}

_PAREN_RE = re.compile(r"（[^）]*）|\([^)]*\)")


def strip_parens(t: str) -> str:
    return _PAREN_RE.sub("", t or "").strip()


def split_field(text: str) -> list[str]:
    """“A｜B｜C” → [A, B, C]（去空）"""
    return [p.strip() for p in re.split(r"[｜|]", text or "") if p.strip()]


_END_MARK_RE = re.compile(r"落幅\s*[：:=＝]")
_START_MARK_RE = re.compile(r"^\s*起幅\s*[：:=＝]\s*")


def start_end(seg: str) -> tuple[str, str]:
    """W-PASS 11-07：**机位与景别** 第三段写成“起幅：A；落幅：B”（L-203 分镜，ep01 psm11 / ep02 psm7 全部 16 镜）→ (A, B)。
    首帧只该画起幅（ep02-S8 v1 / v3 把起幅 + 落幅画成上下两格拼图、v2 一亮一暗两部手机）；没写 落幅 标记 → (原文, "")"""
    seg = seg or ""
    m = _END_MARK_RE.search(seg)
    if not m:
        return seg, ""
    start = _START_MARK_RE.sub("", seg[:m.start()]).rstrip(" ；;，,、→-—>")
    return start.strip(), seg[m.end():].strip()


def kv_field(text: str) -> dict[str, str]:
    """“动机光源=…｜布光=…｜色温=…” → dict（只认第一个 = ）"""
    out = {}
    for p in split_field(text):
        if "=" in p or "＝" in p:
            k, v = re.split(r"\s*[=＝]\s*", p, maxsplit=1)
            out[k.strip()] = v.strip()
    return out


def parse_angle(text: str, names: list[str] | None = None) -> dict:
    """**机位与景别** 第二段（或整段）→ {height, azimuth, ots_from, ots_to, raw}
    height: eye | slightly_high | high | slightly_low | low；azimuth: side45 | ots | front | front_right45 | front_left45 |
    front_right | front_left | none"""
    raw = text or ""
    t = strip_parens(raw)
    names = [n for n in (names or []) if n]
    h = "eye"
    if re.search(r"高机位|俯拍|俯视", t):
        h = "high"
    elif re.search(r"略俯|微俯", t):
        h = "slightly_high"
    elif re.search(r"仰拍|仰视", t):
        h = "low"
    elif re.search(r"略低|微仰|略仰", t):
        h = "slightly_low"
    az = "none"
    if "过肩" in t or "越肩" in raw or "越" in raw and "拍" in raw:
        az = "ots"
    elif re.search(r"右前\s*45|右前方\s*45", t):
        az = "front_right45"
    elif re.search(r"左前\s*45|左前方\s*45", t):
        az = "front_left45"
    elif re.search(r"从右前方|右前方", t):
        az = "front_right"
    elif re.search(r"从左前方|左前方", t):
        az = "front_left"
    elif re.search(r"侧\s*45|侧面|侧 ?机位", t):
        az = "side45"
    elif "正面" in t:
        az = "front"
    frm = to = by = None
    if az == "ots":
        frm, to, by = ots_pair(raw, names)
    return {"height": h, "azimuth": az, "ots_from": frm, "ots_to": to, "ots_by": by, "raw": raw}


_NAME_CLAUSE = r"[^，；。｜|]"


def _ots_pats(alt: str) -> tuple[list[str], list[str], str]:
    """（越 X 拍 Y 成对式, 只给 from 的式子, 只给 to 的式子）。名字后允许括注 / 说明（调用方先去括号再匹配原文）"""
    pair = [rf"越过?着?\s*({alt})(?:的)?{_NAME_CLAUSE}{{0,14}}?拍\s*({alt})",                              # 越X拍Y / 越过X的左肩略仰拍Y
            rf"从\s*({alt}){_NAME_CLAUSE}{{0,20}}?(?:肩|腰侧|后方|身后|背后){_NAME_CLAUSE}{{0,16}}?拍\s*({alt})"]   # 从X的右肩后方越肩拍Y
    frm = [rf"从\s*({alt}){_NAME_CLAUSE}{{0,20}}?(?:肩|腰侧|后方|身后|背后)",
           rf"(?:相机|机位)在\s*({alt}){_NAME_CLAUSE}{{0,12}}?(?:肩|身|背)后",
           rf"越过?着?\s*({alt})(?:的)?{_NAME_CLAUSE}{{0,6}}?肩"]
    return pair, frm, rf"拍\s*({alt})"


def _ots_text(text: str) -> str:
    """过肩认人用的文本：括注里写了“越…拍…”的展开成独立小句（保留原文位置），其余括注（站位 / 说明）去掉"""
    return _PAREN_RE.sub(lambda m: ("，" + m.group(0)[1:-1] + "，") if re.search(r"越[^）)]{0,24}拍", m.group(0)) else "", text or "")


def ots_pair(text: str, names: list[str]) -> tuple[str | None, str | None, str | None]:
    """过肩镜的（被越的人, 被拍的人, 依据）。依据 text = 原文写明（「越X拍Y」「从X的(左|右)?肩(后方)?越过…拍Y」「越过X的…肩拍Y」，
    名字后带括注 / 说明也认，按原文先后取第一处）；fallback = 原文没写明、按 cast 顺序猜（FIX-PRE 11-06：S12 / S14 / S15 原来
    括注一长就匹配不上，退到 cast 顺序把被拍的主体当成了被越的前景）；None = 没有人名"""
    names = [n for n in (names or []) if n]
    if not names:
        return None, None, None
    alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    pair, frm_pats, to_pat = _ots_pats(alt)
    for t in (_ots_text(text),):                         # 名字后的括注去掉（“从卓琳（左下，前景）右肩后方…”），写着越 / 拍的括注展开（“（越江予白拍祁念）”），原文先后不变
        hits = [(m.start(), m.group(1), m.group(2)) for p in pair for m in re.finditer(p, t) if m.group(1) != m.group(2)]
        if hits:
            _, a, b = min(hits)
            return a, b, "text"
        f = min((m for p in frm_pats for m in re.finditer(p, t)), key=lambda m: m.start(), default=None)
        f_name = f.group(1) if f else None
        g_ = next((m for m in re.finditer(to_pat, t) if m.group(1) != f_name), None)
        t_name = g_.group(1) if g_ else None
        if f_name and t_name:
            return f_name, t_name, "text"
        if f_name or t_name:
            order = [n for n in names if n in t] or names
            if f_name is None:
                f_name = next((n for n in order if n != t_name), None)
            else:
                t_name = next((n for n in order if n != f_name), None)
            return f_name, t_name, "fallback"
    raw = text or ""
    frm = next((n for n in names if n in raw), None)
    to = next((n for n in names if n != frm and n in raw), None)
    return frm, to, "fallback"


HEIGHT_ZH = {"eye": "平视（相机与人物眼睛同高）", "slightly_high": "略俯", "high": "高机位俯拍（相机高过头顶、向下看）",
             "slightly_low": "略低微仰", "low": "仰拍"}
HEIGHT_EN = {"eye": "at eye level", "slightly_high": "from slightly above", "high": "from a high angle, the camera above head height looking down",
             "slightly_low": "from slightly below", "low": "from a low angle"}
AZ_ZH = {"side45": "侧 45° 机位", "front": "正面机位", "front_right45": "机位在右前 45°", "front_left45": "机位在左前 45°",
         "front_right": "机位在右前方", "front_left": "机位在左前方", "none": ""}
AZ_EN = {"side45": "from a 45-degree side angle", "front": "from the front", "front_right45": "from the front right at 45 degrees",
         "front_left45": "from the front left at 45 degrees", "front_right": "from the front right", "front_left": "from the front left",
         "none": ""}


def angle_zh(a: dict) -> str:
    if a["azimuth"] == "ots":
        f, t = a.get("ots_from"), a.get("ots_to")
        who = f"（相机在{f}肩后上方，越过{f}的肩拍{t}）" if f and t else ""
        return f"过肩机位{'、' + HEIGHT_ZH[a['height']] if a['height'] != 'eye' else ''}{who}"
    parts = [AZ_ZH.get(a["azimuth"], ""), HEIGHT_ZH[a["height"]]]
    return "，".join(p for p in parts if p)


def angle_en(a: dict, en_of=None) -> str:
    """en_of(name) → 人物短英文（没有就用 the figure）"""
    en_of = en_of or (lambda n: "the figure")
    if a["azimuth"] == "ots":
        f, t = a.get("ots_from"), a.get("ots_to")
        s = "over the shoulder"
        if f and t:
            s += f", shooting past {en_of(f)}'s shoulder in the soft foreground to {en_of(t)}"
        if a["height"] != "eye":
            s += f", {HEIGHT_EN[a['height']]}"
        return s
    parts = [AZ_EN.get(a["azimuth"], ""), HEIGHT_EN[a["height"]]]
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------- 人物短英文 / 代词
_EN_ID_RE = re.compile(r"^(an?\s+\d+-year-old\s+(?:East Asian\s+)?(?:woman|man|girl|boy))", re.I)


def short_en(en_phrase: str) -> str:
    """人物表 EN 外貌长句 → 短身份（“a 35-year-old East Asian woman”）；没有年龄句式就取第一个逗号前"""
    t = (en_phrase or "").strip()
    m = _EN_ID_RE.match(t)
    if m:
        return m.group(1)
    return t.split(",", 1)[0].strip() or "the figure"


def pronouns(en_phrase: str) -> tuple[str, str, str]:
    t = (en_phrase or "").lower()
    if re.search(r"\b(woman|girl|female|lady)\b", t):
        return "she", "her", "her"
    if re.search(r"\b(man|boy|male|gentleman)\b", t):
        return "he", "him", "his"
    return "they", "them", "their"


# ---------------------------------------------------------------- ④ 否定句 → 正向 / 负面词
DEFAULT_NEG_REWRITES = {
    "没有正面补光": "正面留在阴影里",
    "没有被单独打亮": "只接受顶灯的余光",
    "不是死白": "保留灰阶渐变",
    "不死白": "保留灰阶渐变",
    "灯圈不发光": "灯圈熄灭、是暗的",
    "环形灯架关着": "熄灭的环形灯架",
    "环形灯关着": "熄灭的环形灯",
    "关着的环形灯架": "熄灭的环形灯架",
    "环形灯已灭": "环形灯熄灭",
    "眼睛不看镜头": "",
    "不看镜头": "",
    "谁都没有看对方": "各自看着自己的落点",
}
NEG_PREFIX_RE = re.compile(r"^(?:没有|不是|不再|不|无|非|别|勿|禁止|避免)")
NEG_WORD_OK = ("无妆", "无缝", "无名指", "无框", "无袖", "无领", "不锈钢", "不对称", "不规则",
               # REVIEW-B 11-05：以 非 / 别 / 无 / 不 开头但不是否定句的常用词（“非常安静的化妆间”“别墅客厅”“无人的走廊”“不远处的落地灯”
               # 原来整句被丢、残片“常安静的化妆间 / 墅客厅”进负面词）
               "非常", "别墅", "别致", "别针", "别处", "别过", "无人", "无声", "无边", "无数", "无线", "无意", "不远处", "不动声色",
               "不紧不慢", "不经意", "不由自主", "不自觉", "不安", "不少", "不时", "不同", "不断", "不止")   # 不是否定句的词


def rewrite_negations(text: str, table: dict | None = None) -> tuple[str, list[str]]:
    """中文正文：按表改正向；表外以否定词开头的小句整句去掉，去掉否定前缀后的对象进负面词。→ (新文, 负面词列表)"""
    tb = dict(DEFAULT_NEG_REWRITES)
    tb.update(table or {})
    t = text or ""
    for k in sorted(tb, key=len, reverse=True):
        if k in t:
            t = t.replace(k, tb[k])
    negs: list[str] = []
    out = []
    for cl in re.split(r"(?<=[，、；])", t):
        core = cl.strip("，、； ")
        if not core:
            continue
        if NEG_PREFIX_RE.match(core) and not any(w in core for w in NEG_WORD_OK):
            obj = NEG_PREFIX_RE.sub("", core).strip("，、；。 ")
            if obj and obj not in negs:
                negs.append(obj)
            continue
        out.append(cl)
    s = "".join(out).strip("，、； ")
    s = re.sub(r"[，、；]{2,}", "，", s)
    return s, negs


# ---------------------------------------------------------------- ② 人物行：img 原文按人切段、去身份 / 服装句
_IDENT_DROP = (r"^穿", r"^的?[^，]{0,8}(?:直发|长发|短发|卷发|中长发|刘海|头发|发丝|寸头|背头|马尾)", r"^(?:裸妆|淡妆|哑光|无妆|水光|灰绿色眼睛|单眼皮|鼻梁高)",
               r"一颗痣", r"^(?:戴|的[^，]{0,6}(?:手链|手包|耳环|项链|戒指)[^，]{0,4}$)")
_IDENT_KEEP = re.compile(r"吹起|扬开|扬起|扬到|飘|遮住|挡住|散开|被风|垂下来")     # 身份句里带动作的（风吹发丝 / 发丝遮脸）不算身份描述
_LIGHT_CLAUSE = re.compile(r"主光|补光|的光|光在|白幕被|白幕落|白幕在|白幕明亮|白幕比|顶灯从|环形灯从|明暗过渡|渐变|黑旗|光比")


def person_segments(img_body: str, names: list[str]) -> tuple[list[str], dict[str, list[str]], list[str]]:
    """剧本 img 原文 → (前导小句（第一个人名前）, {人: [小句]}, 尾段小句（最后一个“；”后、不以人名开头）)。
    段以“；”分，段内以“，、”分小句；段的主人 = 段首出现的人名"""
    lead: list[str] = []
    per: dict[str, list[str]] = {n: [] for n in names}
    tail: list[str] = []
    for seg in re.split(r"[；;]", img_body or ""):
        seg = seg.strip("，。 ")
        if not seg:
            continue
        clauses = [c.strip() for c in re.split(r"[，]", seg) if c.strip()]      # “、”是并列（服装 / 妆容列表留在一句里整句去掉）
        owner = None
        for i, c in enumerate(clauses):
            n = next((n for n in names if c.startswith(n) or c.startswith("她" + n)), None)
            if n is None and owner is None:
                lead.append(c)
                continue
            if n is not None:
                owner = n
                c = c[len(n):]
                if c.startswith("的"):
                    c = c          # “的左手搭在…” 保留“的”，下面拼回名字
            per[owner].append(c)
        if owner is None:
            tail.extend(clauses)
            lead = [c for c in lead if c not in clauses]
    return lead, per, tail


def person_line(name: str, clauses: list[str], look: dict | None, x: str | None, depth: str | None,
                neg_table: dict | None = None) -> tuple[str, list[str]]:
    """一人一行：名（服装、发、妆）+ 站位 + 动作 + 视线。→ (行, 负面词)"""
    look = look or {}
    tags = [look.get(k, "").strip() for k in ("服装", "发", "妆")]
    tags = [t for t in tags if t]
    head = name + (f"（{'、'.join(tags)}）" if tags else "")
    keep = []
    for c in clauses:
        c0 = c.strip("，。 ")
        if not c0:
            continue
        if any(re.search(p, c0) for p in _IDENT_DROP) and not _IDENT_KEEP.search(c0):   # 发丝被风吹起 / 遮脸是动作，留下
            continue
        if not c0.startswith("的") and _LIGHT_CLAUSE.search(c0) and not re.search(r"视线|目光|手|脸|看", c0):
            continue
        keep.append(name + c0 if c0.startswith("的") else c0)     # “的左手搭在…” → “卓琳的左手搭在…”
    pos = ""
    if x in X_ZH and not any(re.search(r"画面(?:中央|左|右)|^(?:站|蹲|坐|靠|倚)在", c) for c in keep):   # 剧本已写站位就不重复
        pos = f"在{X_ZH[x]}" + (f"（{DEPTH_ZH[depth]}）" if depth in DEPTH_ZH else "")
    body = "，".join(x for x in (pos, *keep) if x)
    body, negs = rewrite_negations(body, neg_table)
    line = head + ("，" + body if body else "")
    return line, negs


def frame_count_sentence(n_faced: int) -> str:
    """⑤ 按露脸人数：两人 / 三人同框…（单人不写）"""
    if n_faced < 2:
        return ""
    z = ZH_NUM.get(n_faced, str(n_faced))
    return f"{z}人同框，{z}张脸都清晰入画，各自的视线落在自己写明的落点上"


# FIX-PRE 11-06（REVIEW-PRE G）：只认“露出的是身体部位”的不露脸写法——“以侧脸入画”“只露出一道金边”“被手掌盖住”都不是不露脸；
# 部位里带 脸 / 眼 / 嘴 / 唇 / 眉 的（“以半边脸与肩入画”）也不算
_BODY_PART = r"(?:肩|背|后脑|腰|手|臂|肘|发|头顶|脖|颈|腿|脚|膝|臀|躯干|身)"
_FACE_PART_RE = re.compile(r"脸|颜|眼|眉|嘴|唇|鼻|面")
_BACK_RE = re.compile(r"背影|背对镜头|背向镜头|背身|背朝镜头")
_HIDDEN_PART_RE = re.compile(rf"以([^，；。｜|]{{1,14}}?{_BODY_PART}[^，；。｜|]{{0,8}}?)(?:在[^，；。]{{0,8}})?入画")
_ONLY_SHOW_RE = re.compile(rf"只露(?:出)?[^，；。｜|]{{0,6}}?{_BODY_PART}")
_FACE_COVERED_RE = re.compile(r"(?:整张脸|全脸|脸完全|整个脸)被[^，；。]{0,6}(?:遮|挡|盖)")
HIDDEN_FACE_RE = re.compile("|".join(p.pattern for p in (_HIDDEN_PART_RE, _ONLY_SHOW_RE, _BACK_RE, _FACE_COVERED_RE)))


def hidden_face(text: str) -> tuple[bool, str]:
    """站位 / img 里这个人的描述 → (不露脸?, 露出的部位)：前景遮挡 / 局部 / 背影 / 只露肩的人不算露脸（PS-MASTER 11-05 审 dump）。
    只认挂在身体部位上的写法（以…肩 / 后脑…入画、只露出…手、背影 / 背对镜头、整张脸被遮）；部位写的是脸 / 侧脸 / 物件不算"""
    t = text or ""
    m = next((m for m in _HIDDEN_PART_RE.finditer(t) if not _FACE_PART_RE.search(m.group(1))), None)
    part = (m.group(1) if m else "").strip("的 ")
    if part:                                   # 写明了露出的部位（“背对镜头｜只以右肩与后脑…入画”）比笼统的“背影”更具体，优先
        return True, part
    if _BACK_RE.search(t):
        return True, "背影"
    if _ONLY_SHOW_RE.search(t) or _FACE_COVERED_RE.search(t):
        return True, "肩与背"
    return False, ""


def frame_sentence(faced: list[str], hidden: dict[str, str]) -> str:
    """⑤ 同框句按露脸人数：全露脸 → “N人同框，N张脸都清晰入画…”；有人不露脸 → “卓琳的脸清晰入画，祁念只以虚化的左肩和腰侧在前景”"""
    if not hidden:
        return frame_count_sentence(len(faced))
    parts = []
    if faced:
        parts.append((faced[0] + "的脸清晰入画") if len(faced) == 1 else frame_count_sentence(len(faced)))
    for n, part in hidden.items():
        parts.append(f"{n}背对镜头的背影" if part == "背影" else f"{n}只以虚化的{part}在前景")
    return "，".join(parts)


def focus_names(angle_field: str, comp_field: str, names: list[str]) -> tuple[list[str], list[str]]:
    """焦点人物 / 焦外人物：机位与景别第三段 + 构图里“X…在焦点上 / 清楚”“X…略虚 / 略在焦外”"""
    t = (angle_field or "") + "｜" + (comp_field or "")
    sharp, soft = [], []
    alt = "|".join(re.escape(n) for n in names if n) or "(?!x)x"
    for n in names:
        for m in re.finditer(re.escape(n), t):
            rest = t[m.end():]
            rest = re.sub(rf"^(?:[与和、]?(?:{alt}))+", "", rest)          # “祁念与卓琳同在焦点上”：两人共用后面的判词
            win = re.split(rf"{alt}|[；。｜|]", rest, maxsplit=1)[0][:30]     # 窗口到下一个人名 / 段分隔为止（括注里的判词也算）
            if re.search(r"在焦点上|同在焦点|清楚|锐利", win) and n not in sharp:
                sharp.append(n)
            if re.search(r"略虚|焦外|略在焦外|虚", win) and n not in soft:
                soft.append(n)
    soft = [n for n in soft if n not in sharp]
    return sharp, soft


# ---------------------------------------------------------------- 视频：L1 布光句（按分镜 **灯光** 字段，AUDIT-RULES 11-05）
_SOURCE_EN = (("环形灯", "the ring light"), ("天花顶灯", "the ceiling light"), ("顶灯", "the ceiling light"), ("落地灯", "the floor lamp"),
              ("台灯", "the desk lamp"), ("化妆镜", "the mirror bulbs"), ("霓虹", "the neon"), ("窗", "the window light"), ("月光", "the moonlight"),
              ("路灯", "the street light"), ("屏幕", "the screen glow"), ("仪表盘", "the dashboard glow"), ("烛", "the candle"), ("火", "the firelight"))
_DIR_EN = (("右前方 45", "from the front right at 45 degrees"), ("右前 45", "from the front right at 45 degrees"),
           ("左前方 45", "from the front left at 45 degrees"), ("左前 45", "from the front left at 45 degrees"),
           ("上侧 45", "from the upper side at 45 degrees"), ("上侧45", "from the upper side at 45 degrees"),
           ("右侧", "from the right side"), ("左侧", "from the left side"), ("侧后", "from behind on one side"), ("逆光", "from behind"),
           ("头顶", "from directly above"), ("上方", "from above"), ("侧", "from the side"), ("正面", "from the front"))


def lighting_en_from(lighting_kv: dict, segment: str, generic: str = "") -> str:
    """分镜 **灯光** 字段（动机光源=…｜布光=…｜色温=…）→ 英文 L1 布光句（不带句号）。取不到动机光源 → generic（配置通用句）。
    幻想段高调暖金（L-130），现实段冷调；布光里的 黑旗 / 负补光、人物比背景亮 N 档 一并带上。模板里不写死“环形灯 + 正面”"""
    src = (lighting_kv or {}).get("动机光源") or ""
    if not src.strip():
        return generic
    name = next((en for zh, en in _SOURCE_EN if zh in src), "the practical light")
    d = next((en for zh, en in _DIR_EN if zh in src), "")
    tone = "high-key warm golden with a soft falloff" if segment == "fantasy" else "cool"
    s = f"lit by {name} as the motivated key{(' ' + d) if d else ''}, {tone}"
    bu = (lighting_kv or {}).get("布光") or ""
    if re.search(r"黑旗|负补光", bu):
        s += " with a black flag on the other side for negative fill"
    m = re.search(r"人物比(?:白幕|背景)亮([一二两三]|\d)到?([一二两三]|\d)?档", bu)
    if m:
        s += ", the figures a stop or two brighter than the backdrop" if m.group(2) else ", the figures a stop brighter than the backdrop"
    elif re.search(r"白幕(?:落到)?中灰|背景.{0,4}暗", bu):
        s += ", the background a stop or two darker"
    elif segment == "fantasy" and "渐变" in bu:
        s += ", the backdrop bright with a gentle gradient"
    return s


TEMPLATE_FORBIDDEN_RE = re.compile(r"ring light[^.]{0,40}\bfront\b(?! (?:right|left))|\bfront\b(?! (?:right|left))[^.]{0,40}ring light", re.I)


def template_default_hits(options: dict) -> list[str]:
    """模板默认句（run_style prompt_template_v2.lighting_en.* / constraint_en）里不得写死“ring light + front”组合（AUDIT-RULES 11-05）"""
    out = []
    le = (options or {}).get("lighting_en") or {}
    vals = [v for k, v in le.items() if not k.startswith("_") and isinstance(v, str)]
    vals += [v for k, d in le.items() if isinstance(d, dict) for kk, v in d.items() if not kk.startswith("_") and isinstance(v, str)]
    vals.append(str((options or {}).get("constraint_en") or ""))
    for v in vals:
        if TEMPLATE_FORBIDDEN_RE.search(v):
            out.append(v[:80])
    return out


# ---------------------------------------------------------------- 视频：运镜句 / 正文拆分
AMP_WORD = {"small": "with small amplitude", "large": "with large amplitude", "medium": "", None: ""}
SPEED_WORD = {"slow": "at slow speed", "fast": "at fast speed", "normal": "", None: ""}
AMP_ORDER = ["small", "medium", "large"]


def amp_speed_of(cam_field: str) -> tuple[str | None, str | None]:
    """**运镜** 字段“推近｜小幅｜起幅=… → 落幅=…｜动机=…” → (amp, speed)：幅度取第二段（小幅 / 中幅 / 大幅），速度只认第二段里的 慢 / 快
    （没写 = None = 官方“normal 不写”）。出图 / 视频两条路都只从这一个镜级字段取，保证同句"""
    mv = split_field(cam_field)
    seg = mv[1].strip() if len(mv) >= 2 else ""
    amp = next((v for k, v in (("小幅", "small"), ("中幅", "medium"), ("大幅", "large")) if k in seg), None)
    speed = "slow" if "慢" in seg else ("fast" if "快" in seg else None)
    return amp, speed


def shift_amp(amp: str | None, shift: int) -> str | None:
    if amp not in AMP_ORDER or not shift:
        return amp
    i = max(0, min(len(AMP_ORDER) - 1, AMP_ORDER.index(amp) + shift))
    return AMP_ORDER[i]


def camera_sentence(move: dict | None, amp: str | None, speed: str | None, pron: tuple[str, str, str],
                    direction: str = "right", key: str = "h3_official") -> str:
    """词表条目（camera_moves.yaml）→ 官方写法运镜句（不带句号）。{amp} {speed} {P} {P1} {P2} {dir} {dir_inv} 占位"""
    tpl = (move or {}).get(key) if move else None
    if not tpl:
        tpl = "The camera holds a static shot"
    amp = shift_amp(amp, int((move or {}).get("h3_amp_shift") or 0))
    s, o, p = pron
    inv = {"left": "right", "right": "left"}.get(direction, "right")
    out = tpl.format(amp=AMP_WORD.get(amp, ""), speed=SPEED_WORD.get(speed, ""), P=p, P1=s, P2=o, dir=direction, dir_inv=inv)
    out = re.sub(r"\s{2,}", " ", out).replace(" ,", ",").strip().rstrip(".")
    return out


OPEN_PHRASE = "established by Picture 1"
_BEAT_FIRST_RE = re.compile(r"\bIn the first\b")
_CAM_RE = re.compile(r"(?:,?\s*and\s+|,\s*|\s+)?(?:a robotic high-speed camera move:\s*)?[Tt]he camera\b[^.]*?(?=\s+(?:as|while)\s+[a-z]|\.)")
_SLOWMO_RE = re.compile(r",?\s*while (?:she|he|they) moves? in super slow motion(?:, hair drifting)?")
_HANDHELD_RE = re.compile(r",?\s*(?:[Hh]andheld documentary camera:[^.]*?)(?=\s+as\s+|\.)")
_PRON_RE = re.compile(r"\b(?:her|his|their|him|them|she|he|they)\b")


def template_pattern(tpl: str) -> re.Pattern:
    """词表模板原句（camera_moves.yaml template）→ 正则：代词不限、首字母大小写不限；用于从写作方分拍里整句去掉已按模板写的运镜句"""
    esc = re.escape(tpl.strip().rstrip("."))
    esc = _PRON_RE.sub("(?:her|his|their|him|them|she|he|they)", esc)
    return re.compile(r"(?:,?\s*and\s+|,\s*|\s+)?" + esc, re.I)


def split_h3_body(body: str) -> tuple[str, str]:
    """写作方 integrated_multimodal_description → (锚点句, 分拍正文)。锚点句 = 第一句（含 established by Picture 1）；
    分拍从 “In the first …” 起；没有时间轴写法就从第二句起"""
    b = (body or "").strip()
    if b.startswith("[Shot 1]"):
        b = b[len("[Shot 1]"):].strip()
    m = _BEAT_FIRST_RE.search(b)
    if m:
        return b[:m.start()].strip(), b[m.start():].strip()
    i = b.find(OPEN_PHRASE)
    j = b.find(". ", i if i >= 0 else 0)
    if j < 0:
        return b, ""
    return b[:j + 1].strip(), b[j + 2:].strip()


def strip_camera_clauses(text: str, templates: list[str] | None = None) -> tuple[str, list[str]]:
    """分拍正文里嵌着的运镜分句（“and the camera pushes in with small amplitude” / V2 bolt 长句）去掉；<d> 内不动。
    templates：词表模板原句，先整句去掉（代词不限），再走通用正则。→ (新文, 去掉的片段)"""
    removed: list[str] = []
    parts = re.split(r"(<d>.*?</d>)", text or "", flags=re.S)
    tpls = [template_pattern(x) for x in (templates or []) if x and len(x) > 12]
    for i in range(0, len(parts), 2):
        seg = parts[i]

        def cut(m):
            removed.append(m.group(0).strip(" ,"))
            return ""
        for rx in tpls:
            seg = rx.sub(cut, seg)
        seg = _HANDHELD_RE.sub(cut, seg)
        seg = _CAM_RE.sub(cut, seg)
        seg = _SLOWMO_RE.sub(cut, seg)
        seg = re.sub(r"\s+,", ",", seg)
        seg = re.sub(r",\s*,", ",", seg)
        seg = re.sub(r"(?<=[a-z]),\s+as\b", " as", seg)
        seg = re.sub(r"\s{2,}", " ", seg)
        parts[i] = seg
    return "".join(parts).strip(), removed


def video_body(d: dict) -> tuple[str, dict]:
    """官方写法 H3 正文（FL2VA 形态；I2VA 由调用方按官方 Case 2 转）。
    d = {size, angle_field, comp_field, chars:[{name,en,look_en,x,depth,presence,part,faced}], h3_body, lighting_en,
         constraint_en, move（词表条目）, move_id, amp, speed, direction, focus（人名）}
    → (正文, 记录 {anchor, camera, removed, beats_src})"""
    chars = [c for c in d.get("chars") or [] if c.get("presence") in ("在画", "局部")]
    names = [c["name"] for c in chars]
    en_by = {c["name"]: short_en(c.get("en") or "") for c in chars}
    a = parse_angle(d.get("angle_field") or "", names)
    comp = f"{SIZE_EN.get(d.get('size') or '', 'a medium shot')} {angle_en(a, lambda n: en_by.get(n, 'the figure'))}".strip()
    subj = []
    for c in chars:
        s = en_by[c["name"]]
        if c.get("look_en"):
            s += f" in {c['look_en']}"
        if c.get("presence") == "局部":
            s += f" entering the frame only partially at {X_EN.get(c.get('x') or '', 'the edge')}"
        else:
            pos = " ".join(x for x in (f"at {X_EN[c['x']]}" if c.get("x") in X_EN else "", DEPTH_EN.get(c.get("depth") or "", "")) if x)
            if pos:
                s += f" {pos}"
        subj.append(s)
    if len(subj) > 1:
        subjects = ", ".join(subj[:-1]) + ", and " + subj[-1]
    else:
        subjects = subj[0] if subj else "the figure"
    light = (d.get("lighting_en") or "").strip().rstrip(".")
    anchor = (f"[Shot 1] Live-action, cinematic, shallow depth of field, {comp} begins in the position, framing and lighting "
              f"established by Picture 1, with {subjects}" + (f", {light}" if light else "") + ".")
    focus = d.get("focus") or (names[0] if names else None)
    fc = next((c for c in chars if c["name"] == focus), chars[0] if chars else None)
    pron = pronouns(fc.get("en") if fc else "") if len(chars) == 1 or fc else ("they", "them", "their")
    if len(chars) > 1 and not d.get("focus"):
        pron = ("they", "them", "their")
    cam = camera_sentence(d.get("move"), d.get("amp"), d.get("speed"), pron, d.get("direction") or "right") + "."
    _anchor_src, beats_src = split_h3_body(d.get("h3_body") or "")
    beats, removed = strip_camera_clauses(beats_src, d.get("templates") or [])
    cons = (d.get("constraint_en") or "").strip()
    if cons and not cons.endswith("."):
        cons += "."
    body = " ".join(x for x in (anchor, cam, cons, beats) if x)
    return body, {"anchor": anchor, "camera": cam, "removed": removed, "beats_chars": len(beats), "move_id": d.get("move_id"),
                  "pron": pron[0], "angle": a}


# ---------------------------------------------------------------- L1 英文审计（H3 正文禁用的平光句）
FLAT_LIGHT_EN = (r"flattening the shadows", r"flat(?:tened)? light(?:ing)?", r"even(?:ly)? lit", r"even lighting", r"shadowless",
                 r"front[- ]lit", r"ring light from the front(?! (?:right|left))", r"bright ring light from the front",
                 r"\bno shadows?\b", r"soft(?:box)? from the front", r"light(?:ing)? from the front(?! (?:right|left))")


def flat_light_hits(text: str) -> list[str]:
    t = re.sub(r"<d>.*?</d>", " ", text or "", flags=re.S)
    return sorted({m.group(0) for p in FLAT_LIGHT_EN for m in re.finditer(p, t, re.I)})
