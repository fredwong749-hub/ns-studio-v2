"""判词自洽检查（确定性代码，不靠模型）：看图模型的“结论”与自己写的“证据”互相打架时，该项作废、重问一次，
重问仍矛盾 → untested（不算 fail、不计重出次数）。PS-MASTER 11-04：出图一次通过率 23.5% 的主要原因之一是好图被矛盾判词打回。

三类（都是纯函数，测试夹具用真实判词原文）：
  ① 看镜头：证据写“未直视 / 非直视 / 没有看镜头 / 看向 X 而非镜头”却判 defect（或反过来）
  ② 造型 / 鞋型：证据里出现的物件（色 + 款）和“应为”的原文物件相同，却判不符
  ③ 方位：模型复述的“原文说 X 在左 / 右”和编译输入（cast 的 x 站位）不一致；或“原文”与“画面”说的是同一侧却判不符
"""
from __future__ import annotations

import re

# ---- ① 看镜头
_GAZE_POS = ("直视", "正对", "看向镜头", "看着镜头", "盯着镜头", "朝向镜头", "对视", "看镜头", "对着镜头", "注视镜头", "望向镜头")
_NEG = r"(?:非|未|不是|不曾|没有|没|并未|并非|不)"
# “否定词 + (直视|正对|看…镜头…)”：否定的是看镜头本身
_NEG_POS_RE = re.compile(_NEG + r"\s*(?:是\s*)?(?:直视|正对|对视|对着|注视|盯着|盯|看向|看着|看|望向|朝向)\s*(?:着)?(?:画面外的?)?(?:镜头|观众|摄像机)?")
_AWAY_RE = re.compile(r"(?:看向|望向|看着|朝向|视线(?:朝|向|落在)?|瞳孔(?:偏向|朝向|转向)?|眼神(?:朝|向)?)\s*(?:画面)?(?:的)?\s*"
                      r"(?:左|右|上|下|外|远处|别处|对岸|窗外|地面|地上|鞋|脚|他|她|对方|手机|桌面|侧面|旁边|前方)")


def gaze_polarity(e: str) -> tuple[bool, bool]:
    """证据句 → (说看镜头, 说没看镜头)。先把“非直视 / 未直视镜头”这类否定短语摘出来算“没看”，剩下的肯定短语才算“看”"""
    e = str(e or "")
    neg = bool(_NEG_POS_RE.search(e)) or bool(_AWAY_RE.search(e))
    rest = _NEG_POS_RE.sub("", e)
    pos = any(k in rest for k in _GAZE_POS)
    return pos, neg


def gaze_contradiction(s: str, e: str) -> bool:
    pos, neg = gaze_polarity(e)
    if s == "defect":          # 判“看镜头”，证据却只说没看
        return neg and not pos
    if s == "ok":              # 判“没看镜头”，证据却只说看镜头
        return pos and not neg
    return False


# ---- ② 造型 / 鞋型
_COLOR = r"(?:[黑白红蓝绿黄灰棕褐米粉紫金银裸杏卡橙]{1,2}色?|透明)"
_GARMENT = ("短靴", "长靴", "踝靴", "马丁靴", "高跟鞋", "细高跟", "凉鞋", "运动鞋", "帆布鞋", "乐福鞋", "平底鞋", "皮鞋", "玛丽珍",
            "丝袜", "连衣裙", "半身裙", "短裙", "长裙", "西装外套", "外套", "风衣", "大衣", "衬衫", "T恤", "毛衣", "针织衫",
            "西裤", "长裤", "牛仔裤", "短裤", "背心", "夹克", "靴", "鞋")
_PHRASE_RE = re.compile("(" + _COLOR + r"?(?:" + "|".join(sorted(_GARMENT, key=len, reverse=True)) + "))")


def garment_phrases(text: str) -> list[str]:
    return [m.group(1) for m in _PHRASE_RE.finditer(str(text or ""))]


def outfit_contradiction(evidence: str, expected: str) -> bool:
    """判不符，证据里提到的物件（色 + 款）全都能在“应为”的原文里原样找到 → 自相矛盾
    （S9 v13：“脚上穿的是黑色短靴，与造型 A 中‘黑色短靴’描述不符”）。证据里出现了原文没有的物件（白色短靴）= 真不符"""
    ph = garment_phrases(evidence)
    if not ph:
        return False
    exp = str(expected or "")
    return all(p in exp for p in ph)


# ---- ③ 方位
_SIDES = ("左", "右")


def truth_positions(cast: list[dict] | None) -> dict[str, str]:
    """编译输入 cast 的站位 → {角色名: 左|右}（只收明确写了左 / 右的；中 / 没写不收）"""
    out = {}
    for c in cast or []:
        name = c.get("name") if isinstance(c, dict) else getattr(c, "name", None)
        x = c.get("x") if isinstance(c, dict) else getattr(c, "x", None)
        if name and x in _SIDES:
            out[str(name)] = x
    return out


def _claims(text: str, name: str, lead: str) -> list[str]:
    pat = re.compile(lead + r"[^。；\n]{0,24}?" + re.escape(name) + r"[^。；，,\n]{0,3}?在\s*(?:画面)?\s*(?:的)?(左|右)")
    return [m.group(1) for m in pat.finditer(str(text or ""))]


def source_claims(text: str, names) -> dict[str, list[str]]:
    """判词里模型自称的“原文说 X 在左 / 右”"""
    return {n: c for n in names if (c := _claims(text, n, r"原文(?:里|中)?(?:写|说|是|写的是)?(?:的)?"))}


def image_claims(text: str, names) -> dict[str, list[str]]:
    """判词里模型对画面的描述“画面里 X 在左 / 右”"""
    return {n: c for n, c in ((n, _claims(text, n, r"画面(?:里|中|上)?(?:的)?")) for n in names) if c}


def position_contradiction(text: str, cast: list[dict] | None, verdict_fail: bool = True) -> str | None:
    """→ 矛盾说明或 None。① 模型复述的“原文”左右与 cast 真实站位不一致（ep01-S15 v1：原文是卓琳左、唐瑶右，判词却写“原文说唐瑶在左”）；
    ② 判不符，但“原文”和“画面”说的是同一侧"""
    truth = truth_positions(cast)
    if not truth:
        return None
    for n, sides in source_claims(text, truth).items():
        if any(s != truth[n] for s in sides):
            return f"判词称原文 {n} 在{sides[0]}，编译输入里 {n} 在{truth[n]}"
    if verdict_fail:
        src, img = source_claims(text, truth), image_claims(text, truth)
        for n in src:
            if n in img and set(src[n]) == set(img[n]):
                return f"判不符，但判词里原文与画面都说 {n} 在{src[n][0]}"
    return None


def truth_note(cast: list[dict] | None) -> str:
    t = truth_positions(cast)
    return "；".join(f"{n}在画面{s}侧" for n, s in t.items())
