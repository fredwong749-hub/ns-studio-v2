"""
Frame Spec 编译器（docs/IMAGE_PROMPT_METHOD.md + docs/frame_spec_compiler.md）：剧本镜头 → 画面说明 JSON → 首帧/尾帧提示词。

纯 Python（只依赖 ns_schema 与 ns_agent.compiler 的常量），不连 ComfyUI、不假设项目路径——gpu3 渲染器可直接导入：

    from ns_agent.frame_spec import build_spec, validate, refine_with_llm, openai_llm
    spec = build_spec(ep, shot, script.globals, looks={"林晚": {...}}, plate_for=lambda name, text, look: "assets/...png")
    spec = refine_with_llm(spec, shot, openai_llm("http://gpu3:8080/v1"))   # 可选；校验不过自动退回规则版

流程：① 确定性解析（景别/机位/运镜/手法/角色代称/屏幕文字/光源/场景/起止状态/facts）
      ② 可选：本地文字模型补全姿态、表情、视线、朝向（只重组不创作），校验不过就丢弃
      ③ 校验：原文每个分句都要落进 facts / not_in_image / notes；数字和颜色必须进 facts；
         facts 与道具必须是原文子串（不得新增人物、道具、事件）
      ④ 按固定顺序拼提示词：触发词 → 角色 DNA → 景别机位 → 人物造型 → 姿态表情 → 场景 → 光线 → 镜头焦外 → 色调 → 画质
剧本原文永不修改：提示词里的每个描述短语都是原文分句的原样拷贝，只是重排和分组。
"""
import hashlib
import json
import re
import urllib.request
from typing import Callable

from ns_schema.frame_spec import (Adornment, CharacterSpec, Fact, FrameSpec, FrameState, Gaze, Lighting, Line,
                                  ModelPrompt, PropState, Reference, SceneSpec, ScriptRef, Technique)
from ns_schema.script import Episode, ScriptGlobals, Shot

from . import gpu3_fields as G3
from .bible import Bible, load_bible
from .compiler import (DARK_WORDS, LAST_FRAME_KEEP, _strip_triggers, classify_adult, corrector_terms, end_state,  # noqa: F401
                       identity_triggers, is_dark, lens_term, material_terms, split_camera, split_dash,
                       without_possessive_mentions, without_possessive_places)

# ------------------------------------------------------------------ 词表（确定性解析）

SHOT_SIZES = ("大特写", "极近特写", "特写", "近景", "中近景", "中景", "全景", "远景")
ANGLES = ("低机位仰拍", "仰拍", "俯拍", "过肩", "桌下视角", "桌面下方", "主观视角", "POV", "平视", "侧拍")
TECHNIQUES = (  # (kind, 关键词, 配方)；按顺序先命中先用
    ("double_exposure", ("双重曝光", "叠化", "叠影"),
     "分别生成两层画面，按原文构图叠化合成为首帧；H3 只做轻微运动"),
    ("split_makeup_mirror", ("各半的脸", "素颜与浓妆各半", "半素颜"),
     "先出完整镜中脸，再对半张脸做妆容局部重绘（M1 素颜 ↔ M4 浓妆）"),
    ("montage", ("蒙太奇", "四格", "宫格"), "拆格单独出图或只取主格"),
    ("screen_text", ("屏幕", "对话框", "搜索框", "界面", "预约页面"), "只允许原文指定的屏幕文字，其余无文字"),
    ("silhouette", ("剪影",), "逆光剪影，人物无面部细节；不加人脸参考"),
    ("reflection", ("倒影", "镜中", "映出", "映着", "映脸"), "主体 + 玻璃/镜中模糊倒影；倒影区域是质检重点"),
    ("timelapse", ("延时", "time-lapse", "一遍遍"), "首帧 = 第一次，尾帧 = 最后一次结果；中间交给 H3"),
    ("freeze", ("定格",), "取动作峰值瞬间"),
    ("pov", ("主观视角", "POV", "视角）"), "按原文视角构图"),
    ("slow_motion", ("慢镜", "slow motion"), "取动作峰值瞬间"),
)
SCENES = (  # (场景锁 id, 关键词)
    ("SK-ELEVATOR", ("电梯",)), ("SK-OFFICE-LOBBY", ("旋转门", "大堂", "写字楼大门")),
    ("SK-OFFICE", ("工位", "写字楼", "办公", "会议")), ("SK-COUNTER", ("专柜",)),
    ("SK-HOME-KITCHEN", ("厨房", "水槽")), ("SK-HOME-DINING", ("餐桌",)),
    ("SK-HOME-BATHROOM", ("浴室",)), ("SK-HOME-BEDROOM", ("卧室", "床上")), ("SK-HOME-CLOSET", ("衣橱", "衣柜")),
    ("SK-HOME-ENTRY", ("玄关",)), ("SK-TAXI", ("出租车",)), ("SK-CAR", ("豪车", "车内", "副驾")),
    ("SK-ALLEY", ("巷",)), ("SK-STREET", ("街道", "商业街", "街头")), ("SK-HOME", ("出租屋",)),
)
TIMES = ("清晨", "早晨", "第二天清晨", "白天", "午后", "黄昏", "傍晚", "夜晚", "晚间", "深夜", "夜景", "当晚", "夜")
LIGHT_WORDS = ("灯", "光", "霓虹", "烛", "阳", "晨", "暗", "黑暗")
PROP_WORDS = ("文件夹", "汤", "碗", "家常菜", "手机", "台灯", "西装", "外套", "衣领", "衣柜", "连衣裙", "纸巾", "口红",
              "唇膏", "镜", "香烟", "汤匙", "电脑", "键盘", "屏幕", "高跟鞋", "椅背", "水杯", "杯", "裙摆", "居家裤",
              "地垫", "洗手台", "方向盘", "落地窗", "玻璃幕墙", "大理石", "旋转门", "金属壁", "楼层数字")
COLOUR_RE = re.compile(r"[红橙黄绿青蓝紫粉白黑灰金银棕褐]色?|冰蓝|草莓粉|樱桃红|正红|暖黄")
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:cm|mm|厘米|毫米|岁|秒|层|%)?|[一二两三四五六七八九十百]+(?:串|枚|颗|个|盏|碗|人|秒|次|件|步|根|条|位|层|格)")
SIZE_WORDS = SHOT_SIZES + ANGLES + ("构图", "画面", "镜头")
METAPHOR_WORDS = ("像", "仿佛", "如同", "形成对比", "呼应", "气场", "意味", "观众", "代餐", "心理", "可视化")
SOUND_WORDS = ("声", "响", "铃声")
BODY_PARTS = {"face": ("脸", "眼", "唇", "嘴", "睫毛", "瞳孔", "刘海", "特写"),
              "hands": ("手", "指", "拇指", "指腹", "指节", "指尖", "握"),
              "feet": ("脚", "足", "高跟", "鞋", "脚趾"),
              "legs": ("腿", "膝"), "back": ("背影", "背对", "后颈", "背抵"),
              "full_body": ("全身", "全景", "走过", "独行", "走着")}
EXPRESSION_WORDS = ("笑", "哭", "泪", "湿", "紧张", "惊", "羞", "得意", "怕", "渴望", "表情", "抿", "咬", "眨", "闭眼",
                    "睁", "红", "礼貌", "端庄", "微张", "上扬")
GAZE_WORDS = ("看", "望", "瞥", "视线", "眼神", "盯", "目光", "迎上")
HAND_WORDS = BODY_PARTS["hands"]
# 表情只写表情词（不带名词短语）：原文关键词 → 表情描述
EXPRESSION_MAP = (("上扬", "嘴角微微上扬"), ("笑", "微笑"), ("泪", "含泪"), ("哭", "哭泣"), ("湿", "睫毛湿润"),
                  ("紧张", "紧张"), ("惊", "惊讶"), ("羞", "羞涩"), ("得意", "得意"), ("怕", "害怕"), ("渴望", "渴望"),
                  ("抿", "抿唇"), ("咬", "咬唇"), ("闭眼", "闭眼"), ("睁", "睁着眼"), ("微张", "嘴唇微张"),
                  ("礼貌", "礼貌"), ("端庄", "端庄"), ("泛红", "脸颊泛红"), ("眨", "眨眼"))
GAZE_MAP = (("低头", "视线向下"), ("垂眼", "视线向下"), ("抬眼", "视线上抬"), ("抬头", "视线上抬"), ("望着天花板", "望向天花板"),
            ("看向窗外", "望向窗外"), ("瞥", "斜瞥"), ("盯", "凝视"), ("迎上", "直视对方"), ("背对", "背对镜头"),
            ("闭眼", "闭眼"), ("侧目", "侧目"))
FOCUS_PARTS = (("nails", ("指甲", "美甲")), ("eye", ("眼睛", "瞳孔", "睫毛", "眼")), ("lips", ("唇", "嘴")),
               ("hand", ("手", "指")), ("foot", ("脚", "足", "高跟", "鞋")), ("face", ("脸",)))
SCREEN_SHARE = {"大特写": "0.9（局部占满画面）", "极近特写": "0.9（局部占满画面）", "特写": "0.7（头部/局部）",
                "近景": "0.5（胸像）", "中近景": "0.45（腰部以上）", "中景": "0.4（腰部以上）", "全景": "0.2（全身入画）",
                "远景": "0.1（人物很小）"}


# T08：表情关键词要带上下文才算（“湿透的衬衫”不是睫毛湿润，“抿了一口汤”不是抿唇，“咬住吸管”不是咬唇）
EXPRESSION_RULES = (
    (r"上扬", "嘴角微微上扬"), (r"笑(?!声|话)", "微笑"), (r"泪", "含泪"), (r"哭", "哭泣"),
    (r"(?:睫毛|眼|眸)[^，,。]{0,6}湿|湿润的(?:眼|睫|眸)|泪湿", "睫毛湿润"),
    (r"紧张", "紧张"), (r"惊(?!醒)", "惊讶"), (r"羞", "羞涩"), (r"得意", "得意"), (r"害怕|惧怕|怕(?!是)", "害怕"),
    (r"渴望", "渴望"), (r"抿[着住紧]?(?:嘴|唇|双唇)|(?:嘴|唇)[^，,。]{0,2}抿", "抿唇"), (r"咬[着住紧]?[^，,。牙]{0,6}(?:下唇|嘴唇|唇)", "咬唇"), (r"咬[紧着住]?牙", "咬牙"),
    (r"闭[上着紧]?(?:双)?眼|闭目|眼睛?紧闭", "闭眼"), (r"睁[开着大]?的?(?:双)?眼|眼睛?[^，,。]{0,6}睁", "睁着眼"),
    (r"(?:唇|嘴)[^，,。]{0,3}微张|微张[^，,。]{0,2}(?:唇|嘴)", "嘴唇微张"),
    (r"礼貌", "礼貌"), (r"端庄", "端庄"), (r"(?:脸|颊)[^，,。]{0,4}(?:泛红|通红|微红|红)|泛红", "脸颊泛红"),
    (r"耳[^，,。]{0,3}(?:泛红|通红|微红|红)", "耳根泛红"), (r"眨", "眨眼"),
    (r"皱眉|蹙眉", "皱眉"), (r"迷离", "眼神迷离"), (r"委屈", "委屈"), (r"慌张|慌乱", "慌张"), (r"愣住|怔住|发愣", "发愣"),
    (r"疲惫", "疲惫"),
)
_EXPR_RULES_RE = tuple((re.compile(p), v) for p, v in EXPRESSION_RULES)


def expression_terms(text: str) -> str:
    out = []
    for rx, v in _EXPR_RULES_RE:
        if rx.search(text) and v not in out:
            out.append(v)
    return "，".join(out)


# 表情 sanitizer（gpu3：expression 只能有表情词）。分段后：命中服装/运镜/动作/视线词 → 去掉（记原因）；
# 剩下的必须含表情词，否则也去掉（not_expression）。顺序 = 判定优先级。
EXPR_STRIP = (
    ("clothing", re.compile(r"衬衫|裙|鞋|高跟|外套|西装|丝袜|袜|束腰|内衣|胸罩|文胸|睡衣|浴袍|衣领|领口|纽扣|扣子|拉链|漆皮|丝绸|"
                            r"蕾丝|毛衣|裤|衣服|上衣|帽|项链|耳环|手链|戒指|乳环|阴环|铁珠|美甲|指甲|妆|口红|唇膏|唇彩|发型|刘海")),
    ("camera", re.compile(r"特写|近景|中景|全景|远景|镜头|机位|景别|推近|拉远|摇至|跟拍|俯拍|仰拍|焦点|焦外|虚化|画面|构图|慢动作|"
                          r"慢镜|定格|过肩")),
    ("gaze", re.compile(r"看向|看着|望向|望着|盯|瞥|注视|凝视|视线|目光|直视|低头|抬头|抬眼|垂眼|垂眸|俯视|仰望|对视|侧目|"
                        r"背对|转头|回头|别过")),
    ("action", re.compile(r"走|跑|坐|站|蹲|跪|躺|趴|靠|拿|握|抓|捧|端(?!庄)|抱|推|拉|递|伸|抬手|举|转身|弯腰|踩|踢|穿|脱|戴|摘|"
                          r"打开|关上|敲|搂|吻|摸|抚|撩|整理|扶|按|拨|翻|吃|喝|写|打字|喂|洗|擦|倒|放下|拿起|走向|贴")),
)
EXPR_KEEP = re.compile("|".join(sorted({v for _, v in EXPRESSION_RULES}, key=len, reverse=True)) +
                       r"|表情|神情|神色|眼神|笑|泪|哭|羞|惊|怒|恼|怕|慌|愣|怔|呆|媚|娇|冷漠|平静|淡定|温柔|坚定|期待|失落|"
                       r"委屈|难过|伤心|痛苦|享受|满足|害羞|妩媚|挑眉|皱眉|咬唇|抿唇|嘟嘴|撅嘴|噘嘴|眯眼|闭眼|睁眼|红晕|绯红|泛红|"
                       r"微张|上扬|下垂的嘴角|喘|无辜|窘|尴尬|错愕|茫然|迷离|紧张|放松|自信|得意|骄傲|不屑|厌恶|嫌弃|心虚")


def sanitize_expression(text: str) -> tuple[str, list[str]]:
    """→ (只含表情词的文本, 去掉的 “片段｜原因” 列表)。分隔符：，,、；;。和空白"""
    kept, stripped = [], []
    for seg in (s.strip() for s in re.split(r"[，,、；;。\s]+", text or "")):
        if not seg:
            continue
        why = next((k for k, rx in EXPR_STRIP if rx.search(seg)), None)
        if why is None and not EXPR_KEEP.search(seg):
            why = "not_expression"
        if why:
            stripped.append(f"{seg}｜{why}")
        elif seg not in kept:
            kept.append(seg)
    return "，".join(kept), stripped


def gaze_terms(text: str) -> str:
    out = []
    for k, v in GAZE_MAP:
        if k in text and v not in out:
            out.append(v)
    return "，".join(out)


CLOSE_SIZES = ("大特写", "极近特写", "特写", "近景")
# T08 焦点部位（顺序 = 同一分句里的优先级）：(code, 中文, 正则)。“手机”不是手，“背景”不是背
FOCUS_RULES = (
    ("nails", "美甲", r"指甲|美甲"), ("eye", "眼睛", r"眼睛|瞳孔|睫毛|眼(?!前|镜)"), ("lips", "嘴唇", r"唇|嘴"),
    ("hand", "手", r"手(?!机)|指尖|指腹|指节|手指|拇指"), ("shoes", "高跟鞋", r"高跟|鞋跟|细跟|鞋"),
    ("foot", "脚", r"脚|足|脚趾|脚踝"), ("face", "脸", r"脸|面容|面部|侧颜"), ("legs", "腿", r"腿|膝"),
    ("back", "背", r"背(?!景|包)|后颈|脊"), ("torso", "身体", r"腰|胸|锁骨|臀|腹"),
    ("screen", "屏幕", r"屏幕|对话框|搜索框|手机界面|聊天记录|预约页面"),
)
_FOCUS_RE = tuple((k, zh, re.compile(p)) for k, zh, p in FOCUS_RULES)
FOCUS_CUE = re.compile(r"特写|近景|聚焦|对准|焦点|推向|推近|推至|摇至|移至|拉近|落在")


def _focus_in(text: str) -> tuple[str, str, str] | None:
    """text 里优先级最高的部位 → (code, 中文, 原文词)"""
    for k, zh, rx in _FOCUS_RE:
        m = rx.search(text)
        if m:
            label = ("鞋" if "高跟" not in text else "高跟鞋") if k == "shoes" else zh
            return k, label, m.group(0) if k in ("shoes", "screen") else ""
    bare = text.replace("镜头", "")
    obj = next((p for p in PROP_WORDS if p in bare), None)
    return ("object", obj, obj) if obj else None


def focus_detail(shot_size: str, img: str, framing: str = "") -> tuple[str, str, str, str]:
    """→ (focus_part, focus_label, focus_object, source)。
    text：同一分句里写了“特写X / X特写 / 镜头推向X / 焦点落在X”；guess：特写/近景镜头的第一个描述分句；
    其余（中景/全景且无焦点线索）→ ("", "", "", "none")"""
    for c in clauses(img):
        for m in FOCUS_CUE.finditer(c):
            hit = _focus_in(c[m.end():m.end() + 10]) or (
                _focus_in(c[max(0, m.start() - 6):m.start()]) if m.group(0) in ("特写", "近景") else None)
            if hit:
                return hit[0], hit[1], hit[2], "text"
    if shot_size not in CLOSE_SIZES and framing not in ("extreme_close", "close"):
        return "", "", "", "none"
    first = next((c for c in clauses(img) if not _has(SHOT_SIZES, c) or len(c) > 6), img)
    hit = _focus_in(first) or _focus_in(img)   # 第一个描述分句里出现的部位优先
    if hit:
        return hit[0], hit[1], hit[2], "guess"
    return "other", "", "", "guess"


def focus_part(shot_size: str, img: str) -> str:
    """兼容旧接口：只返回 code"""
    return focus_detail(shot_size, img)[0]

# 角色代称（剧本里“她”即林晚；“他”按本镜/本集上下文）
MENTIONS = {
    "林晚": ("林晚", "蓝发齐刘海女性", "蓝发女性", "蓝发", "女主", "女员工", "女性", "她"),
    "顾沉": ("顾沉", "黑色高定衬衫中年男总经理", "黑色高定衬衫中年男经理", "黑色高定衬衫男经理", "男总经理", "男经理",
             "总经理", "经理", "男性驾驶", "CEO"),
    "陈宇": ("陈宇", "年轻男生"),
}
CHAR_IDS = {"林晚": "lin_wan", "顾沉": "gu_chen", "陈宇": "chen_yu"}   # V1 旧表；运行时一律经 _char_ids()（cast_table，P1-5b）


def _char_ids() -> dict[str, str]:
    from . import cast_table as CT
    return CT.char_ids() if _manifest_mode() else CHAR_IDS


def _lead_id() -> str | None:
    from . import cast_table as CT
    return CT.lead_id() if _manifest_mode() else "lin_wan"
OFFSCREEN = ("画外",)
EXTRAS = ("柜姐", "成年男性路人", "路人", "男人")
ADORNMENT_BY_TRIGGER = {  # 数量写成整数（REQUIREMENTS C4）
    "nprng2": [Adornment(item="乳环", count=2, placement="双侧")],
    "lbrng9": [Adornment(item="阴环", count=9, placement="左4右4阴蒂1")],
    "lbrng9_beads": [Adornment(item="5毫米黑色铁珠", count=9, placement="每环一串，每串一颗")],
}


def clauses(text: str) -> list[str]:
    return [c.strip() for c in re.split(r"[，,；;。]", text) if c.strip()]


def _sha(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _first(words, text: str) -> str:
    return next((w for w in words if w in text), "")


def _has(words, text: str) -> bool:
    return any(w in text for w in words)


def _manifest_mode() -> bool:
    from . import asset_refs as AR
    from . import cast_table as CT
    p = CT.current_project()
    return bool(p and AR.manifest_rel(p))


def mentions(bible: Bible | None = None) -> dict[str, tuple[str, ...]]:
    """角色代称表：内置 MENTIONS + visual_bible [characters.*].detect（只追加，不改顺序）。
    OVERSIGHT-F P1-5b：资产包模式（V2）只认当前剧人物表（cast_table：清单角色名 + 别名，“她” = 女主），不带 V1 的 MENTIONS / visual_bible"""
    if _manifest_mode():
        from . import asset_refs as AR
        from . import cast_table as CT
        ids, lead = CT.char_ids(), CT.lead_id()
        out2: dict[str, tuple[str, ...]] = {}
        for cid in dict.fromkeys(ids.values()):
            words = tuple(n for n, c in ids.items() if c == cid) + (("她",) if cid == lead else ())
            out2[AR.ID_NAME.get(cid, cid)] = words
        return out2
    out = {k: tuple(v) for k, v in MENTIONS.items()}
    for c in (bible or load_bible()).characters:
        cur = out.get(c.name, ())
        out[c.name] = cur + tuple(w for w in c.detect if w not in cur)
    return out


def resolve_characters(img: str, motion: str, prev_male: str | None = None,
                       bible: Bible | None = None) -> dict[str, list[str]]:
    """原文代称 → 角色；返回 {角色: [原文里指代他/她的词]}。'他' 指本镜出现的男性，否则沿用本集上一个男性（默认顾沉）"""
    found: dict[str, list[str]] = {}
    img, _pl, obj_i = without_possessive_mentions(img, bible)      # QA-03 + FIX P0 ⑦：地点 / 物件所有格不算入镜
    motion, _pm, obj_m = without_possessive_mentions(motion, bible)
    text = img + "，" + motion
    for name, words in mentions(bible).items():
        hits = [w for w in words if w in text]
        if hits:
            found[name] = hits
    if "他" in text.replace("其他", "") and not _manifest_mode():
        # “她与陈宇的日常：他递给她…”：物件所有格的主人又以“他”出现 → 就是他（不落到默认顾沉）
        male = next((m for m in ("陈宇", "顾沉") if m in found), None) or             next((m for m in ("陈宇", "顾沉") if m in obj_i + obj_m), prev_male or "顾沉")
        found.setdefault(male, []).append("他")
    return found


def visible_in(name: str, mentions: list[str], img: str) -> bool:
    """首帧可见：img 里提到；“画外男性”不算"""
    for w in mentions:
        i = img.find(w)
        if i >= 0 and not any(img[max(0, i - 2):i].endswith(o) for o in OFFSCREEN):
            return True
    return False


def _clause_with(img: str, words) -> str:
    return next((c for c in clauses(img) if _has(words, c)), "")


def _clarity(img: str, mentions: list[str]) -> str:
    c = next((c for c in clauses(img) if _has(mentions, c)), "")
    if "剪影" in c:
        return "silhouette"
    if "虚化" in c:
        return "bokeh"
    return "sharp"


def _facing(text: str) -> str:
    if _has(("背影", "背对", "背抵"), text):
        return "back"
    if _has(("侧脸", "侧影", "侧面", "侧目"), text):
        return "profile"
    return "front"


def _fact_kind(c: str, people: list[str]) -> str:
    if NUMBER_RE.search(c):
        return "number"
    if _has(SHOT_SIZES + ANGLES, c):
        return "framing"
    if _has(people, c):
        return "person"
    if COLOUR_RE.search(c):
        return "colour"
    if _has(TIMES, c):
        return "time"
    if _has(LIGHT_WORDS, c):
        return "light"
    if _has(tuple(w for _, ws in SCENES for w in ws), c):
        return "place"
    return "noun"


def classify_clause(c: str) -> str:
    """原文分句去向：not_in_image（比喻/声音） | framing | pose | scene | light"""
    if _has(METAPHOR_WORDS, c) or (_has(SOUND_WORDS, c) and not _has(("声控",), c)):
        return "not_in_image"
    if _has(SHOT_SIZES + ANGLES, c) and len(c) <= 12:
        return "framing"
    if _has(LIGHT_WORDS, c) and not _has(tuple(w for ws in MENTIONS.values() for w in ws), c):
        return "light"
    if _has(tuple(w for ws in MENTIONS.values() for w in ws) + EXTRAS, c) or _has(
            EXPRESSION_WORDS + GAZE_WORDS + HAND_WORDS + ("身", "肩", "腿", "膝", "脚"), c):
        return "pose"
    return "scene"


# ------------------------------------------------------------------ T08：视线 / 占比 / 时间线字段

# 视线线索 (target, 正则)；同一角色取原文里最早出现的那条。<who> 是被看的人，<obj> 是被看的物件
GAZE_CUES = (
    ("camera", r"(?:看|望|盯|凝视|注视|直视|瞥|迎)[向着了]?(?:镜头|观众)|对着镜头|面向镜头|"
               r"直视(?!着?(?:他|她|对方|顾沉|陈宇|林晚))"),
    ("closed", r"闭[上着紧]?(?:双)?眼|闭目|眼睛?紧闭"),
    ("object", r"(?:看|盯|望|注视|凝视|瞥)[着向了一眼]{0,2}(?P<obj>手机屏幕|手机|屏幕|镜中的?自己|镜子|镜中|文件|电脑|"
               r"对话框|聊天记录|照片|戒指|衣柜|手中的[^，,。—]{1,4}|手里的[^，,。—]{1,4}|自己的[^，,。—]{1,4})"),
    ("other", r"(?:看|望|盯|凝视|注视|直视|瞥|迎上)[着向了一眼]{0,2}(?P<who>他|她|对方|顾沉|陈宇|林晚|男人|总经理|经理|年轻男生)"),
    ("other", r"(?:目光|视线)(?:锁定|落在|停在)了?(?P<who>她|他|顾沉|陈宇|林晚)"),
    ("other", r"迎着(?P<who>他|她)的(?:视线|目光)"),
    ("other", r"仰望着?(?P<who>他|她)"),
    ("mutual", r"对视|对望|四目相对|目光交汇|目光相接|相视|交换过?一次眼神"),
    ("down", r"低[着下]?头|垂[下着]?(?:眼|眸)|俯视|低眉|目光下垂|视线向下|看向?地[面上板]|看着脚"),
    ("up", r"抬[起]?(?:眼|眸|头)|仰望|仰头|(?:望|看|盯)[向着]天花板"),
    ("away", r"(?:看|望)[向着]?窗外|望向远[方处]|别过[脸头]|别开[脸视]|移开(?:视线|目光)|转过头|扭头|看向别处|侧过脸|"
             r"眺望|出神|发呆|目光游离|背对|背影|(?:看|望|瞥)[向着]?画外|望[着向]?(?:黑暗|虚空)|望黑暗"),
    ("profile", r"侧脸|侧颜"),   # 有另一人入镜 → other（guess），否则 away（guess）
    # 兜底：“看着/看了一眼/盯着 + 非人称词” → 看物件（这行字、消息、图纸、时间…）
    ("object", r"(?:看|盯|望|凝视|注视)(?:着|了一眼|一眼|向)(?P<obj>(?!的)[^，,。—（(“”\"他她你我自镜]{1,6})"),
)
_GAZE_RE = tuple((k, re.compile(p)) for k, p in GAZE_CUES)
WHO_IDS = {"她": "lin_wan", "林晚": "lin_wan", "顾沉": "gu_chen", "陈宇": "chen_yu", "年轻男生": "chen_yu"}


def _gaze_hits(text: str, present: list[str]) -> list[tuple[int, str, str, str, str | None]]:
    """→ [(位置, target, at, cue, 主语 id | None=双方)]，按位置排序"""
    out = []
    from . import cast_table as CT
    fem = CT.female_ids()
    male = next((p for p in present if p not in fem), None)
    who_ids = CT.who_ids() if _manifest_mode() else WHO_IDS
    for k, rx in _GAZE_RE:
        for m in rx.finditer(text):
            subj = None if k == "mutual" else G3.who(text, m.start(), present)[0]
            at = ""
            if k == "object":
                at = m.group("obj")
            elif k == "other":
                w = m.group("who")
                at = who_ids.get(w) or (male if w in ("他", "男人", "总经理", "经理") else "") or ""
                if w == "对方" or not at or at == subj:
                    at = next((p for p in present if p != subj), "")
            out.append((m.start(), k, at, m.group(0), subj))
    return sorted(out, key=lambda x: x[0])


def gaze_for(cid: str, text: str, present: list[str], last: bool = False) -> Gaze | None:
    """原文里写到的 cid 的视线；没写 → None。首帧取最早的线索，尾帧（last=True）取最后的线索"""
    others = [p for p in present if p != cid]
    hits = _gaze_hits(text, present)
    for _, k, at, cue, subj in (reversed(hits) if last else hits):
        if subj is not None and subj != cid:
            continue
        if k == "mutual":
            if not others:   # 一个人“对视” = 跟镜中/屏幕里的自己
                return Gaze(target="object", at="镜中的自己" if "镜" in text else "", cue=cue, source="guess")
            return Gaze(target="other", at=others[0], cue=cue, source="text")
        if k == "profile":
            return Gaze(target="other" if others else "away", at=others[0] if others else "", cue=cue, source="guess")
        if k == "other" and (not at or at == cid):
            continue
        return Gaze(target=k, at=at, cue=cue, source="text")
    return None


def default_gaze(cid: str, present: list[str], technique: str, face_visible: bool | None) -> Gaze:
    """原文没写视线时的猜测：脸不可见 → away；有另一人 → 看对方；屏幕镜头 → 看屏幕；否则 away（不看镜头）"""
    others = [p for p in present if p != cid]
    if face_visible is False:
        return Gaze(target="away", source="guess")
    if others:
        return Gaze(target="other", at=others[0], source="guess")
    if technique == "screen_text":
        return Gaze(target="object", at="屏幕", source="guess")
    return Gaze(target="away", source="guess")


SCREEN_POS = (("left", r"画面左|左侧|左边|左半"), ("right", r"画面右|右侧|右边|右半"), ("center", r"画面中央|画面正中|居中|正中"),
              ("foreground", r"前景"), ("background", r"背景|远处|身后"), ("bottom", r"画面下|下方"), ("top", r"画面上|上方"))
SHARE_TEXT = re.compile(r"占满|充满画面|占据(?:整个)?画面|填满画面|(?P<frac>三分之[一二]|\d/\d)")


def _share_value(s: str) -> float | None:
    m = re.match(r"\s*([\d.]+)", s or "")
    return float(m.group(1)) if m else None


def share_and_position(own: list[str], base: float | None, primary: bool, bokeh: bool) -> tuple[float | None, str, str]:
    """→ (占比, 来源, 位置)。own = 含该角色代称的原文分句"""
    pos = next((k for k, p in SCREEN_POS for c in own if re.search(p, c)), "")
    for c in own:
        m = SHARE_TEXT.search(c)
        if m:
            f = m.group("frac")
            val = 0.9 if not f else {"三分之一": 1 / 3, "三分之二": 2 / 3}.get(f) or int(f[0]) / int(f[2])
            return round(val, 2), "text", pos
    if bokeh:
        return 0.1, "guess", pos
    if base is None:
        return None, "guess", pos
    return (base if primary else round(base * 0.6, 2)), "guess", pos


def timeline_index(timeline: dict | None) -> dict[str, dict]:
    """state_timeline.json → {shot_id: 本镜条目}；build_spec(timeline_shot=...) 用"""
    return {s["shot_id"]: s for s in (timeline or {}).get("shots", []) if "shot_id" in s}


_TL_CACHE: dict[str, tuple[float, dict]] = {}


def load_timeline_index(root=None) -> dict[str, dict]:
    """读 <root>/data/state_timeline.json（按 mtime 缓存）；没有 / 读不了 → {}（字段按同一规则现算）"""
    try:
        from .bible import project_root
        from .run_paths import timeline_json
        p = timeline_json(project_root(root))   # 不 import state_timeline：只读 JSON；V2CUT-O：按当前剧取（V2 = data/import/runs/<run>/）
        mt = p.stat().st_mtime
        hit = _TL_CACHE.get(str(p))
        if hit and hit[0] == mt:
            return hit[1]
        idx = timeline_index(json.loads(p.read_text(encoding="utf-8")))
        _TL_CACHE[str(p)] = (mt, idx)
        return idx
    except Exception:  # noqa: BLE001
        return {}


def _src(v) -> str:
    return v if v in ("text", "timeline", "guess", "same", "none") else "guess"


def apply_t08(spec: FrameSpec, img: str, motion: str, timeline_shot: dict | None) -> None:
    """T08 字段：framing / faces_visible / prop_states / 每角色 face_visible、视线、占比、镜内换装；focus；risks。
    时间线有的字段原样复用（docs/state_timeline.md §七），没有时间线按 gpu3_fields 同一规则现算。不改提示词。"""
    tl = timeline_shot or {}
    present = [c.id for c in spec.characters if c.id != "extra"]
    if tl.get("framing"):
        spec.framing, spec.framing_source = tl["framing"], _src(tl.get("framing_source"))
    else:
        spec.framing, spec.framing_source = G3.shot_framing(img, motion)
    if "faces_visible" in tl:
        spec.faces_visible, spec.faces_visible_source = list(tl["faces_visible"]), _src(tl.get("faces_visible_source"))
    else:
        spec.faces_visible, spec.faces_visible_source = G3.faces_visible(img, spec.framing, present)
    spec.timeline_inputs_sha = tl.get("inputs_sha") if tl else None
    spec.prop_states = [PropState(**{k: p[k] for k in PropState.model_fields if p.get(k) is not None})
                        for p in tl.get("props", []) if p.get("id")]
    part, label, obj, src = focus_detail(spec.shot_size, img, spec.framing)
    spec.focus_part, spec.focus_label, spec.focus_object, spec.focus_part_source = part, label, obj, src

    start_text = img + "，" + motion_start(motion)
    end_text = spec.last_frame.description
    order = sorted(spec.characters,
                   key=lambda c: min([i for i in (img.find(w) for w in c.mentions) if i >= 0] or [1 << 30]))
    primary = next((c for c in order if c.clarity != "bokeh"), None)
    base = _share_value(SCREEN_SHARE.get(spec.shot_size, ""))
    tchars = tl.get("characters", {})
    for c in spec.characters:
        if c.id != "extra":
            c.face_visible = c.id in spec.faces_visible
            tc = tchars.get(c.id) or {}
            c.changes_within_shot = list(tc.get("changes_within_shot") or [])
            c.items_end = dict(tc.get("items_end") or {})
            c.visibility_end = tc.get("visibility_end")
        own = [x for x in clauses(img) if _has(c.mentions, x)]
        val, ssrc, pos = share_and_position(own, base, c is primary, c.clarity == "bokeh")
        c.screen_share_value, c.screen_share_source, c.screen_position = val, ssrc, pos
        if val is not None and c is not primary and c.clarity != "bokeh" and ssrc == "guess":
            c.screen_share = f"{val}（次要人物）"
        if c.id == "extra":
            c.gaze_start = Gaze(target="unknown", source="guess")
            c.gaze_end = c.gaze_start.model_copy(update={"source": "same"})
            continue
        c.gaze_start = gaze_for(c.id, start_text, present) or default_gaze(
            c.id, present, spec.technique.kind, c.face_visible)
        c.gaze_end = (gaze_for(c.id, end_text, present, last=True) if end_text else None) or c.gaze_start.model_copy(
            update={"source": "same"})

    # 表情只放表情词（gpu3）：规则输出本来就是表情词，这里兜底（LLM 补全后也会再跑一次）
    sanitize_spec_expressions(spec)

    risks = []
    if spec.framing == "full" and spec.faces_visible:
        risks.append("far_small_face：全景/远景且脸入镜（H3 小脸弱点，SOP §8）")
    risks += [f"trigger_conflict：{p.id} 触发词会画成穿着，但 state={p.state}" for p in spec.prop_states
              if p.trigger_conflict]
    if any(c.changes_within_shot for c in spec.characters):
        risks.append("wardrobe_change：镜内换装，尾帧按 items_end")
    spec.risks = risks


def spec_stats(specs: list[FrameSpec]) -> dict:
    """全剧统计（T08 验收用）：focus_part / gaze / framing 分布，各字段 guess 数，校验问题，表情被去掉的词"""
    from collections import Counter
    st: dict = {"shots": len(specs), "focus_part": Counter(), "focus_source": Counter(), "framing": Counter(),
                "framing_source": Counter(), "faces_visible_source": Counter(), "gaze_start": Counter(),
                "gaze_end": Counter(), "gaze_start_source": Counter(), "gaze_end_source": Counter(),
                "screen_share_source": Counter(),
                "risks": Counter(), "with_timeline": 0, "prop_states": 0, "validation_problems": 0,
                "expression_dirty": 0, "expression_stripped": 0}
    for sp in specs:
        st["focus_part"][sp.focus_part or "(none)"] += 1
        st["focus_source"][sp.focus_part_source] += 1
        st["framing"][sp.framing] += 1
        st["framing_source"][sp.framing_source] += 1
        st["faces_visible_source"][sp.faces_visible_source] += 1
        st["with_timeline"] += sp.timeline_inputs_sha is not None
        st["prop_states"] += len(sp.prop_states)
        st["validation_problems"] += len(sp.validation)
        st["expression_stripped"] += len(sp.expression_stripped)
        for r in sp.risks:
            st["risks"][r.split("：")[0]] += 1
        for c in sp.characters:
            st["gaze_start"][c.gaze_start.target] += 1
            st["gaze_end"][c.gaze_end.target] += 1
            st["gaze_start_source"][c.gaze_start.source] += 1
            st["gaze_end_source"][c.gaze_end.source] += 1
            st["screen_share_source"][c.screen_share_source] += 1
            for x in (c.expression.start, c.expression.end):
                st["expression_dirty"] += bool(x) and sanitize_expression(x)[0] != x
    return {k: dict(v.most_common()) if isinstance(v, Counter) else v for k, v in st.items()}


def sanitize_spec_expressions(spec: FrameSpec) -> None:
    stripped = list(spec.expression_stripped)
    for c in spec.characters:
        for k in ("start", "end"):
            clean, gone = sanitize_expression(getattr(c.expression, k))
            setattr(c.expression, k, clean)
            stripped += [f"{c.name}.{k}：{x}" for x in gone]
    for k in ("first_frame", "last_frame"):
        fs = getattr(spec, k)
        clean, gone = sanitize_expression(fs.expression)
        fs.expression = clean
        stripped += [f"{k}：{x}" for x in gone]
    spec.expression_stripped = list(dict.fromkeys(stripped))


CAMERA_PATH = ("从", "展现", "镜头", "焦点")   # “从高跟鞋沿…摇至…”“展现空旷楼层”是运镜路径，不是人物状态


def motion_start(motion_body: str) -> str:
    """首帧 = motion 起点：去掉运镜前缀后、第一个“——”前的第一个人物动作分句（跳过运镜路径）"""
    text = split_camera(motion_body)[1]
    segs = split_dash(text)
    head = segs[0] if segs else ""
    parts = [c for c in clauses(head) if not c.startswith(CAMERA_PATH)]
    return parts[0] if parts else ""


def build_spec(ep: Episode, shot: Shot, g: ScriptGlobals, *, looks: dict[str, dict] | None = None,
               plate_for: Callable[[str, str, dict], str | None] | None = None, prev_male: str | None = None,
               available_triggers: set[str] | None = None, boost_first: str = "", boost_last: str = "",
               bible: Bible | None = None, timeline_shot: dict | None = None) -> FrameSpec:
    """确定性规则版 spec。looks: {角色名: {"look_id", "makeup": [...], "items": [...], "makeup_design"}}；
    plate_for(角色名, 镜头原文, look) -> 底板路径（相对项目根目录）。available_triggers: 有 LoRA 文件的触发词（None = 全保留）。
    timeline_shot: data/state_timeline.json 本镜条目（load_timeline_index()[shot.id]）；给了就复用 framing / faces_visible /
    props / changes_within_shot，不给就按同一规则现算（T08）"""
    looks = looks or {}
    b = bible or load_bible()
    img = _strip_triggers(shot.img_prompt, shot.img_triggers)
    motion = _strip_triggers(shot.motion_prompt, shot.motion_triggers)
    style = g.image_suffix or ""
    adult = bool(classify_adult(ep, shot, b))
    spec = FrameSpec(shot_id=shot.id, episode=ep.number, adult=adult,
                     script_ref=ScriptRef(img=shot.img_prompt, motion=shot.motion_prompt,
                                          sha=_sha(shot.img_prompt, shot.motion_prompt)),
                     decoration_code=shot.decor_state)
    spec.shot_size = _first(SHOT_SIZES, img) or _first(SHOT_SIZES + ("极近",), motion) or "中景"
    spec.angle = _first(ANGLES, img) or _first(ANGLES, motion) or "平视"
    m = re.search(r"\d{2,3}\s*mm[^，,]*?f/\s*[\d.]+", style)
    spec.lens = m.group(0).replace("人像镜头", " ") if m else ""
    spec.camera_move = split_camera(motion)[0]

    for kind, words, recipe in TECHNIQUES:
        if kind == "screen_text" and "“" not in img:
            continue   # 只有原文写了屏幕上的具体文字才算屏幕文字镜头
        if _has(words, img) or (kind in ("timelapse", "slow_motion") and _has(words, motion)):
            spec.technique = Technique(kind=kind, recipe=recipe)
            break
    if spec.technique.kind == "screen_text" or _has(("屏幕", "对话框", "搜索框", "界面", "页面"), img):
        spec.on_screen_text = re.findall(r"“([^”]+)”", img)

    # 角色
    found = resolve_characters(img, motion, prev_male, b)
    people_words = [w for ws in found.values() for w in ws]
    for name, mentions in found.items():
        if not visible_in(name, mentions, img) and not (_char_ids().get(name) == _lead_id() and "她" in motion and not img):
            spec.notes.append(f"{name} 不在首帧画面内（{'/'.join(mentions)}）")
            continue
        own = [c for c in clauses(img) if _has(mentions, c)] or clauses(img)
        own_text = "，".join(own)
        body = [k for k, ws in BODY_PARTS.items() if _has(ws, own_text)]
        lk = looks.get(name, {})
        cs = CharacterSpec(id=_char_ids().get(name, "extra"), name=name, mentions=mentions, look_id=lk.get("look_id"),
                           items=list(lk.get("items", [])), facing=_facing(own_text),
                           clarity=_clarity(img, mentions), visible_body=body,
                           makeup=lk.get("makeup_design", ""))
        if _char_ids().get(name) == _lead_id():
            lock = next((c.description for c in g.character_locks if c.name == name), "")
            nails = re.search(r"手指甲为([^；;，,]+(?:、[^；;，,]+)*)", lock)
            if "hands" in body and nails:
                cs.nails = nails.group(1)
            heels = [t for t in shot.img_triggers if t.startswith("hh")]
            if "feet" in body and heels:
                trig = {t.trigger: t for t in g.lora_triggers}
                cs.heels = "；".join(trig[t].description if t in trig else t for t in heels)
            for t in shot.img_triggers:
                cs.adornment += ADORNMENT_BY_TRIGGER.get(t, [])
        cs.expression.start = expression_terms(own_text + "，" + motion_start(motion))
        cs.screen_share = "0.1（焦外）" if cs.clarity == "bokeh" else SCREEN_SHARE.get(spec.shot_size, "")
        if plate_for and cs.clarity not in ("silhouette",):
            plate = plate_for(name, img + "，" + motion, lk)
            if plate:
                cs.plate = plate
                cs.references.append(Reference(role="identity_face", source=plate,
                                               note=f"只提供{name}的面部身份与发型"))
        spec.characters.append(cs)
    for x in EXTRAS:
        if x in img and not any(x in c.mentions for c in spec.characters):
            spec.characters.append(CharacterSpec(id="extra", name=x, mentions=[x], clarity=_clarity(img, [x])))
    lead = next((c for c in spec.characters if c.id == _lead_id()), None)
    spec.look_id = lead.look_id if lead else None

    # 场景 / 光线
    spec.scene = SceneSpec(id=next((sid for sid, ws in SCENES if _has(ws, img)), ""),
                           place=_clause_with(img, tuple(w for _, ws in SCENES for w in ws)),
                           time_of_day=_first(TIMES, img), props=[p for p in PROP_WORDS if p in img])
    dark = is_dark(img, b)
    spec.lighting = Lighting(sources=[c for c in clauses(img) if classify_clause(c) == "light"
                                      or (_has(("灯", "光斑", "晨光", "霓虹", "白光", "流光"), c))],
                             dark=dark,
                             grade="保留剧本光源与夜色，色彩浓郁饱满但不提亮暗部" if dark
                             else "浓郁饱和的暖色调，剧本光源优先")

    # 起止状态
    start = motion_start(motion)
    end = end_state(motion)
    pose = [c for c in clauses(img) if classify_clause(c) == "pose"]
    spec.first_frame = FrameState(description="，".join(pose), action=start,
                                  expression=lead.expression.start if lead else expression_terms(img + start),
                                  gaze=gaze_terms(img + "，" + start),
                                  hands="，".join(c for c in pose if _has(HAND_WORDS, c)))
    end_parts = clauses(end)
    spec.last_frame = FrameState(description=end, action=end,
                                 expression=expression_terms(end),
                                 gaze=gaze_terms(end),
                                 hands="，".join(c for c in end_parts if _has(HAND_WORDS, c)))
    if lead:
        lead.expression.end = spec.last_frame.expression

    visible = [c for c in spec.characters if c.clarity != "silhouette" or True]
    n = len(visible)
    spec.composition = "9:16竖构图" + (f"，画面中仅{['', '一', '两', '三'][n]}人" if 2 <= n <= 3 else "")

    # facts：img 每个分句一条（原样），比喻/声音进 not_in_image；motion 起点作为首帧动作 fact
    for c in clauses(img):
        where = classify_clause(c)
        if where == "not_in_image":
            spec.not_in_image.append(c)
            continue
        spec.facts.append(Fact(text=c, kind=_fact_kind(c, people_words), must=True, frame="first"))
    if start and not any(start in f.text or f.text in start for f in spec.facts):
        if not _has(METAPHOR_WORDS, start):
            spec.facts.append(Fact(text=start, kind="action", must=True, frame="first"))
    for c in end_parts:
        if not _has(METAPHOR_WORDS, c):
            spec.facts.append(Fact(text=c, kind="action", must=False, frame="last"))
    if shot.sound:
        spec.not_in_image.append(f"音效：{shot.sound}")
    lw_in = any(c.name == "林晚" for c in spec.characters)
    lock_text = "".join(c.description for c in g.character_locks)
    for t in [m for m in material_terms(shot.img_prompt, lin_wan=lw_in, bible=b) if m not in lock_text]:
        # B5/B7/B8 材质规则：项目方全局要求，按 must 事实核对
        spec.facts.append(Fact(text=t, kind="rule", must=True, frame="both"))
    spec.boost_first, spec.boost_last = boost_first or "", boost_last or ""
    if spec.boost_first:
        spec.facts.append(Fact(text=spec.boost_first, kind="boost", must=True, frame="first"))
    if spec.boost_last:
        spec.facts.append(Fact(text=spec.boost_last, kind="boost", must=True, frame="last"))
    spec.dialogue = [Line(speaker=d.speaker_note or "", text=d.text) for d in shot.dialogue]
    spec.voiceover = shot.voiceover.text if shot.voiceover else ""

    apply_t08(spec, img, motion, timeline_shot)
    spec.model_prompt = assemble(spec, shot, g, available_triggers, b)
    spec.validation = validate(spec)
    return spec


# ------------------------------------------------------------------ 提示词拼装

def _style_with_text_exception(style: str, texts: list[str]) -> str:
    """屏幕文字镜头：只放开原文指定的文字，其余仍然无文字"""
    if not texts:
        return style
    allowed = "、".join(f"“{t}”" for t in texts)
    out = re.sub(r"无字幕、无文字、无水印|无字幕，无文字|无任何文字", f"除屏幕上的{allowed}外无其他文字、无字幕、无水印", style)
    return out if out != style else f"{style}，除屏幕上的{allowed}外无其他文字"


def _drop_terms(text: str, terms: list[str]) -> str:
    for t in terms:
        if t and t in text:
            text = text.replace(f"（{t}）", "").replace(f"({t})", "").replace(t, "")
    text = re.sub(r"[，,]{2,}", "，", text).strip("，, ")
    return re.sub(r"（[，,\s]*）", "", text)


def assemble(spec: FrameSpec, shot: Shot, g: ScriptGlobals, available_triggers: set[str] | None = None,
             bible: Bible | None = None) -> ModelPrompt:
    """顺序：触发词 → 角色 DNA → 景别机位 → 人物造型 → 姿态表情 → 场景 → 光线 → 镜头焦外 → 色调 → 画质"""
    b = bible or load_bible()
    def join(*parts):
        return "，".join(p.strip("，, ") for p in parts if p and p.strip("，, "))

    triggers = [t for t in shot.img_triggers if available_triggers is None or t in available_triggers]
    visible = {c.name for c in spec.characters}
    # 系统自动补的角色 LoRA 触发词（config/auto_lora_triggers.json，导入时已补）：统一放最前、不重复、不被过滤
    first = [t for t, _f, _s in identity_triggers(visible, (), b)]
    triggers = first + [t for t in triggers if t not in first]
    locks = [f"【{c.name}】{c.description}" for c in g.character_locks if c.name in visible]
    facts_first = [f.text for f in spec.facts if f.frame == "first" and f.kind not in ("boost", "rule")]
    rules = [f.text for f in spec.facts if f.kind == "rule"]
    by = {k: [c for c in facts_first if classify_clause(c) == k] for k in ("framing", "pose", "scene", "light")}
    framing = join(*by["framing"], *(x for x in (spec.shot_size, spec.angle) if not any(x in c for c in by["framing"])))
    wear = []
    for c in spec.characters:
        if c.nails:
            wear.append(f"{c.name}的手指甲：{c.nails}")
        if c.heels:
            wear.append(c.heels)
        wear += [f"{a.item}{a.count or ''}枚（{a.placement}）" if a.count else a.item for a in c.adornment]
        if c.clarity == "bokeh":
            wear.append(f"{c.name}在焦外虚化")
    # QA（ITERAUDIT 11-01 ep02-S1）：系统补的穿戴描述（例“12cm透明细高跟（鞋跟直径约6毫米…）”）里的材质条目，
    # 剧本原文自己已经写了 → 从系统那一份里删掉（原文一字不动；材质条目整句只保留一次）
    script_text = "，".join(facts_first)
    wear = [_drop_terms(w, [m.term for m in b.materials if m.term in script_text]) for w in wear]
    wear = [w for w in wear if w]
    pose = join(*by["pose"])
    img_body = _strip_triggers(shot.img_prompt, shot.img_triggers)
    corr = corrector_terms(img_body, adult=spec.adult, bible=b)
    style = _style_with_text_exception(g.image_suffix or "", spec.on_screen_text)
    lighting = join(*by["light"], b.dark_term if spec.lighting.dark else "")
    corr = [t for t in corr if t not in lighting]
    worn = "，".join(wear)
    rules = [r for r in rules if r not in worn]    # QA-04：高跟描述里已含材质句就不再追加
    # 项目方 2026-10-30：规格词由系统自动补；写作者原文里已有同一条目就不再追加（去重）
    first_own = join(*locks, framing, *wear, pose, *by["scene"], lighting, spec.composition)
    qwen = join("，".join(triggers), *locks, framing, *wear, pose, *by["scene"], lighting, spec.composition,
                *(t for t in corr if t not in first_own), *(r for r in rules if r not in first_own),
                spec.boost_first, lens_term(style, b), style)
    end = spec.last_frame.description
    # G1（2026-10-30）：尾帧沿用首帧的触发词、穿戴、纠正器与规则条目；结束状态里新出现的材质词（如赤脚）再补；
    # 结束状态里写作者已写过的条目不再加
    last_own = join(end, *locks, *wear, lighting)
    corr_last = [t for t in corrector_terms(img_body + "，" + end, adult=spec.adult, bible=b) if t not in last_own]
    rules_last = [r for r in rules if r not in last_own]
    extra = [m for m in material_terms(end, present=visible, bible=b)
             if m not in worn and m not in rules and not any(m in lk for lk in locks)]
    qwen_last = join("，".join(triggers), b.last_frame_keep + end, *locks, *wear, lighting, *corr_last, *rules_last,
                     *extra, spec.boost_last, lens_term(style, b), style) if end or spec.boost_last else ""
    return ModelPrompt(qwen=qwen, qwen_last=qwen_last)


# ------------------------------------------------------------------ 校验

def _spec_text(spec: FrameSpec) -> str:
    d = spec.model_dump(exclude={"script_ref", "model_prompt", "validation", "generator", "version"})
    return json.dumps(d, ensure_ascii=False)


TRIGGER_RE = re.compile(r"(?<![A-Za-z])(?!ep\d)[a-z_]+\d+[a-z_\d]*")


def validate(spec: FrameSpec) -> list[str]:
    """返回问题列表（空 = 通过）。
    1) img 每个分句都要原样落进 facts / not_in_image / notes；2) 原文的数字和颜色必须出现在 facts；
    3) facts / 道具 / 角色必须来自原文（不得新增）"""
    problems = []
    img = spec.script_ref.img
    no_trig = TRIGGER_RE.sub("", img)   # LoRA 触发词（nprng2 等）里的数字不算原文数字
    src = spec.script_ref.img + "，" + spec.script_ref.motion
    placed = [f.text for f in spec.facts] + spec.not_in_image + spec.notes
    for c in clauses(img):
        if re.fullmatch(r"[A-Za-z0-9_ ]+", c):   # 触发词
            continue
        if not any(c in p for p in placed):
            problems.append(f"原文分句未覆盖：{c}")
    facts_text = "，".join(f.text for f in spec.facts)
    for m in NUMBER_RE.finditer(no_trig):
        if m.group(0).strip() and m.group(0) not in facts_text and not any(m.group(0) in x for x in spec.not_in_image):
            problems.append(f"数字未进 facts：{m.group(0)}")
    for m in COLOUR_RE.finditer(img):
        if m.group(0) not in facts_text and not any(m.group(0) in x for x in spec.not_in_image):
            problems.append(f"颜色未进 facts：{m.group(0)}")
    for f in spec.facts:
        if f.kind not in ("boost", "rule") and f.text not in src:
            problems.append(f"facts 不是原文：{f.text}")
    for p in spec.scene.props:
        if p not in src:
            problems.append(f"新增道具：{p}")
    exprs = [(f"{c.name}.{k}", getattr(c.expression, k)) for c in spec.characters for k in ("start", "end")]
    exprs += [(k, getattr(spec, k).expression) for k in ("first_frame", "last_frame")]
    for where, x in exprs:   # T08：表情只放表情词
        if x and sanitize_expression(x)[0] != x:
            problems.append(f"表情含非表情词：{where}={x}")
    for c in spec.characters:
        if c.id != "extra" and not any(w in src for w in c.mentions):
            problems.append(f"新增人物：{c.name}")
        if c.id == "extra" and c.name not in src:
            problems.append(f"新增人物：{c.name}")
    return problems


# ------------------------------------------------------------------ 可选：本地文字模型补全（只重组不创作）

LLM_FIELDS = ("first_frame", "last_frame", "composition")
LLM_CHAR_FIELDS = ("facing", "clarity", "screen_share", "visible_body", "expression")
LLM_PROMPT = """你是短剧分镜的画面说明员。根据剧本原文，把这个镜头的首帧（动作起点）和尾帧（动作终点）写成图片模型能执行的画面说明。
规则：只重组原文，不新增人物、道具、事件；比喻和心理活动翻译成看得见的表情、视线、姿态；数字、颜色、名词原样保留。
只输出 JSON，格式：
{{"first_frame":{{"pose":"","action":"","expression":"","gaze":"","hands":""}},
 "last_frame":{{"pose":"","action":"","expression":"","gaze":"","hands":""}},
 "composition":"",
 "characters":[{{"name":"","facing":"front|three_quarter|profile|back","clarity":"sharp|soft|silhouette|bokeh","screen_share":"","visible_body":[],"expression":{{"start":"","end":""}}}}]}}
剧本 img：{img}
剧本 motion：{motion}
已解析的角色：{chars}"""


def _json_block(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else {}


def refine_with_llm(spec: FrameSpec, shot: Shot, llm: Callable[[str], str], model_name: str = "local") -> FrameSpec:
    """用本地文字模型补全姿态/表情/视线/朝向；只合并白名单字段。合并后校验或新增词检查不过 → 原样返回规则版"""
    prompt = LLM_PROMPT.format(img=shot.img_prompt, motion=shot.motion_prompt,
                               chars="、".join(c.name for c in spec.characters) or "无")
    try:
        out = _json_block(llm(prompt))
    except Exception as e:  # noqa: BLE001 — 模型不可用时退回规则版
        spec.notes.append(f"LLM 补全失败：{type(e).__name__}")
        return spec
    new = spec.model_copy(deep=True)
    for k in ("first_frame", "last_frame"):
        if isinstance(out.get(k), dict):
            base = getattr(new, k).model_dump()
            base.update({f: str(v) for f, v in out[k].items() if f in base and v})
            setattr(new, k, FrameState(**base))
    if out.get("composition"):
        new.composition = f"{spec.composition}，{out['composition']}"
    for oc in out.get("characters") or []:
        cs = next((c for c in new.characters if c.name == oc.get("name")), None)
        if not cs:
            continue  # 不接受新增人物
        for f in LLM_CHAR_FIELDS:
            if f in oc and oc[f]:
                try:
                    setattr(cs, f, type(getattr(cs, f)).model_validate(oc[f]) if f == "expression" else oc[f])
                except Exception:  # noqa: BLE001
                    pass
        try:
            CharacterSpec.model_validate(cs.model_dump())
        except Exception:  # noqa: BLE001
            return spec
    new.generator = f"rules+llm:{model_name}"
    sanitize_spec_expressions(new)   # T08：LLM 写进表情的服装/动作/运镜/视线词去掉（记在 expression_stripped）
    problems = validate(new)
    if problems:
        spec.notes.append("LLM 补全被拒：" + "；".join(problems[:3]))
        return spec
    new.validation = problems
    return new


def openai_llm(base_url: str, model: str = "local", timeout: float = 300, api_key: str | None = None
               ) -> Callable[[str], str]:
    """OpenAI 兼容端点（llama.cpp llama-server / gpu3 27B GGUF）。base_url 例：http://127.0.0.1:8080/v1"""
    def call(prompt: str) -> str:
        body = json.dumps({"model": model, "temperature": 0, "response_format": {"type": "json_object"},
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"]
    return call


def _main(argv: list[str]) -> int:
    """uv run python -m ns_agent.frame_spec stats [项目根目录]   → 全剧 360 镜 spec 统计（读 state_timeline.json）"""
    import sys
    from pathlib import Path

    from ns_schema.script import ScriptImport

    from .bible import project_root
    if not argv or argv[0] != "stats":
        print(_main.__doc__)
        return 2
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    root = project_root(argv[1] if len(argv) > 1 else None)
    from . import run_paths as RP
    script = ScriptImport.model_validate_json(RP.script_json(root).read_text(encoding="utf-8"))   # I-14
    tl = load_timeline_index(root)
    b = load_bible(root)
    specs, prev_male = [], None
    for ep in script.episodes:
        for s in ep.shots:
            specs.append(build_spec(ep, s, script.globals, bible=b, timeline_shot=tl.get(s.id), prev_male=prev_male))
    print(json.dumps(spec_stats(specs), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    import sys as _sys
    raise SystemExit(_main(_sys.argv[1:]))
