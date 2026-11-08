"""判定细则：提示词组装 + 模型输出解析（config/prompts/vqa_inspect.md；检查项 = config/vqa.json checks）。"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import ROOT, cfg

PROMPTS = ROOT / "config" / "prompts"
STATUSES = ("ok", "defect", "unsure", "na")
SIZES = ("大特写", "特写", "近景", "中近景", "中景", "中全景", "全景", "远景")


def sections(name: str) -> dict[str, str]:
    """把 vqa_inspect.md / vqa_judge.md 按 “## 名字” 切段"""
    txt = (PROMPTS / name).read_text(encoding="utf-8")
    out, cur, buf = {}, None, []
    for ln in txt.splitlines():
        m = re.match(r"^## (\w+)\s*$", ln)
        if m:
            if cur:
                out[cur] = "\n".join(buf).strip()
            cur, buf = m.group(1), []
        elif cur:
            buf.append(ln)
    if cur:
        out[cur] = "\n".join(buf).strip()
    return out


def ref_label(path: str) -> str:
    p = path.replace("\\", "/")
    name = Path(p).stem
    if "identity_refs/" in p:
        who = p.split("identity_refs/")[1].split("/")[0]
        return f"身份脸参考（{who}，只看脸和发型）"
    if "refboards/" in p:
        return "参考板（本镜要穿戴的单品，每格一件）"
    if "refcrops/" in p or "look" in name:
        return "造型设定图（只看服装 / 鞋 / 饰品，不看背景和姿势）"
    if "/shoes/" in p:
        return "参考鞋"
    if "/scenes/" in p or "scene" in name:
        return "场景参考（地点 / 陈设 / 光线）"
    if "/items/" in p:
        return "单品参考"
    return "参考图"


def spec_from_prompt(prompt: str) -> dict:
    """从系统编译的出图提示词里摘要点（剧本 / 契约字段齐全时调用方直接传，优先于这里）"""
    p = prompt or ""
    cast = list(dict.fromkeys(re.findall(r"【([^】]{1,6})】", p)))
    cast = [c for c in cast if c not in ("场景",)]
    body = re.sub(r"^(?:图\d+是[^。]*。)+", "", p)          # 去掉“图N是…”参考说明再找第一个景别词
    m0 = re.search("|".join(sorted(SIZES, key=len, reverse=True)), body)
    size = m0.group(0) if m0 else "未写"
    items = []
    m = re.search(r"要穿全：([^（(]+)", p)
    if m:
        items += [x.strip() for x in re.split(r"[、，,]", m.group(1)) if x.strip()]
    items += [x.strip() for x in re.findall(r"[①②③④⑤⑥⑦⑧⑨]([^①②③④⑤⑥⑦⑧⑨—]+)", p)]
    sm = re.search(r"【场景】名称：([^；;]+)", p)
    # 只认“视线”类写法；“侧对镜头 / 侧脸朝向镜头”是朝向不是视线，不算允许看镜头
    cam_ok = bool(re.search(r"(?<![不别勿])(看向?镜头|直视镜头|望向镜头|看向观众|视线[^，。；]{0,4}镜头|眼睛[^，。；]{0,4}镜头|对视镜头)", p))         and not re.search(r"(不|别|勿|避免)(要)?(看|直视|望向)镜头|视线[^，。；]{0,4}(避开|不|越过|绕过|错过)[^，。；]{0,2}镜头", p)
    return {"cast": cast or ["未写"], "size": size, "items": list(dict.fromkeys(items))[:12] or ["未写"],
            "scene": sm.group(1).strip() if sm else "未写", "camera_ok": "允许" if cam_ok else "不允许（原文没写看镜头）"}


def build_image_request(subject: str, prompt: str, references: list[str] | None = None, *,
                        extra_refs: list[tuple[str, str]] | None = None, spec: dict | None = None,
                        root: Path = ROOT) -> dict:
    """→ {system, text, images:[{path, label, max_pixels}], spec}。images 里的路径是本机绝对路径；client 决定传路径还是字节"""
    m = cfg().get("model") or {}
    sec = sections("vqa_inspect.md")
    sp = {**spec_from_prompt(prompt), **(spec or {})}
    imgs, lines = [], []
    refs = [(r, ref_label(r)) for r in (references or [])] + list(extra_refs or [])
    for i, (r, lab) in enumerate(refs, 1):
        fp = Path(r) if Path(r).is_absolute() else root / r
        if not fp.exists():
            continue
        imgs.append({"path": str(fp), "label": f"参考图{len(imgs) + 1}：{lab}", "max_pixels": m.get("ref_max_pixels", 360000)})
        lines.append(f"- 参考图{len(imgs)}：{lab}")
    sfp = Path(subject) if Path(subject).is_absolute() else root / subject
    imgs.append({"path": str(sfp), "label": "【待检图】", "max_pixels": m.get("subject_max_pixels", 1300000)})
    text = sec["user"].format(refs="\n".join(lines) or "- （本镜没有参考图）", spec=(prompt or "（无）")[:2500],
                              cast="、".join(sp["cast"]), size=sp["size"], items="、".join(sp["items"]),
                              scene=sp["scene"], camera_ok=sp["camera_ok"])
    # 跨镜连贯一行不放进模板占位符（模板被运行中的旧进程读到会 KeyError，16:24 vqa_skip 事故）：插在“是否允许看镜头”行后
    cont = "- 跨镜连贯：" + (sp.get("continuity") or "（没有同场上一镜参考：continuity_break 写 na）")
    key = "- 是否允许看镜头：" + sp["camera_ok"]
    text = text.replace(key, key + chr(10) + cont, 1) if key in text else text + chr(10) + cont
    return {"system": sec["system"], "text": text, "images": imgs, "spec": sp}


def build_video_request(frames: list[str], prompt: str, references: list[str] | None = None, *,
                        root: Path = ROOT) -> dict:
    m = cfg().get("model") or {}
    sec = sections("vqa_inspect.md")
    imgs, lines = [], []
    for r in references or []:
        fp = Path(r) if Path(r).is_absolute() else root / r
        if fp.exists():
            imgs.append({"path": str(fp), "label": f"参考图{len(lines) + 1}：{ref_label(r)}", "max_pixels": m.get("ref_max_pixels", 360000)})
            lines.append(f"- 参考图{len(lines) + 1}：{ref_label(r)}")
    for i, f in enumerate(frames, 1):
        imgs.append({"path": str(f), "label": f"【第 {i} 帧】", "max_pixels": 600000})
    text = sec["video_user"].format(n=len(frames), refs="\n".join(lines) or "- （无）", spec=(prompt or "（无）")[:2500])
    text += "\n\n输出格式与判定口径如下（【待检图】= 这组抽帧）：\n请逐项检查" + sec["user"].split("请逐项检查", 1)[1].format()
    return {"system": sec["system"], "text": text, "images": imgs}


# ---------------------------------------------------------------- 解析
def _loads_loose(s: str) -> dict | None:
    s = s.strip()
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.M).strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    body = s[i:j + 1]
    for cand in (body, re.sub(r",\s*([}\]])", r"\1", body)):
        try:
            return json.loads(cand)
        except ValueError:
            continue
    return None


_ITEM = re.compile(r'"(\w+)"\s*:\s*\{\s*"s"\s*:\s*"(\w+)"\s*(?:,\s*"e"\s*:\s*"((?:[^"\\]|\\.)*)")?')


def _salvage(text: str, known: set) -> dict | None:
    """截断 / 不合法的 JSON：逐项抢 "id": {"s": "...", "e": "..."}；people 尽量整段解析。抢到的项照常用，没输出的项 = 缺（不算 ok）"""
    checks = {m.group(1): {"s": m.group(2), "e": m.group(3) or ""} for m in _ITEM.finditer(text or "") if m.group(1) in known}
    if not checks:
        return None
    out = {"checks": checks}
    m = re.search(r'"people"\s*:\s*(\[.*?\])\s*,\s*"', text or "", re.S)
    if m:
        try:
            out["people"] = json.loads(m.group(1))
        except ValueError:
            pass
    m = re.search(r'"people_count"\s*:\s*(\d+)', text or "")
    if m:
        out["people_count"] = int(m.group(1))
    return out


def parse(text: str) -> dict:
    """→ {ok_parse, checks:{id:{s,e}}, defects:[id], unsure:[id], people, people_count, scene, framing, raw?}"""
    d = _loads_loose(text or "")
    known = set((cfg().get("checks") or {})) | set((cfg().get("video_checks") or {}))
    salvaged = False
    if not isinstance(d, dict) or not d.get("checks"):
        sv = _salvage(text or "", known)        # VQAGATE 11-01：输出被 max_new_tokens 截断（ep01 7 张里 3 张）→ 按项抢救
        if sv:
            d, salvaged = sv, True
    if not isinstance(d, dict):
        return {"ok_parse": False, "checks": {}, "defects": [], "unsure": [], "raw": (text or "")[:2000]}
    raw = d.get("checks") or {}
    checks = {}
    for k, v in raw.items():
        if k not in known:
            continue
        if isinstance(v, str):
            v = {"s": v, "e": ""}
        s = str((v or {}).get("s") or (v or {}).get("status") or "").strip().lower()
        s = {"pass": "ok", "fail": "defect", "yes": "defect", "no": "ok", "n/a": "na"}.get(s, s)
        checks[k] = {"s": s if s in STATUSES else "unsure", "e": str((v or {}).get("e") or (v or {}).get("evidence") or "")[:300]}
    return {"ok_parse": True, "salvaged": salvaged, "checks": checks,
            "defects": [k for k, v in checks.items() if v["s"] == "defect"],
            "unsure": [k for k, v in checks.items() if v["s"] == "unsure"],
            "people": d.get("people") or [], "people_count": d.get("people_count"),
            "scene": d.get("scene"), "framing": d.get("framing")}


def describe(v: dict) -> tuple[str, str]:
    """→ (描述文字, 缺陷清单文字)：R11-10 只把这两段文字（不含任何图片）交给写作网关"""
    ppl = []
    for i, p in enumerate(v.get("people") or [], 1):
        if isinstance(p, dict):
            ppl.append(f"第{i}人 {p.get('who', '?')}（{p.get('pos', '?')}）：手 {p.get('hands_visible')}、臂 {p.get('arms_visible')}、腿 {p.get('legs_visible')}；"
                       f"发 {p.get('hair')}；衣 {p.get('outfit')}；鞋 {p.get('shoes')}；视线 {p.get('gaze')}；表情 {p.get('expression')}")
    desc = (f"人数 {v.get('people_count')}；景别 {v.get('framing')}；场景 {v.get('scene')}\n" + "\n".join(ppl)).strip()
    zh = {k: c.get("zh", k) for k, c in {**(cfg().get("checks") or {}), **(cfg().get("video_checks") or {})}.items()}
    bad = [f"- [{'缺陷' if c['s'] == 'defect' else '拿不准'}] {k}（{zh.get(k, k)}）：{c['e']}"
           for k, c in (v.get("checks") or {}).items() if c["s"] in ("defect", "unsure")]
    return desc, "\n".join(bad) or "（本地看图模型没报缺陷）"
