"""
H3 FL2VA 提示词机检（SOP_production.md §9A.1 / §9A.3；规则编号 = docs/audit/rule_ids.md）。

只用标准库、不引用本项目任何模块：gpu3 的 shot_audit.py 从同步过去的仓库直接 import 这个文件。

    from ns_schema.h3_lint import lint_h3, frames_for_seconds
    results = lint_h3(prompt, frames=243, intimate=False)
    # -> [{"rule_id": "H3-A1", "sop_ref": "SOP §9A.1", "ok": True, "detail": "...", "msg": "...", "level": "block"}, ...]

只检查能机器判定的规则；需要 LLM / VLM 判定的（H3-A6、A9、A10、D8、S1-W5 等）不在这里。
可选参数给出时才检查对应规则：seconds（P-B1 对照表）、dialogue（H3-D3 逐字）、offscreen（H3-D5 画外台词）、
monologue（H3-D6 内心独白）。
"""
from __future__ import annotations

import math
import re

FPS = 24
MIN_FRAMES, MAX_FRAMES = 107, 362   # K4（production_rules_v3 §1.4）：4–15 s；4 s = 107 帧（原 124 = 5 s 起）
ALIGN_PREFIX = "How the reference pictures align with the target video"
FIELDS = ("integrated_multimodal_description", "overall_soundscape", "non_diegetic_music")
STYLE_OPENING = "Live-action, cinematic, shallow depth of field,"
OPEN_PHRASE = "established by Picture 1"
CLOSE_PHRASE = "established by Picture 2 at the end of the shot"
VO_PHRASE = "says in an off-screen voiceover"
# FIX（CHAIN F2，官方 base-en §2.1 / §3.1）：只给首帧（I2VA）的指令行（逐字，含尖括号 / 方括号）；
# 与 ns_render.h3_video.I2V_ALIGN_LINE 相同。I2VA 正文第一句锚定 <Picture 1>，全文不得出现 Picture 2
I2V_ALIGN_LINE = "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced."
OPEN_PHRASE_I2V = "established by <Picture 1>"
MODES = ("fl2va", "i2va")
MAX_CHARS = 7000

ALIGN_RE = re.compile(
    r"^How the reference pictures align with the target video — Picture 1 \(from Shot 1\) aligns with the "
    r"0\.00-second mark of the target video; Picture 2 \(from Shot (\d+)\) aligns with the (\d+\.\d{2})-second mark "
    r"of the target video\.$")
D_BLOCK_RE = re.compile(r"<d>(.*?)</d>", re.S)
D_OK_RE = re.compile(r"^\[[A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+)?\] \S.*$", re.S)
SPEAKER_RE = re.compile(r"\((S\d+(?:\s*,\s*S\d+)*)(?:\s*[,;][^)]*)?\)")
CJK_RE = re.compile(r"[㐀-鿿豈-﫿　-〿＀-￯]")
NEG_EN_RE = re.compile(r"\b(no|not|never|without|don't|doesn't|avoid|nothing|none|nobody|neither|nor)\b|n't\b", re.I)
NEG_ZH_RE = re.compile(r"[不无没别勿禁]")
# 配乐里的抽象情绪 / 情绪功能词（指南 §4.7：只写乐器、速度、节奏、强弱）
MOOD_EN_RE = re.compile(r"\b(sad|happy|romantic|tense|tension|emotional|melancholy|melancholic|uplifting|mood|moody|"
                        r"atmosphere|atmospheric|feeling|feelings|evokes?|conveys?|heartfelt|dramatic|mysterious|"
                        r"suspenseful|joyful|sorrowful|nostalgic|ominous|hopeful|sentimental|passionate|sensual|"
                        # FIX（CHAIN F5，官方 §4.7）：解释配乐“作用”的写法（ep02 10 镜 “…providing a restrained background layer”）
                        r"restrained|background layer|providing\b[^.]*?\blayer|underscor\w*|supporting the scene)\b",
                        re.I)
# 图像风格后缀 / 画质词（不属于 H3 正文；风格只写 STYLE_OPENING）
STYLE_TERM_RE = re.compile(
    r"(?<![A-Za-z0-9])\d{1,2}K(?![A-Za-z0-9])|(?<![A-Za-z0-9])\d{2,3}\s?mm(?![A-Za-z])|(?<![A-Za-z])f/\s?\d+(?:\.\d+)?|"
    r"(?<![A-Za-z])(?:Leica|Summilux)(?![A-Za-z])|\bmasterpiece\b|\bbest quality\b|"
    r"\bultra[- ]detailed\b|\bhigh(?:ly)? detailed\b|\bphotorealistic\b|\bRAW photo\b|\bbokeh\b|徕卡|大光圈|焦外|竖构图|9:16",
    re.I)
TIMESTAMP_RE = re.compile(r"^\[Shot 1\]\s*[\(\[]?\s*\d+(?:\.\d+)?\s*(?:s|sec|seconds|秒)?\s*[-–~]")
MALE_SOUND_RE = re.compile(r"\b(man|men|male|his|he|him|boyfriend|husband)\b", re.I)
SOP = "SOP §9A.1"


# ---------------------------------------------------------------- 帧数（§9A.3、§6 对照表）

def snap_frames(n: int) -> int:
    """向上对齐到 17k+5（节点 align_frame_count 的行为）"""
    n = max(5, int(n))
    while n % 17 != 5:
        n += 1
    return n


def frames_for_seconds(seconds: float) -> int:
    """剧本 **时长** → 合法帧数：ceil(秒×24) 向上对齐到 17k+5，最少 107（4 s）、最多 362（production_rules_v3 §1.4）"""
    n = snap_frames(max(MIN_FRAMES, math.ceil(float(seconds) * FPS - 1e-9)))
    return min(n, MAX_FRAMES)


def sss(frames: int) -> str:
    return f"{frames / FPS:.2f}"


def alignment_line(frames: int, last_shot: int = 1) -> str:
    return (f"{ALIGN_PREFIX} — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; "
            f"Picture 2 (from Shot {last_shot}) aligns with the {sss(frames)}-second mark of the target video.")


# ---------------------------------------------------------------- 解析

def parse_h3(prompt: str) -> dict:
    """拆成 {align, fields: {name: value}, order: [name...], layout_ok, layout_detail}。不抛异常。"""
    text = (prompt or "").replace("\r\n", "\n").strip("\n")
    lines = text.split("\n")
    align = lines[0] if lines else ""
    rest = text[len(align):]
    fields: dict[str, str] = {}
    order: list[str] = []
    for m in re.finditer(r"(?m)^([A-Za-z_][A-Za-z0-9_ ]*):[ \t]?", rest):
        name = m.group(1)
        if name.startswith("[") or " " in name.strip():
            continue
        order.append(name)
    # 按出现顺序切值
    marks = [(m.start(), m.end(), m.group(1)) for m in re.finditer(r"(?m)^([A-Za-z_][A-Za-z0-9_]*):[ \t]?", rest)]
    for i, (_, end, name) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else len(rest)
        fields.setdefault(name, rest[end:stop].strip())
    expected = (f"\n\n{FIELDS[0]}: {fields.get(FIELDS[0], '')}\n\n{FIELDS[1]}: {fields.get(FIELDS[1], '')}"
                f"\n\n{FIELDS[2]}: {fields.get(FIELDS[2], '')}")
    problems = []
    if not rest.startswith("\n\n") or rest.startswith("\n\n\n"):
        problems.append("对齐句后必须恰好空一行")
    if tuple(order) != FIELDS:
        problems.append(f"字段顺序/名称应为 {' → '.join(FIELDS)}，实际 {' → '.join(order) or '（无）'}")
    elif rest.rstrip() != expected:
        problems.append("字段之间必须各空一行、每个字段写成一段（字段值里不能有空行）")
    return {"align": align, "fields": fields, "order": order, "layout_ok": not problems,
            "layout_detail": "；".join(problems)}


def _outside_d(text: str) -> str:
    return D_BLOCK_RE.sub(" ", text or "")


def _sentences(text: str) -> list[str]:
    return [x for x in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if x.strip()]


def _r(rule_id: str, ok: bool, detail: str, level: str = "block", sop_ref: str = SOP) -> dict:
    return {"rule_id": rule_id, "sop_ref": sop_ref, "ok": bool(ok), "detail": detail, "msg": detail, "level": level}


# ---------------------------------------------------------------- 检查

def lint_h3(prompt: str, frames: int | None = None, intimate: bool = False, *, seconds: float | None = None,
            dialogue: list[str] | None = None, offscreen: list[str] | None = None,
            monologue: list[str] | None = None, mode: str = "fl2va") -> list[dict]:
    """返回 [{rule_id, sop_ref, ok, detail, msg, level}]；level = block | warn（同 rule_ids.md 的级别）。
    mode（FIX / CHAIN F2）：fl2va = 首尾帧（默认，原规则）；i2va = 只给首帧（官方 I2VA 指令行、<Picture 1> 锚定、
    全文不得出现 Picture 2）"""
    if mode not in MODES:
        raise ValueError(f"mode 只能是 {MODES}")
    out: list[dict] = []
    text = (prompt or "").replace("\r\n", "\n").strip("\n")
    p = parse_h3(text)
    f = p["fields"]
    body = f.get(FIELDS[0], "")
    sound = f.get(FIELDS[1], "")
    music = f.get(FIELDS[2], "")

    # H3-A1 对齐句（I2VA：官方指令行逐字）
    m = ALIGN_RE.match(p["align"]) if mode == "fl2va" else None
    if mode == "i2va":
        ok = p["align"] == I2V_ALIGN_LINE
        out.append(_r("H3-A1", ok, "I2VA 指令行正确" if ok else f"只给首帧时第一行必须逐字是 “{I2V_ALIGN_LINE}”"))
    elif not m:
        det = "第一行不是官方对齐句（逐字，包括破折号 —）" if p["align"].startswith(ALIGN_PREFIX) else \
            "第一行必须是对齐句 “How the reference pictures align with the target video — …”"
        out.append(_r("H3-A1", False, det))
    else:
        shot_n, s_val = int(m.group(1)), m.group(2)
        probs = []
        if shot_n != 1:
            probs.append(f"单镜头应写 Picture 2 (from Shot 1)，实际 Shot {shot_n}")
        if frames is not None and s_val != sss(frames):
            probs.append(f"S.SS = {s_val}，按 {frames} 帧应为 {sss(frames)}")
        out.append(_r("H3-A1", not probs, "；".join(probs) or f"对齐句正确（{s_val} s）"))

    # H3-A2 空行与字段
    out.append(_r("H3-A2", p["layout_ok"], p["layout_detail"] or "空行与三个字段顺序正确"))

    # H3-A3 单镜头、无时间戳
    shots = body.count("[Shot ")
    probs = []
    if shots != 1 or not body.startswith("[Shot 1]"):
        probs.append(f"正文必须以 [Shot 1] 开头且只有一个镜头（现有 {shots} 个 [Shot …]）")
    if TIMESTAMP_RE.match(body):
        probs.append("[Shot 1] 后面不写时间戳")
    out.append(_r("H3-A3", not probs, "；".join(probs) or "单镜头 [Shot 1]"))

    # H3-A4 开头 Picture 1 / 结尾收在 Picture 2（I2VA：第一句锚定 <Picture 1>，全文不得出现 Picture 2）
    sents = _sentences(_outside_d(body))
    probs = []
    if mode == "i2va":
        if not sents or not (OPEN_PHRASE_I2V in sents[0] or OPEN_PHRASE in sents[0]):
            probs.append(f"第一句要写 “…{OPEN_PHRASE_I2V}”")
        if "Picture 2" in text:
            probs.append("只给首帧（I2VA）时全文不得出现 Picture 2（没有这张图）")
        out.append(_r("H3-A4", not probs, "；".join(probs) or "首帧锚定、无 Picture 2"))
    elif not sents or OPEN_PHRASE not in sents[0]:
        probs.append(f"第一句要写 “…{OPEN_PHRASE}”")
    if mode == "fl2va":
        last = sents[-1] if sents else ""
        if CLOSE_PHRASE not in last or not re.search(r"\bsettles? into\b", last):
            probs.append(f"最后一句要写 “settles into … {CLOSE_PHRASE}.”")
        out.append(_r("H3-A4", not probs, "；".join(probs) or "首帧起、尾帧收"))

    # H3-A5 否定句 / 禁用清单（<d> 外；N/A 除外）
    if FIELDS[0] in f:
        scan = _outside_d(body) + "\n" + sound + ("\n" + music if music.strip() != "N/A" else "")
    else:                       # 没有官方字段（旧版拼接）：整段都算正文
        scan = _outside_d(text)
    scan = scan.replace("N/A", " ")
    hits = sorted({h.group(0).lower() for h in NEG_EN_RE.finditer(scan)} | set(NEG_ZH_RE.findall(scan)))
    out.append(_r("H3-A5", not hits, f"出现否定词：{'、'.join(hits)}（改成“画面里有什么”的正向写法）" if hits
                  else "没有否定句"))

    # H3-A7 英文正文：<d> 外不出现中文
    outside = _outside_d(text)
    cjk = len(CJK_RE.findall(outside))
    letters = [c for c in outside if not c.isspace()]
    ascii_ratio = (sum(1 for c in letters if ord(c) < 128) / len(letters)) if letters else 1.0
    out.append(_r("H3-A7", cjk == 0,
                  f"<d> 外有 {cjk} 个中文字符（ASCII 占比 {ascii_ratio:.0%}）：需要英文正文（剧本 v3 提供）" if cjk
                  else "正文为英文"))

    # H3-A8 风格开场 + 不带图像风格后缀
    probs = []
    if not body.startswith("[Shot 1] " + STYLE_OPENING):
        probs.append(f"[Shot 1] 后应以 “{STYLE_OPENING}” 开场")
    style_hits = sorted({h.group(0) for h in STYLE_TERM_RE.finditer(_outside_d(text))})
    if style_hits:
        probs.append(f"带了图像风格/画质词：{'、'.join(style_hits)}（H3 正文不拼图像后缀）")
    out.append(_r("H3-A8", not probs, "；".join(probs) or "风格开场正确"))

    # H3-A11 时长 4–15 s
    eff = frames / FPS if frames is not None else (float(m.group(2)) if m else None)   # I2VA 没有 S.SS：要给 frames
    if eff is None:
        out.append(_r("H3-A11", False, "无法得到时长（缺帧数且对齐句无 S.SS）"))
    else:
        # 项目方 2026-10-31“改成正常节奏”：动作量按正常速度在 N 秒内做完、最后约 1 秒仍在动（写作规则，机器只查时长）
        out.append(_r("H3-A11", 4 <= eff <= 15.1, f"时长 {eff:.2f} s（应在 4–15 s；动作量按正常速度做完，最后约 1 秒仍在动）"))

    # H3-A12 长度（K：官方 API 硬上限 “maximum of 7000 characters per text” → block，production_rules_v3 §1.5）
    out.append(_r("H3-A12", len(text) <= MAX_CHARS, f"{len(text)} 字符（上限 {MAX_CHARS}）"))

    # H3-X2（R3，warn）：首句与末句不放 <d>（H3 片头约 0.5 s 乱语；成片 0.3 s 交叉溶解吃掉首尾）
    # <d> \u91cc\u7684\u53e5\u53f7\u4e0d\u7b97\u65ad\u53e5\uff1b</d> \u4e4b\u540e\u7b97\u4e00\u53e5\u7ed3\u675f\uff08\u201c\u2026 says: <d>\u2026</d> She settles \u2026\u201d \u7684\u672b\u53e5\u4e0d\u542b\u53f0\u8bcd\uff09
    masked = D_BLOCK_RE.sub(lambda mm: re.sub(r"[.!?]", "\u2024", mm.group(0)), _description(text))
    sents = [x for part in re.split(r"(?<=</d>)\s+", masked) for x in _sentences(part)]
    edge = [i for i in ({0, len(sents) - 1} if sents else set()) if "<d>" in sents[i]]
    out.append(_r("H3-X2", not edge, "首句 / 末句里有 <d>（台词放在中间的句子）" if edge else "首尾句无台词",
                  level="warn", sop_ref="production_rules_v3 §1.5"))

    # H3-D1 <d> 只放语言标签和原文
    blocks = D_BLOCK_RE.findall(text)
    probs = []
    if text.count("<d>") != text.count("</d>") or len(blocks) != text.count("<d>"):
        probs.append("<d> 与 </d> 不成对")
    bad = [b for b in blocks if not D_OK_RE.match(b) or "<" in b]
    if bad:
        probs.append(f"{len(bad)} 处 <d> 不是 “<d>[Chinese] 原文</d>” 格式：{bad[0][:30]}")
    out.append(_r("H3-D1", not probs, "；".join(probs) or f"{len(blocks)} 处 <d> 格式正确"))

    # H3-D2 说话人编号在 <d> 前、按首次开口顺序
    probs, seen = [], []
    prev_end = 0
    for dm in D_BLOCK_RE.finditer(body):
        lead = body[prev_end:dm.start()]
        ids = SPEAKER_RE.findall(lead)
        if not ids:
            probs.append(f"<d>{dm.group(1)[:16]}…</d> 前没有 (S1)/(S2) 说话人编号")
        else:
            for sid in re.findall(r"S\d+", ids[-1]):
                if sid not in seen:
                    seen.append(sid)
        prev_end = dm.end()
    if seen and seen != [f"S{i + 1}" for i in range(len(seen))]:
        probs.append(f"编号应按首次开口顺序 S1、S2…，实际 {'、'.join(seen)}")
    out.append(_r("H3-D2", not probs, "；".join(probs) or (f"说话人 {'、'.join(seen)}" if seen else "本镜无台词")))

    spoken = [re.sub(r"^\[[^\]]*\]\s?", "", b) for b in blocks]
    # H3-D3 台词逐字
    if dialogue is not None:
        want = [x for x in dialogue if x and x.strip()]
        ok = spoken == want
        det = "台词与剧本逐字一致" if ok else \
            f"剧本 {len(want)} 句、提示词 {len(spoken)} 句，" + next(
                (f"第 {i + 1} 句不一致：剧本「{a}」≠ 提示词「{b}」" for i, (a, b) in enumerate(zip(want, spoken)) if a != b),
                "条数或顺序不一致")
        out.append(_r("H3-D3", ok, det))

    # H3-D4 不用双引号包台词。FIX（CHAIN F4，官方 §4.5）：<d> 外表示画面里可见文字的英文双引号放行
    # （A red neon sign reading "营业中" glows…）；只拦 <d> 里的引号、贴着 <d> / </d> 的引号（= 给台词加引号）
    q_in = sum(len(re.findall(r"[\"“”]", b)) for b in D_BLOCK_RE.findall(text))
    q_wrap = len(re.findall(r"[\"“”]\s*<d>|</d>\s*[\"“”]", text))
    # 没写 <d>、直接用引号写台词（“… says: "台词"”）同样拦
    q_said = len(re.findall(r"\b(?:says?|said|asks?|whispers?|replies|voiceover)\b(?:\s+\w+ly)?\s*:?\s*[\"“]", text, re.I))
    q = q_in + q_wrap + q_said
    out.append(_r("H3-D4", q == 0, f"台词带了 {q} 个双引号（会被烧成字幕）" if q else "台词没有双引号"))

    # H3-D5 画外音
    # 每个 <d> 的引导段 = 上一个 </d> 之后到本 <d> 之前（不跨到前一句台词）
    leads, prev_end = {}, 0
    for dm in D_BLOCK_RE.finditer(body):
        leads[dm.start()] = body[prev_end:dm.start()]
        prev_end = dm.end()
    vo_blocks = [dm for dm in D_BLOCK_RE.finditer(body) if "voiceover" in leads[dm.start()].lower()]
    probs = []
    for dm in vo_blocks:
        if VO_PHRASE not in leads[dm.start()]:
            probs.append("画外音要写 “says in an off-screen voiceover”")
        after = _sentences(body[dm.end():])
        if not after or not re.search(r"lips\b.*\bclosed", after[0], re.I):
            probs.append("</d> 后要紧跟画面内人物嘴唇闭合的句子（…lips remain closed）")
    for line in offscreen or []:
        idx = next((i for i, x in enumerate(spoken) if x == line), None)
        if idx is None or not any(re.sub(r"^\[[^\]]*\]\s?", "", dm.group(1)) == line for dm in vo_blocks):
            probs.append(f"剧本画外台词「{line[:12]}」没按画外音写")
    if vo_blocks or offscreen:
        out.append(_r("H3-D5", not probs, "；".join(dict.fromkeys(probs)) or "画外音写法正确"))

    # H3-D6 🎙 内心独白不进 H3
    if monologue is not None:
        leaked = []
        for mono in monologue:
            # 按标点切成小句；6 字以上的小句原样出现在提示词里才算泄漏（避免“惊喜”这类常用词误报）
            for piece in re.split(r"[。！？!?…—\n，,；;：:“”\"「」『』（）()]+", mono or ""):
                piece = piece.strip()
                if len(piece) >= 6 and piece in text:
                    leaked.append(piece)
        out.append(_r("H3-D6", not leaked, f"内心独白进了提示词：{leaked[0]}" if leaked else "独白未进提示词"))

    # H3-D7 发声人数（亲密镜 block；其余 warn）
    n_spk = len(seen)
    if intimate:
        probs = []
        if n_spk > 1:
            probs.append(f"亲密镜只让女主发声，现有 {n_spk} 个说话人")
        male = sorted({h.group(0).lower() for h in MALE_SOUND_RE.finditer(sound)})
        if male:
            probs.append(f"soundscape 里写了男性声音：{'、'.join(male)}")
        out.append(_r("H3-D7", not probs, "；".join(probs) or "亲密镜单人发声"))
    elif n_spk > 1:
        # 项目方 2026-10-30：“普通对话镜每镜只让一个人说话”（原为 warn）
        out.append(_r("H3-D7", False, f"{n_spk} 人发声：普通对话镜每镜只让一个人说话（项目方 2026-10-30）"))

    # H3-S1 soundscape：1–4 句英文、一段、无 <d>
    probs = []
    ss = _sentences(sound)
    if not 1 <= len(ss) <= 4:
        probs.append(f"应为 1–4 句，现有 {len(ss)} 句")
    if "\n" in sound:
        probs.append("要写成一段")
    if "<d>" in sound:
        probs.append("不写台词 <d>")
    if CJK_RE.search(sound):
        probs.append("要用英文")
    if sound.strip() == "N/A":
        probs = []
    out.append(_r("H3-S1", not probs, "；".join(probs) or f"{len(ss)} 句英文"))

    # H3-S2 soundscape 不写 N/A
    out.append(_r("H3-S2", bool(sound.strip()) and sound.strip() != "N/A",
                  "overall_soundscape 不能为空或 N/A" if not sound.strip() or sound.strip() == "N/A" else "有环境声"))

    # H3-S3 配乐（项目方 2026-10-30：进 H3，指南 §4.7）：N/A 或 1–3 句英文，写乐器/速度/节奏/强弱，不写抽象情绪
    m_txt = music.strip()
    if m_txt == "N/A":
        out.append(_r("H3-S3", True, "N/A（本镜无配乐）"))
    elif not m_txt:
        out.append(_r("H3-S3", False, "non_diegetic_music 不能为空：写 1–3 句英文配乐，或 N/A"))
    elif CJK_RE.search(m_txt) or "<d>" in m_txt or "\n" in m_txt:
        out.append(_r("H3-S3", False, "non_diegetic_music 要写成一段英文、不写台词 <d>"))
    else:
        ms = _sentences(m_txt)
        moods = sorted({h.group(0).lower() for h in MOOD_EN_RE.finditer(m_txt)})
        probs = ([f"应为 1–3 句，现有 {len(ms)} 句"] if not 1 <= len(ms) <= 3 else []) + \
            ([f"出现抽象情绪词：{'、'.join(moods)}（改写乐器、速度、节奏、强弱）"] if moods else [])
        out.append(_r("H3-S3", not probs, "；".join(probs) or f"{len(ms)} 句英文配乐",
                      level="warn" if probs else "block"))

    # P-B1 帧数网格
    if frames is not None:
        probs = []
        if frames % 17 != 5 or not MIN_FRAMES <= frames <= MAX_FRAMES:
            probs.append(f"{frames} 帧不在 17k+5 网格 / {MIN_FRAMES}–{MAX_FRAMES} 范围内（4–15 s）")
        if seconds is not None and frames != frames_for_seconds(seconds):
            probs.append(f"时长 {seconds:g} s 查表应为 {frames_for_seconds(seconds)} 帧，实际 {frames}")
        out.append(_r("P-B1", not probs, "；".join(probs) or f"{frames} 帧（{sss(frames)} s）",
                      sop_ref="SOP §9A.3；§6 表"))
    # 启发式（warn）：H3-A6 / A9 / A10 / D8 / D9（gpu3 合并进来的检查，见文件末尾）
    out.extend(lint_h3_extra(text, dialogue=dialogue))
    return out


def lint_ok(results: list[dict], overrides: set[str] | frozenset = frozenset()) -> bool:
    """block 级别全部通过（overrides = 有项目方原话豁免的 rule_id）"""
    return all(r["ok"] or r.get("level") != "block" or r["rule_id"] in overrides for r in results)


def failed_rules(results: list[dict], overrides: set[str] | frozenset = frozenset(), level: str = "block") -> list[str]:
    return [r["rule_id"] for r in results
            if not r["ok"] and r.get("level") == level and r["rule_id"] not in overrides]

# ---------------------------------------------------------------- 启发式规则（gpu3 提供，2026-10-30 合并）
# 来源 data/incoming/h3_lint_extra.py（sha256 1a6536e7…，gpu3 shot_audit 的补充检查），原样并入：
# H3-A6、H3-A9、H3-A10、H3-D8、H3-D9，全部 level=warn，等 claude-sop 锁定后再升 block。
# lint_h3 默认带上它们（frame_texts / cutoff / scenetrans 只有直接调 lint_h3_extra 时才能给）。

X_SPEAKER_RE = re.compile(r"\((S\d+(?:\s*,\s*S\d+)*)\)")   # gpu3 原版（比 SPEAKER_RE 严格）

# §9A.1 运镜词汇 [G]: Zoom In/Out, Push In/Pull Out, Pan Left/Right, Truck Left/Right, Tilt Up/Down,
# Pedestal Up/Down, Arc Shot, Tracking Shot, Static Shot — as natural verbs inside a sentence
CAMERA_MOVES = {
    "zoom": r"\bzoom(?:s|ing|ed)? (?:in|out)\b",
    "push in": r"\bpush(?:es|ing|ed)? in\b",
    "pull out": r"\bpull(?:s|ing|ed)? (?:out|back)\b",
    "pan": r"\bpan(?:s|ning|ned)? (?:left|right|across|to)\b",
    "truck": r"\btruck(?:s|ing|ed)? (?:left|right)\b",
    "tilt": r"\btilt(?:s|ing|ed)? (?:up|down)\b",
    "pedestal": r"\bpedestal(?:s|ing|ed)? (?:up|down)\b",
    "arc": r"\barc(?:s|ing|ed)? (?:around|shot)\b|\barc shot\b",
    "tracking": r"\btrack(?:s|ing|ed)? (?:alongside|with|her|him|them|beside|behind)\b|\btracking shot\b",
}
STATIC_RE = re.compile(r"\bstatic (?:medium |wide |close-up |close |full )?shot\b|\bthe camera (?:holds|stays) still\b", re.I)
CAMERA_SUBJ_RE = re.compile(r"\b(?:the )?camera\b", re.I)
OFFICIAL_AMP = re.compile(r"\bwith (?:small|large) amplitude\b|\bat (?:slow|fast) speed\b", re.I)
LOOSE_CAMERA_ADVERB = re.compile(r"\b(?:the )?camera\b[^.]{0,40}\b(slowly|quickly|gently|rapidly|slightly|smoothly|"
                                 r"gradually|steadily)\b", re.I)
STACKED_LABEL = re.compile(r"(?:^|[.;,]\s*)(?:camera(?: movement)?\s*:\s*)?\(?(?:Zoom In|Zoom Out|Push In|Pull Out|"
                           r"Pan Left|Pan Right|Truck Left|Truck Right|Tilt Up|Tilt Down|Pedestal Up|Pedestal Down|"
                           r"Arc Shot|Tracking Shot|Static Shot)\)?\s*(?:[.;]|$)")

# H3-A9: plot summary / abstract emotion / inner state words (not visible or audible)
ABSTRACT_RE = re.compile(
    r"\b(feel(?:s|ing|ings)?|felt|emotion(?:s|al|ally)?|realiz(?:e|es|ed|ing)|remember(?:s|ed|ing)?|memor(?:y|ies)|"
    r"think(?:s|ing)?|thought(?:s)?|wonder(?:s|ed|ing)?|decid(?:e|es|ed|ing)|hope(?:s|d)?|wish(?:es|ed)?|"
    r"secret(?:ly)?|innermost|inner (?:voice|thoughts?|world|feelings?|conflict|turmoil|self|struggle|peace|life|"
    r"desires?|monologue)|soul|regret(?:s|ful)?|guilt(?:y)?|shame|ashamed|longing|yearn(?:s|ing)?|"
    r"determin(?:ed|ation)|resolve[ds]?|destiny|fate|story|plot|meanwhile|years? (?:ago|later)|"
    r"symboli[sz](?:e|es|ing)|represent(?:s|ing)|meaning|deep down|in her mind|in his mind|knows? that|"
    r"because|so that|in order to)\b", re.I)

# H3-A6: words that make a sentence part of the motion path rather than a static description
DYNAMIC_RE = re.compile(
    r"\b(\w+s (?:up|down|in|out|forward|back|away|toward|towards|across|around|aside|closer)|then|as|while|until|"
    r"begins?|continues?|gradually|slowly|quickly|step(?:s|ping)?|walk(?:s|ing)?|turn(?:s|ing)?|lift(?:s|ing)?|"
    r"lower(?:s|ing)?|raise(?:s|ing)?|lean(?:s|ing)?|reach(?:es|ing)?|move(?:s|ing)?|glance(?:s|ing)?|"
    r"look(?:s|ing)?|smile(?:s|ing)?|open(?:s|ing)?|close(?:s|ing)?|pull(?:s|ing)?|push(?:es|ing)?|slide(?:s|ing)?|"
    r"rise(?:s|ing)?|fall(?:s|ing)?|sit(?:s|ting)?|stand(?:s|ing)? up|kneel(?:s|ing)?|bend(?:s|ing)?|"
    r"touch(?:es|ing)?|grab(?:s|bing)?|drop(?:s|ping)?|pick(?:s|ing)?|place(?:s|ing)?|shift(?:s|ing)?|"
    r"tighten(?:s|ing)?|brush(?:es|ing)?|adjust(?:s|ing)?|tuck(?:s|ing)?|press(?:es|ing)?|blink(?:s|ing)?|"
    r"nod(?:s|ding)?|shake(?:s|ing)?|sway(?:s|ing)?|approach(?:es|ing)?|hug(?:s|ging)?|notice(?:s|ing)?|"
    r"says?|whisper(?:s|ing)?|breathe(?:s)?|settles?|travel(?:s|ling|ing)?|tilts?|pans?|zooms?|trucks?|"
    r"pedestals?|arcs?|tracks?|pushes|pulls)\b", re.I)
STATIC_HINT_RE = re.compile(r"\b(wearing|dressed in|with (?:long|short|a|an|her|his)|is|are|has|have|there is|"
                            r"there are|background|room|walls?|light(?:ing)?|hair)\b", re.I)

AGE_RE = re.compile(r"\b(young|middle-aged|mature|older|elderly|teen\w*|adult|\d+-year-old|in (?:her|his) "
                    r"(?:twenties|thirties|forties|fifties))\b", re.I)
GENDER_RE = re.compile(r"\b(woman|man|girl|boy|female|male|lady|gentleman)\b", re.I)
PRESENCE_RE = re.compile(r"\b(on-screen|onscreen|off-screen|offscreen|in frame|in the frame|out of frame|"
                         r"voiceover|visible|in shot|unseen)\b", re.I)
TIMBRE_RE = re.compile(r"\b(\w+(?:, \w+)*,? (?:and )?(?:\w+ )?voice)\b", re.I)
PACE_RE = re.compile(r"\b(slow(?:ly)?|fast|quick(?:ly)?|unhurried|hurried|measured|rapid(?:ly)?|drawn-out|"
                     r"clipped|halting|breathless|leisurely|deliberate(?:ly)?|pace|tempo)\b", re.I)


def _rx(rule_id: str, ok: bool, detail: str, level: str = "warn", sop_ref: str = SOP) -> dict:
    return {"rule_id": rule_id, "sop_ref": sop_ref, "ok": bool(ok), "detail": detail, "msg": detail, "level": level}


def _outside_dx(text: str) -> str:
    return D_BLOCK_RE.sub(" ", text)


def _sentences_x(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+", text.strip()) if x.strip()]


def _description(prompt: str) -> str:
    m = re.search(r"integrated_multimodal_description:\s*(.*?)(?:\n\s*\n\s*overall_soundscape:|$)", prompt, re.S)
    return (m.group(1) if m else prompt).strip()


def camera_moves(text: str) -> list[str]:
    plain = _outside_dx(text)
    out = [k for k, rx in CAMERA_MOVES.items() if re.search(rx, plain, re.I)]
    if STATIC_RE.search(plain):
        out.append("static")
    return out


def _ngrams(text: str, n: int = 3) -> set:
    w = re.findall(r"[a-z]+", text.lower())
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def lint_h3_extra(prompt: str, *, frame_texts: list[str] | None = None, dialogue: list[str] | None = None,
                  cutoff: list[str] | None = None, scenetrans: list[str] | None = None,
                  a6_overlap: float = 0.5) -> list[dict]:
    """frame_texts: English descriptions of Picture 1 / Picture 2 (H3-A6 overlap; skipped when not given);
    cutoff / scenetrans: dialogue lines the script marks as cut off at the end of the shot / continuing across a cut."""
    out: list[dict] = []
    desc = _description(prompt)
    plain = _outside_dx(desc)
    sents = _sentences_x(plain)

    # H3-A6 the body is the motion path, not two static descriptions (one anchor sentence allowed)
    inner = sents[1:-1] if len(sents) > 2 else []
    static = [s for s in inner if not DYNAMIC_RE.search(s) and STATIC_HINT_RE.search(s)]
    probs = []
    if len(static) > 1:
        probs.append(f"{len(static)} static description sentences between the opening and closing: "
                     + " | ".join(s[:50] for s in static[:3]))
    for i, ft in enumerate(frame_texts or []):
        g = _ngrams(ft)
        if g:
            share = len(_ngrams(plain) & g) / len(g)
            if share > a6_overlap:
                probs.append(f"repeats Picture {i + 1}'s description ({share:.0%} of its 3-grams; threshold 待定)")
    out.append(_rx("H3-A6", not probs, "; ".join(probs) or "the body describes the motion path"))

    # H3-A9 visible / audible only: no plot summary, no abstract emotion words
    snd = re.search(r"overall_soundscape:\s*(.*?)(?:\n\s*\n|$)", prompt, re.S)
    hits = sorted({m.group(0).lower() for m in ABSTRACT_RE.finditer(plain + " " + (snd.group(1) if snd else ""))})
    out.append(_rx("H3-A9", not hits, f"not visible / audible: {hits}" if hits else "only visible / audible details"))

    # H3-A10 camera motion: official vocabulary, natural sentence, official amplitude / speed words, one move
    probs = []
    moves = camera_moves(desc)
    if len(moves) > 1:
        probs.append(f"{len(moves)} camera moves {moves} (one per shot)")
    if STACKED_LABEL.search(plain):
        probs.append("camera move stacked as a label; write it as a natural action in the sentence")
    loose = sorted({m.group(1).lower() for m in LOOSE_CAMERA_ADVERB.finditer(plain)})
    if loose:
        probs.append(f"camera speed / amplitude {loose}: use 'at slow speed' / 'at fast speed' / "
                     "'with small amplitude' / 'with large amplitude'")
    for s in sents:
        if CAMERA_SUBJ_RE.search(s) and not camera_moves(s) and re.search(r"\bcamera (?:\w+s)\b", s, re.I) \
                and not re.search(r"\bcamera (?:holds|stays|frames|captures|shows)\b", s, re.I):
            probs.append(f"camera verb outside the official vocabulary: {s[:60]!r}")
    out.append(_rx("H3-A10", not probs, "; ".join(probs) or f"camera: {moves or ['none']}"))

    # H3-D8 a speaker's first appearance: age, gender, on / off screen, timbre, pace
    probs = []
    seen = []
    for m in X_SPEAKER_RE.finditer(desc):
        for sid in re.split(r"\s*,\s*", m.group(1)):
            if sid in seen:
                continue
            seen.append(sid)
            start = max(desc.rfind(". ", 0, m.start()), desc.rfind("</d>", 0, m.start()))
            ctx = desc[start + 1:m.start()]
            miss = [k for k, rx in (("age", AGE_RE), ("gender", GENDER_RE), ("on/off screen", PRESENCE_RE),
                                    ("timbre", TIMBRE_RE), ("pace", PACE_RE)) if not rx.search(ctx)]
            if miss:
                probs.append(f"({sid}) first appears without {', '.join(miss)}")
    out.append(_rx("H3-D8", not probs, "; ".join(probs) or "every speaker introduced"))

    # H3-D9 <cutoff> / <scenetrans> exactly where the script marks them
    probs = []
    for tag, lines in (("<cutoff>", cutoff or []), ("<scenetrans>", scenetrans or [])):
        for line in lines:
            i = desc.find(line.strip())
            if i < 0:
                continue
            j = desc.find("</d>", i)
            near = desc[max(0, i - 60):(j + 60) if j >= 0 else i + 60]
            if tag not in near:
                probs.append(f"script marks {line[:16]!r} for {tag}; the tag is missing next to its <d>")
        if tag in desc and not lines:
            probs.append(f"{tag} used although the script marks no such line")
    if scenetrans and "<scenetrans>" in desc and not re.search(r"\b(continues?|carries) (?:over|across)\b", plain, re.I):
        probs.append("<scenetrans> without saying the audio continues across the cut")
    out.append(_rx("H3-D9", not probs, "; ".join(probs) or "no cutoff / scenetrans needed"))
    return out


# ---------------------------------------------------------------- 人物表锚点 / 声线（R3，production_rules_v3 §1.5）

def anchor_sentence(prompt: str) -> str:
    """integrated_multimodal_description 的第一句（锚点句）"""
    sents = _sentences(_description(prompt or ""))
    return sents[0] if sents else ""


def lint_h3_registry(prompt: str, *, anchors: list[tuple[str, str]] | None = None,
                     voices: list[tuple[str, str]] | None = None) -> list[dict]:
    """H3-X1：锚点句逐字包含每个“在画”角色的 EN外貌短语与当前造型 EN（anchors = [(说明, 短语)]，空短语跳过）；
    H3-X3：第 n 个说话人 (Sn) 第一次出现的那一句逐字包含其 VOICE 声线短语（voices = [(说明, 短语)]，按首次开口顺序）。
    只做字符串包含比对；不给就不查。"""
    out: list[dict] = []
    ref = "production_rules_v3 §1.5"
    if anchors is not None:
        first = anchor_sentence(prompt)
        miss = [f"{who}：{ph}" for who, ph in anchors if ph and ph not in first]
        out.append(_r("H3-X1", not miss, ("锚点句（首句）缺少：" + "；".join(miss)) if miss else "锚点句含人物表 EN 与造型 EN",
                      sop_ref=ref))
    if voices is not None:
        desc = _description(prompt or "")
        sents = _sentences(D_BLOCK_RE.sub(" <d></d> ", desc))
        miss = []
        for n, (who, ph) in enumerate(voices, start=1):
            if not ph:
                continue
            tag = re.compile(rf"\(S{n}\b")
            sent = next((x for x in sents if tag.search(x)), None)
            if sent is None or ph not in sent:
                miss.append(f"(S{n}) {who}：{ph}")
        out.append(_r("H3-X3", not miss, ("说话人首次出现的句子缺少 VOICE：" + "；".join(miss)) if miss
                      else "说话人首次出现含 VOICE 声线短语", sop_ref=ref))
    return out
