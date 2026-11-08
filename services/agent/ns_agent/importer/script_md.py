"""
确定性剧本导入器：解析《为他而骚》v2 的两份 Markdown。

- 剧本和镜头 v2.md：全局锁（角色锁、motion 后缀、基础负面、触发词表）+ 60 集 × 6 镜
- image提示词 v2.md：数据 B 装饰状态总表、数据 D 基准图注册表、数据 E LoRA 注册表

只做结构化，不改写任何提示词原文。格式异常写进 warnings，不抛异常。
"""
import hashlib
import json
import re
from pathlib import Path

from ns_schema.script import (
    CastEntry, CharacterConflict, CharacterEntry, CharacterLock, DecorRange, DialogueLine, Episode, Extra, LookLine,
    LoraTrigger, ReferenceImage, SceneConflict, SceneEntry, ScriptGlobals, ScriptImport, Shot, SourceFile, Voiceover,
)

# 封板修复 P0：“## ep03 《…》”“## EP03《…》”“## 🔴ep03《…》”“##ep03《…》”都认（旧写法整集被静默丢弃）
EP_RE = re.compile(r"^##\s*(?:🔴\s*)?ep\s*(\d+)\s*《(.+?)》(.*)$", re.I)
EP_LOOSE_RE = re.compile(r"^##\s*(?:🔴\s*)?ep\s*\d", re.I)
# 原稿里有手工修改留下的残缺格式（缺结尾 **、缺冒号），这里放宽匹配
SHOT_RE = re.compile(r"^\*\*镜(\d+)｜img(?:（(.*?)）)?(?:\*\*)?[：:]\s*(.*)$")
FIELD_RE = re.compile(r"^\*\*([^*：:`]+?)(?:\*\*)?`?(?:[：:]\s*(.*))?$")
PAREN_RE = re.compile(r"（(.*?)）")
# 一句台词：可选的连续括号 + 一对引号
# 括号（可多个）后可带冒号（QA-07）；或无括号的“名字：”（QA-08）；再接一对引号
LINE_RE = re.compile(r"(?P<parens>(?:（[^）]*）\s*)*)(?:[：:]\s*)?(?:(?P<name>[^\s“\"「（）：:，。／]{1,8})[：:]\s*)?[“\"「](?P<text>.*?)[”\"」]")
CODE_BLOCK_RE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
MD_ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")   # Markdown 反斜杠转义（ASCII 标点）

# 挂在镜头上的附加字段；其它不认识的字段挂在集上（比对用 _split_label 的 base：带括注的 label 同样认，label 原样保留）
# W7（11-03，A2 第 18 轮 P1-①）：W2 之前的旧集合。V1 / S2 等已归档剧编译冻结用它（ns_agent.frame_job.freeze_archived_extras）
LEGACY_SHOT_EXTRA_LABELS = frozenset({"对白屏幕上", "motion补充", "口型", "口型内容", "字幕卡"})


def _shot_spec_labels() -> frozenset:
    """镜头语言 v2 七段的 md_label（config/director/shot_spec_v2.yaml 单一来源；A2 第 18 轮 F3：原来只手写收了四段）"""
    f = Path(__file__).resolve().parents[4] / "config" / "director" / "shot_spec_v2.yaml"
    try:
        import yaml
        segs = (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("segments") or []
    except (OSError, ImportError, ValueError):   # arch-ok: 读不到 = 用下面同内容的兜底（test_w7 断言两者一致）
        segs = []
    labs = {str(sg.get("md_label") or "").strip("*：: ") for sg in segs if isinstance(sg, dict)} - {""}
    return frozenset(labs or SHOT_SPEC_FALLBACK)


SHOT_SPEC_FALLBACK = ("机位与景别", "镜头", "构图", "灯光", "运镜", "表演节拍", "声音")
# ISS-1103-031008-8c50（MERGE-IMG-O 11-03，L-148）：镜头语言 v2 七段 + 光线 / 分拍 归到本镜 extras（原来落进集级 extras，
# 编译 shot_language / frame_job.first_beat_gaze 拿不到），label 不变
SHOT_EXTRA_LABELS = set(LEGACY_SHOT_EXTRA_LABELS) | set(_shot_spec_labels()) | {"光线", "分拍", "光学"}   # 光学（K-WRT-816，CODER-PT 11-05）：焦段｜T｜高度=，prompt_template_v2.lens_of 先读它
# v3 每镜的 **H3提示词**：SOP §9A.1 官方 FL2VA 全文，多行（含空行），原样保留到下一个 **字段** / 镜头 / 集标题
H3_LABELS = {"H3提示词", "H3 提示词"}
H3_CHECK_LABELS = {"H3校验", "H3 校验"}   # 如 **H3校验**：暂缓（露骨内容）→ Shot.h3_check（原文）
SHOTS_PER_EPISODE = (6, 8)                   # 下限 6；上限 = shot_cap：max(8, 说话轮次 + 独白段数, ⌈总时长 ÷ 6⌉)（K1 + G29）
MIN_SHOT_SECONDS, MAX_SHOT_SECONDS = 4, 15   # 镜头时长（**时长**：N秒；K4：MiniMax-H3 官方 4–15 整数）
EPISODE_SECONDS = (60, 120)                  # 一集总时长通过范围（项目方 11-01 21:4x：“改成 60–120 秒”，L-092 / D-SCR-08）；⤴ 取代 K1 的 50–75（75–90 只提示）
EPISODE_SECONDS_WARN = 120                   # 不再有“只提示”带：60–120 之外一律不过
POSE_TRIGGER_RE = re.compile(r"pose_[A-Za-z0-9_]+")
EPISODE_COUNT = 60


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _unwrap(text: str) -> str:
    """字段值去 Markdown 标记、文字不动（QA-08：会进提示词）。与 gpu3 weita_render.clean 同口径（T04）：
    反斜杠转义还原（\\. → .、\\\\ → \\）；反引号、* 一律是包裹/强调标记（整段包裹、正文里的 **加粗** / *斜体*、残缺的单边符号）"""
    s = MD_ESCAPE_RE.sub(r"\1", text.strip())
    return s.replace("`", "").replace("*", "").strip()


def _split_label(label: str) -> tuple[str, str | None]:
    note = PAREN_RE.search(label)
    base = PAREN_RE.sub("", label).strip()
    return base, (note.group(1) if note else None)


def _leading_triggers(prompt: str, known: set[str]) -> list[str]:
    """开头连续的触发词：触发词表里的，或姿势触发词 pose_<名称>（v3，名称见 docs/pose_library.md）"""
    found = []
    for token in re.split(r"[，,]", prompt):
        token = token.strip()
        if token in known or POSE_TRIGGER_RE.fullmatch(token):
            found.append(token)
        else:
            break
    return found


def _parse_dialogue(raw: str) -> list[DialogueLine]:
    """只返回说出口的台词（有引号的段）；没有引号的段见 _split_dialogue"""
    return _split_dialogue(raw)[0]


def _split_dialogue(raw: str) -> tuple[list[DialogueLine], list[str]]:
    """按引号对切分：一段“（括号…）”+“台词”算一句。
    原稿有时两句之间不写“／”（“…”（他）“…”），也有连续多个括号（（接通）（她，很小声））：
    紧挨台词的最后一个括号是说话人，更前面的括号是舞台提示。
    没有引号的段（“（沉默三秒）”“—”“无台词”）不是台词（契约 §2.5；gpu3 也不当台词，T04）：
    原文放进第二个返回值（Shot.dialogue_stage），校验器据此报 S1-W4。"""
    lines, stage = [], []
    for part in _split_outside_quotes(raw.replace("**", ""), "／"):
        part = part.strip()
        if not part:
            continue
        matches = list(LINE_RE.finditer(part))
        if not matches:
            stage.append(part)
            continue
        for m in matches:
            notes = PAREN_RE.findall(m.group("parens") or "")
            # “他：“…””这种无括号写法（QA-08）：名字当说话人
            if not notes and m.group("name"):
                notes = [m.group("name").strip()]
            lines.append(DialogueLine(speaker_note=notes[-1] if notes else None, text=m.group("text"),
                                      stage="；".join(notes[:-1]) or None))
    return lines, stage


def _split_outside_quotes(text: str, sep: str) -> list[str]:
    """只在引号外切分（QA-06：台词里的“／”是停顿，不是分句）"""
    # 封板修复 P2：ASCII 双引号开 / 关是同一个字符，单独按开关计；只在成对时才认（不成对的“5"”、混用“"…””不影响切分）
    use_ascii = text.count('"') % 2 == 0
    parts, buf, depth, ascii_open = [], [], 0, False
    for ch in text:
        if ch in "“「":
            depth += 1
        elif ch in "”」" and depth:
            depth -= 1
        elif ch == '"' and use_ascii:
            ascii_open = not ascii_open
        if ch == sep and depth == 0 and not ascii_open:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _parse_strength(raw: str | None) -> tuple[float, float] | None:
    if not raw:
        return None
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", raw)]
    if len(nums) == 1:
        return (nums[0], nums[0])
    return (nums[0], nums[1]) if len(nums) >= 2 else None


# ------------------------------------------------------------------ 全局部分

def _code_block_after(text: str, marker: str) -> str | None:
    i = text.find(marker)
    if i < 0:
        return None
    m = CODE_BLOCK_RE.search(text, i)
    return m.group(1) if m else None


def _inline_code_after(text: str, marker: str) -> str:
    for line in text.splitlines():
        if marker in line:
            m = re.search(r"`([^`]+)`", line)
            if m:
                return m.group(1).strip()
    return ""


# 与 gpu3 ns-studio/scripts/weita_render.py 的 md 解析器逐字一致（loader_parity 对比）：
# 集外（批标题 / “# ” 段之后、下一个“## epNN”之前）的“**林晚日常穿搭…**：…”与“| epNN | … |”表格行
_W_EP_RE = re.compile(r"^##\s*ep(\d+)《(.+?)》(.*)$")
_W_BATCH_RE = re.compile(r"^#\s*《为他而骚》第(.+?)批")
_W_FIELD_RE = re.compile(r"^\*\*(.+?)\*\*\s*[：:]\s*(.*)$")
_W_ROW_RE = re.compile(r"^\|\s*ep(\d{2})\s*\|(.*)\|\s*$")
_W_HEAD_RE = re.compile(r"^\|\s*集\s*\|(.*)\|\s*$")


def _w_unescape(s: str) -> str:
    return re.sub(r"\\(.)", r"\1", s)


def _w_clean(v: str) -> str:
    v = _w_unescape(v.strip())
    if m := re.fullmatch(r"`([^`]*)`", v):
        return m.group(1).replace("*", "").strip()
    if m := re.fullmatch(r"\*([^*]+)\*", v):
        return m.group(1).strip()
    return v.replace("`", "").replace("*", "").strip()


def _owner_headers(script: str) -> tuple[str, dict[str, str]]:
    """（daily_outfit, decor_tables）：林晚日常穿搭整行；各批装饰连续性自查表按集一行（表头：值；…）"""
    outfit, decor, heads = "", {}, []
    in_ep = in_code = False
    for raw in script.splitlines():
        ln = raw.strip()
        if ln.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not ln or ln == "---":
            continue
        if _W_BATCH_RE.match(ln) or (ln.startswith("# ") and not _W_EP_RE.match(ln)):
            in_ep = False
            continue
        if _W_EP_RE.match(ln):
            in_ep = True
            continue
        if in_ep:
            continue
        if m := _W_HEAD_RE.match(ln):
            heads = [h.strip() for h in _w_unescape(m.group(1)).split("|")]
            continue
        if m := _W_ROW_RE.match(ln):
            cells = [c.replace("*", "").replace("🔴", "").strip() for c in _w_unescape(m.group(2)).split("|")]
            decor[str(int(m.group(1)))] = ("；".join(f"{h}：{c}" for h, c in zip(heads, cells))
                                           if heads and len(heads) == len(cells) else "；".join(cells))
            continue
        if ln.startswith(r"\*\*"):
            ln = "**" + ln[4:]
        if (m := _W_FIELD_RE.match(ln)) and _w_unescape(m.group(1)).strip().startswith("林晚日常穿搭"):
            outfit = _w_clean(m.group(2))
    return outfit, decor


def _parse_globals(script: str, image_doc: str | None, warnings: list[str]) -> ScriptGlobals:
    g = ScriptGlobals()
    g.daily_outfit, g.decor_tables = _owner_headers(script)

    block = _code_block_after(script, "每条prompt统一前缀")
    for line in (block or "").splitlines():
        m = re.match(r"^\s*【(.+?)】(.+)$", line)
        if m:
            g.character_locks.append(CharacterLock(name=m.group(1), description=m.group(2).strip()))
    if not g.character_locks:
        warnings.append("没有找到角色锁（每条prompt统一前缀）")

    g.motion_suffix = _inline_code_after(script, "每条motion统一后缀")
    g.image_suffix = _inline_code_after(script, "每条img统一后缀")
    g.base_negative = _inline_code_after(script, "基础负面")

    triggers: dict[str, LoraTrigger] = {}
    kind = None
    for line in (_code_block_after(script, "LoRA 触发词表") or "").splitlines():
        if line.startswith("图像LoRA"):
            kind = "image"
        elif line.startswith("视频LoRA"):
            kind = "video"
        elif kind and (m := re.match(r"^\s+([A-Za-z0-9_]+)\s+(.+)$", line)):
            triggers[m.group(1)] = LoraTrigger(trigger=m.group(1), kind=kind, description=m.group(2).strip())
        elif line.strip() and not line.startswith(" "):
            kind = None

    if image_doc:
        _merge_image_doc(g, image_doc, triggers, warnings)
    g.lora_triggers = list(triggers.values())
    return g


def _json_block(doc: str, marker: str, warnings: list[str]):
    block = _code_block_after(doc, marker)
    if block is None:
        warnings.append(f"image提示词：没有找到 {marker}")
        return None
    try:
        return json.loads(block)
    except json.JSONDecodeError as e:
        warnings.append(f"image提示词：{marker} 不是合法 JSON：{e}")
        return None


def _merge_image_doc(g: ScriptGlobals, doc: str, triggers: dict[str, LoraTrigger], warnings: list[str]):
    # 数据 B 装饰状态总表
    block = _code_block_after(doc, "数据B")
    for part in re.split(r"[｜\n]", block or ""):
        m = re.match(r"^\s*ep(\d+)(?:-(\d+))?:(D\d+|DN)(.*)$", part.strip())
        if not m:
            continue
        first = int(m.group(1))
        last = int(m.group(2) or first)
        note = m.group(4).strip()
        overrides = {int(ep): st for ep, st in re.findall(r"(\d+)为(D\d+|DN)", note)}
        g.decor_schedule.append(DecorRange(first_ep=first, last_ep=last, state=m.group(3),
                                           note=note, overrides=overrides))
    if not g.decor_schedule:
        warnings.append("image提示词：数据B 装饰状态总表为空")

    for item in _json_block(doc, "数据D", warnings) or []:
        g.references.append(ReferenceImage(id=item["id"], description=item.get("desc", ""),
                                           usage=item.get("用途")))

    for item in _json_block(doc, "数据E", warnings) or []:
        t = item["trigger"]
        kind = "video" if "视频" in item.get("类型", "") else "image"
        entry = triggers.get(t) or LoraTrigger(trigger=t, kind=kind, description=item.get("内容", ""))
        entry.file = item.get("文件")
        entry.strength = _parse_strength(item.get("强度"))
        entry.starts = item.get("起用")
        triggers[t] = entry
    for t in triggers.values():
        if t.file is None:
            warnings.append(f"触发词 {t.trigger} 在数据E里没有对应的 LoRA 文件")


def decor_state_for(schedule: list[DecorRange], ep: int) -> str | None:
    for r in schedule:
        if r.first_ep <= ep <= r.last_ep:
            return r.overrides.get(ep, r.state)
    return None


# ------------------------------------------------------------------ 登记表（场景表 S1 fredw-92/S-back；人物表 R3）
# 格式（docs/qwen_writing_contract.md §1.2 / §1.3；production_rules_v3 §1.1）：
#   **全剧场景表**：  /  **全剧人物表**：      ← 全局头（第一个 ## ep 之前）
#   **本集场景表**：  /  **本集人物表**：      ← 集内（**配乐** 之后、镜1 之前），只写本集新增的场景 / 新登场的角色
#   | 场景id | 名称 | 空间描述 | 时段 | 光线 | 固定陈设 |
#   | 角色 | 角色id | 中文外貌句 | EN外貌短语 | VOICE声线短语 |
#   |---|---|…|
# 每镜：**场景**：<场景id>｜<本镜场景细节>
SCENE_COLUMNS = ("场景id", "名称", "空间描述", "时段", "光线", "固定陈设")
SCENE_FIELDS = ("scene_id", "name", "space", "time_of_day", "light", "fixtures")
SCENE_FIELD_LABEL = dict(zip(SCENE_FIELDS, SCENE_COLUMNS))
SCENE_TABLE_LABEL_RE = re.compile(r"^\*\*(全剧场景表|本集场景表)\*\*\s*[：:]?\s*$")
SCENE_ID_RE = re.compile(r"^sk_[a-z0-9_]+$")
SCENE_LABELS = {"场景"}
CHARACTER_COLUMNS = ("角色", "角色id", "中文外貌句", "EN外貌短语", "VOICE声线短语")
CHARACTER_FIELDS = ("name", "char_id", "zh", "en", "voice")
CHARACTER_FIELD_LABEL = dict(zip(CHARACTER_FIELDS, CHARACTER_COLUMNS))
CHARACTER_TABLE_LABEL_RE = re.compile(r"^\*\*(全剧人物表|本集人物表)\*\*\s*[：:]?\s*$")
CHARACTER_ID_RE = re.compile(r"^(ch|lead)_[a-z0-9_]+$")   # V2（L-126）：项目方指定资产的女主 id = lead_f（CONSENT 总调度 fredw-2b 19:2x）
_TABLE_SEP_RE = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_PIPE_SPLIT_RE = re.compile(r"(?<!\\)\|")


def _table_cells(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [_unwrap(c.replace("\\|", "|")) for c in _PIPE_SPLIT_RE.split(body)]


def _norm_head(c: str) -> str:
    return c.replace(" ", "").replace("ID", "id").replace("Id", "id")


def registry_tables(script: str, label_re: re.Pattern, columns: tuple[str, ...], fields: tuple[str, ...],
                    kind: str) -> tuple[list[dict], list[dict], set[int]]:
    """扫全文的登记表（kind = 场景 / 人物）→ (定义行 [{episode: None|N, cells: {field: 原文}, line}],
    格式问题 [{episode, line, detail}], 被表格占用的行号)。代码块里的行不算；集外的“本集…表”记为问题"""
    defs, problems, used = [], [], set()
    ep: int | None = None
    mode, scope, in_code = None, None, False
    whole, local = f"全剧{kind}表", f"本集{kind}表"
    n = len(columns)
    for lineno, raw in enumerate(script.splitlines(), start=1):
        line = raw.rstrip().replace(r"\*", "*").replace(r"\`", "`")
        st = line.strip()
        if st.startswith("```"):
            in_code = not in_code
            mode = None
            continue
        if in_code:
            continue
        if m := EP_RE.match(line):
            ep, mode = int(m.group(1)), None
            continue
        if line.startswith("# ") or line.startswith("## "):
            ep, mode = None, None
            continue
        if m := label_re.match(st):
            scope = None if m.group(1) == whole else ep
            if m.group(1) == local and ep is None:
                problems.append({"episode": None, "line": lineno,
                                 "detail": f"“{local}”写在了集外（应在 ## ep… 之后、镜1 之前）"})
            mode = "head"
            used.add(lineno)
            continue
        if mode is None:
            continue
        if not st:
            if mode == "head":
                used.add(lineno)         # 标签与表头之间可以空一行
                continue
            mode = None
            continue
        if not st.startswith("|"):
            if mode == "head":
                problems.append({"episode": scope, "line": lineno,
                                 "detail": f"{kind}表标签后面没有表格（应为 | {' | '.join(columns[:2])} | … |）"})
            mode = None
            continue
        used.add(lineno)
        cells = _table_cells(st)
        if mode == "head":
            if tuple(_norm_head(c) for c in cells) != tuple(_norm_head(c) for c in columns):
                problems.append({"episode": scope, "line": lineno,
                                 "detail": f"{kind}表表头应为 | {' | '.join(columns)} |，现为 | {' | '.join(cells)} |"})
            mode = "sep"
            continue
        if mode == "sep":
            mode = "rows"
            if _TABLE_SEP_RE.match(st):
                continue
            problems.append({"episode": scope, "line": lineno,
                             "detail": f"{kind}表表头下一行应为分隔行 |{'---|' * n}"})
        if len(cells) != n:
            problems.append({"episode": scope, "line": lineno,
                             "detail": f"{kind}表这一行有 {len(cells)} 格（应为 {n} 格；格内不要用 |）：{st[:40]}"})
            continue
        defs.append({"episode": scope, "cells": dict(zip(fields, cells)), "line": lineno})
    return defs, problems, used


def scene_tables(script: str) -> tuple[list[dict], list[dict], set[int]]:
    """场景表（S1）：见 registry_tables"""
    return registry_tables(script, SCENE_TABLE_LABEL_RE, SCENE_COLUMNS, SCENE_FIELDS, "场景")


def character_tables(script: str) -> tuple[list[dict], list[dict], set[int]]:
    """人物表（R3，production_rules_v3 §1.1）：见 registry_tables"""
    return registry_tables(script, CHARACTER_TABLE_LABEL_RE, CHARACTER_COLUMNS, CHARACTER_FIELDS, "人物")


def build_character_registry(defs: list[dict]) -> list[CharacterEntry]:
    """按角色名合并：第一次定义为准；后面的定义空格 = 沿用，非空且不同 = 冲突（QW-P5，记下，不改原值）"""
    reg: dict[str, CharacterEntry] = {}
    for d in defs:
        c = d["cells"]
        name = c["name"]
        if not name:
            continue
        e = reg.get(name)
        if e is None:
            reg[name] = CharacterEntry(**c, defined_in=[d["episode"]], source_line=d["line"])
            continue
        e.defined_in.append(d["episode"])
        for f in CHARACTER_FIELDS[1:]:
            old, new = getattr(e, f), c[f]
            if new and not old:
                setattr(e, f, new)
            elif new and old and new != old:
                e.conflicts.append(CharacterConflict(field=CHARACTER_FIELD_LABEL[f], first=old, other=new,
                                                     episode=d["episode"], source_line=d["line"]))
    return list(reg.values())


def build_scene_library(defs: list[dict]) -> list[SceneEntry]:
    """按场景 id 合并：第一次定义为准；后面的定义空格不算改写（沿用），非空且不同 = 冲突（记下，不改原值）"""
    lib: dict[str, SceneEntry] = {}
    for d in defs:
        c = d["cells"]
        sid = c["scene_id"]
        if not sid:
            continue
        e = lib.get(sid)
        if e is None:
            lib[sid] = SceneEntry(**c, defined_in=[d["episode"]], source_line=d["line"])
            continue
        e.defined_in.append(d["episode"])
        for f in SCENE_FIELDS[1:]:
            old, new = getattr(e, f), c[f]
            if new and not old:
                setattr(e, f, new)
            elif new and old and new != old:
                e.conflicts.append(SceneConflict(field=SCENE_FIELD_LABEL[f], first=old, other=new,
                                                 episode=d["episode"], source_line=d["line"]))
    return list(lib.values())


def split_scene_line(value: str) -> tuple[str | None, str | None]:
    """**场景** 的值：<场景id>｜<本镜场景细节>（全角｜；半角 | 也认）"""
    v = value.strip()
    if not v:
        return None, None
    for sep in ("｜", "|"):
        if sep in v:
            sid, detail = v.split(sep, 1)
            return sid.strip() or None, detail.strip() or None
    return v, None


# ------------------------------------------------------------------ v3 集头 / 每镜行（R3，production_rules_v3 §1.2–1.3）
LOOK_LABEL_RE = re.compile(r"^造型[｜|]\s*(.+?)\s*(?:[｜|]\s*镜\s*(\d+)\s*起)?\s*$")
CAST_ITEM_RE = re.compile(r"^(.+?)（(.*)）$")
NAME_SPLIT_RE = re.compile(r"[、，,]")
LINK_VALUES = ("接力", "同场换机位", "换场", "接上一集")


def _split_outside_parens(text: str, seps: str = "、，,") -> list[str]:
    out, buf, depth = [], [], 0
    for ch in text:
        if ch in "（(":
            depth += 1
        elif ch in "）)" and depth:
            depth -= 1
        if ch in seps and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return [x.strip() for x in out if x.strip()]


def split_names(value: str) -> list[str]:
    """**出场人物** / **用到的场景**：顿号（或逗号）分隔"""
    return [x.strip() for x in NAME_SPLIT_RE.split(value or "") if x.strip()]


def parse_cast(value: str) -> list[CastEntry]:
    """**人物**：<名>（在画｜左｜前）、<名>（局部｜手｜右）、<名>（画外）。格式不对的项也保留（presence 原文），由校验器报"""
    out = []
    for item in _split_outside_parens(value):
        m = CAST_ITEM_RE.match(item)
        if not m:
            out.append(CastEntry(name=item, raw=item))
            continue
        parts = [x.strip() for x in re.split(r"[｜|]", m.group(2))]
        e = CastEntry(name=m.group(1).strip(), presence=parts[0], raw=item)
        if e.presence == "在画":
            e.x, e.depth = (parts + [None, None])[1:3]
        elif e.presence == "局部":
            e.part, e.x = (parts + [None, None])[1:3]
        out.append(e)
    return out


def split_link(value: str) -> tuple[str | None, str | None]:
    """**衔接**：<接力/同场换机位/换场/接上一集>｜承：<延续项>"""
    v = (value or "").strip()
    if not v:
        return None, None
    head, rest = re.split(r"[｜|]", v, maxsplit=1) if re.search(r"[｜|]", v) else (v, "")
    carry = re.sub(r"^承\s*[：:]\s*", "", rest.strip()).strip()
    return head.strip() or None, carry or None


def parse_look(character: str, from_shot: int | None, value: str, lineno: int) -> LookLine:
    """**造型｜<角色名>**：服装=<…>｜鞋=<…>｜…｜EN=<…>（槽位按原文中文名保存；EN 单列）"""
    look = LookLine(character=character, from_shot=from_shot, raw=value, source_line=lineno)
    for part in re.split(r"[｜|]", value or ""):
        part = part.strip()
        if not part:
            continue
        k, sep, v = part.partition("=") if "=" in part else part.partition("＝")
        if not sep:
            look.slots.setdefault("?", part)       # 没有“槽位=”：原文留在 "?"，校验器报 QW-E6
            continue
        k, v = k.strip(), v.strip()
        if k.upper() == "EN":
            look.en = v
        else:
            look.slots[k] = v
    return look


def first_word(note: str | None) -> str | None:
    """说话人括号 / 🎙 括号的第一个词（逗号、顿号、空格之前）"""
    if not note:
        return None
    w = re.split(r"[，,、\s]", note.strip(), maxsplit=1)[0].strip()
    return w or None


# ------------------------------------------------------------------ 分集 / 分镜

def _parse_episodes(script: str, known_triggers: set[str], warnings: list[str],
                    skip_lines: set[int] | None = None) -> list[Episode]:
    skip_lines = skip_lines or set()
    episodes: list[Episode] = []
    ep: Episode | None = None
    shot: Shot | None = None
    # 当前字段的写入目标，用来接续多行内容
    target: tuple[object, str] | None = None
    # 正在收集的 **H3提示词**（多行、含空行，逐字）
    h3_shot: Shot | None = None
    h3_lines: list[str] = []

    def finish_h3():
        nonlocal h3_shot, h3_lines
        if h3_shot is not None:
            h3_shot.h3_prompt = "\n".join(h3_lines).strip("\n") or None
        h3_shot, h3_lines = None, []

    def write(obj, attr, value, append=False):
        if attr == "_extra":
            obj.text = (obj.text + "\n" + value).strip() if append else value
        elif attr == "voiceover":
            vo = obj.voiceover
            vo.text = (vo.text + "\n" + value).strip() if append else value
        else:
            old = getattr(obj, attr) or ""
            setattr(obj, attr, (old + "\n" + value).strip() if append else value)

    for lineno, raw in enumerate(script.splitlines(), start=1):
        # 原稿部分行是转义过的 Markdown（\*\*镜2…），先还原
        line = raw.rstrip().replace(r"\*", "*").replace(r"\`", "`")

        if h3_shot is not None:
            if (SHOT_RE.match(line) or FIELD_RE.match(line) or EP_RE.match(line) or line.startswith("# ")
                    or line.startswith("## ") or line.strip() == "---"):
                finish_h3()      # 字段结束，本行照常处理
            else:
                if not line.strip().startswith("```"):   # 允许用代码块包起来
                    h3_lines.append(raw.rstrip())        # 代码块里逐字（不做 Markdown 反转义，与 gpu3 同，S1-W9）
                continue

        if m := EP_RE.match(line):
            tail = m.group(3).strip()
            # 封板修复 P0：🔴 写在整行任何位置都算（曾写进书名号里就漏判成人集）
            ep = Episode(number=int(m.group(1)), title=m.group(2).replace("🔴", "").strip(), high_intensity="🔴" in line,
                         tag=tail.replace("🔴", "").strip(), source_line=lineno)
            episodes.append(ep)
            shot, target = None, None
            continue
        if EP_LOOSE_RE.match(line):
            warnings.append(f"第 {lineno} 行像集标题但格式不对（应为“## epNN《标题》”），这一集没有导入：{line[:40]}")
        if line.startswith("# ") or line.startswith("## ") or EP_LOOSE_RE.match(line):
            ep, shot, target = None, None, None
            continue
        if ep is None:
            continue
        if lineno in skip_lines:        # 场景表（scene_tables 已解析）
            target = None
            continue
        if not line.strip() or line.strip() == "---":
            target = None
            continue

        if m := SHOT_RE.match(line):
            shot = Shot(episode=ep.number, index=int(m.group(1)), img_note=m.group(2),
                        img_prompt=_unwrap(m.group(3)), source_line=lineno)
            ep.shots.append(shot)
            target = (shot, "img_prompt")
            continue

        if m := FIELD_RE.match(line):
            label, value = m.group(1), _unwrap(m.group(2) or "")
            base, note = _split_label(label)
            if base == "剧情":
                ep.synopsis, target = value, (ep, "synopsis")
            elif base == "配乐" and shot is None:
                ep.music, target = value, (ep, "music")
            elif base == "出场人物" and shot is None:          # R3
                ep.cast, target = split_names(value), None
            elif base == "用到的场景" and shot is None:        # R3
                ep.scene_ids, target = split_names(value), None
            elif shot is None and (lm := LOOK_LABEL_RE.match(base)):   # R3：**造型｜<角色名>[｜镜N起]**
                ep.looks.append(parse_look(lm.group(1), int(lm.group(2)) if lm.group(2) else None, value, lineno))
                target = None
            elif shot is not None and base == "人物":           # R3：每镜人物行
                shot.cast_raw, shot.cast = value, parse_cast(value)
                target = None
            elif shot is not None and base == "衔接":           # R3：每镜衔接行
                shot.link, shot.link_carry = split_link(value)
                target = None
            elif shot is not None and base in H3_LABELS:
                first = (m.group(2) or "").strip()
                h3_shot, h3_lines = shot, ([first] if first and not first.startswith("```") else [])
                target = None
            elif shot is not None and base in H3_CHECK_LABELS:
                shot.h3_check, target = value or None, (shot, "h3_check")
            elif shot is not None and base in SCENE_LABELS:
                shot.scene_id, shot.scene_detail = split_scene_line(value)
                target = None
            elif base == "负面追加":
                ep.negative_extra, target = value, (ep, "negative_extra")
            elif shot is None or base not in {"motion", "sound", "🎙", "对白", "时长"} | SHOT_EXTRA_LABELS:
                extra = Extra(label=label, text=value)
                ep.extras.append(extra)
                target = (extra, "_extra")
            elif base == "时长":
                m_sec = re.search(r"\d+", value)
                if m_sec and MIN_SHOT_SECONDS <= int(m_sec.group()) <= MAX_SHOT_SECONDS:
                    shot.duration, shot.duration_source = int(m_sec.group()), "script"
                else:   # K4：不认的时长 duration_source 仍为 default，校验器 S1-W2 报 block
                    warnings.append(f"{shot.id} 时长无法识别或超出 {MIN_SHOT_SECONDS}–{MAX_SHOT_SECONDS} 秒：{value}")
                target = None
            elif base == "motion":
                shot.motion_prompt, target = value, (shot, "motion_prompt")
            elif base == "sound":
                shot.sound, target = value, (shot, "sound")
            elif base == "🎙":
                shot.voiceover = Voiceover(note=note, text=value)
                target = (shot, "voiceover")
            elif base == "对白":
                # "对白（无台词…）" 这种空标签不能覆盖已有台词
                if value:
                    shot.dialogue_raw = f"{shot.dialogue_raw}／{value}" if shot.dialogue_raw else value
                target = (shot, "dialogue_raw")
            else:
                extra = Extra(label=label, text=value)
                shot.extras.append(extra)
                target = (extra, "_extra")
            continue

        # 续行：接到上一个字段
        if target is not None:
            write(*target, _unwrap(line), append=True)
        else:
            warnings.append(f"第 {lineno} 行无法归属：{line[:40]}")

    finish_h3()
    for ep in episodes:
        for s in ep.shots:
            s.img_triggers = _leading_triggers(s.img_prompt, known_triggers)
            s.motion_triggers = _leading_triggers(s.motion_prompt, known_triggers)
            if s.dialogue_raw:
                s.dialogue, s.dialogue_stage = _split_dialogue(s.dialogue_raw)
                for d in s.dialogue:
                    d.speaker = first_word(d.speaker_note)
            if s.voiceover is not None:
                s.voiceover.speaker = first_word(s.voiceover.note)
            s.sound_intimate, s.sound_h3 = _sound_blocks(s.sound)
            s.music_cue = _block(s.sound, "配乐")
        ep.music_bgm = _block(ep.music, "BGM")
    return episodes


SOUND_TAGS = ("亲密声", "配乐", "H3 audio", "BGM")


def _block(text: str, tag: str) -> str:
    """取出【tag】后面的内容，到下一个已知【…】标记或结尾为止（整行原文另行保留）"""
    others = "|".join(re.escape(f"【{t}】") for t in SOUND_TAGS if t != tag)
    m = re.search(rf"【{re.escape(tag)}】\s*[：:]?\s*(.*?)(?={others}|$)", text, re.S)
    return m.group(1).strip().rstrip("；;，, ") if m else ""


def _sound_blocks(sound: str) -> tuple[str, str]:
    """v3 的 sound 行：原文；【亲密声】…；【配乐】…；【H3 audio】…（整行原文仍保留在 sound）"""
    return _block(sound, "亲密声"), _block(sound, "H3 audio")


PACE_SHOT_S = 6     # K-WRT-005 / K-WRT-Q07：单镜 ≤6 秒；镜数上限至少要装得下 总时长 ÷ 6（总调度 11-01 定 G29 方案 A）


def shot_cap(ep: Episode, target_s: float | None = None) -> int:
    """K1 + G29：本集镜数上限 = max(8, 说话轮次 + 独白段数, ⌈总时长 ÷ 6⌉)；同一说话人连续开口算一轮。
    总时长 = target_s（写作前，P3 的 target_duration_s），没给就用本集各镜 **时长** 之和。
    ⤴ 旧口径 max(8, 轮次 + 独白) 与总时长 50–75 秒放在一起时平均镜长下限 7.5 秒（ep03 / ep04 节奏慢的结构原因），已被取代"""
    turns, last = 0, None
    for s in ep.shots:
        for d in s.dialogue:
            sp = (d.speaker_note or "").strip()
            if sp != last:
                turns, last = turns + 1, sp
    monologues = sum(1 for s in ep.shots if s.voiceover and s.voiceover.text)
    total = target_s if target_s else sum((s.duration or 0) for s in ep.shots)
    return max(SHOTS_PER_EPISODE[1], turns + monologues, -(-int(total) // PACE_SHOT_S))


def _validate(episodes: list[Episode], warnings: list[str]):
    numbers = [e.number for e in episodes]
    missing = sorted(set(range(1, EPISODE_COUNT + 1)) - set(numbers))
    if missing:
        warnings.append(f"缺少集数：{missing}")
    dupes = sorted({n for n in numbers if numbers.count(n) > 1})
    if dupes:
        warnings.append(f"重复集数：{dupes}")
    for ep in episodes:
        idx = [s.index for s in ep.shots]
        if idx != list(range(1, len(idx) + 1)):
            warnings.append(f"ep{ep.number:02d} 镜号不连续：{idx}")
        cap = shot_cap(ep)
        if not SHOTS_PER_EPISODE[0] <= len(idx) <= cap:
            warnings.append(f"ep{ep.number:02d} 有 {len(idx)} 镜（应为 {SHOTS_PER_EPISODE[0]}–{cap}）")
        for s in ep.shots:
            for field in ("img_prompt", "motion_prompt", "sound"):
                if not getattr(s, field):
                    warnings.append(f"{s.id} 缺少 {field}")
        # 只在剧本写了时长时检查整集总长（v2 没写，全部默认 10 秒）
        if any(s.duration_source == "script" for s in ep.shots):
            total = sum(s.duration for s in ep.shots)
            if not EPISODE_SECONDS[0] <= total <= EPISODE_SECONDS[1]:
                warnings.append(f"ep{ep.number:02d} 总时长 {total} 秒，超出 {EPISODE_SECONDS[0]}–{EPISODE_SECONDS[1]} 秒"
                                + ("（只提示）" if EPISODE_SECONDS[1] < total <= EPISODE_SECONDS_WARN else ""))


def _scene_warnings(g: ScriptGlobals, episodes: list[Episode], problems: list[dict], warnings: list[str]) -> None:
    for p in problems:
        warnings.append(f"第 {p['line']} 行场景表格式：{p['detail']}")
    for e in g.scene_library:
        for c in e.conflicts:
            where = f"ep{c.episode:02d} 本集场景表" if c.episode else "全剧场景表"
            warnings.append(f"场景 {e.scene_id} 在{where}（第 {c.source_line} 行）的{c.field}与第一次定义不同")
    known = {e.scene_id for e in g.scene_library}
    for ep in episodes:
        for s in ep.shots:
            if s.scene_id and s.scene_id not in known:
                warnings.append(f"{s.id} 的场景 {s.scene_id} 不在场景表里")


def _character_warnings(g: ScriptGlobals, problems: list[dict], warnings: list[str]) -> None:
    for p in problems:
        warnings.append(f"第 {p['line']} 行人物表格式：{p['detail']}")
    for e in g.character_registry:
        for c in e.conflicts:
            where = f"ep{c.episode:02d} 本集人物表" if c.episode else "全剧人物表"
            warnings.append(f"角色 {e.name} 在{where}（第 {c.source_line} 行）的{c.field}与第一次定义不同")


def import_script(script_path: Path, image_doc_path: Path | None = None) -> ScriptImport:
    warnings: list[str] = []
    script = script_path.read_text(encoding="utf-8")
    image_doc = image_doc_path.read_text(encoding="utf-8") if image_doc_path else None
    sources = [SourceFile(path=str(script_path), sha256=_sha256(script_path))]
    if image_doc_path:
        sources.append(SourceFile(path=str(image_doc_path), sha256=_sha256(image_doc_path)))

    g = _parse_globals(script, image_doc, warnings)
    defs, problems, used = scene_tables(script)
    g.scene_library = build_scene_library(defs)
    cdefs, cproblems, cused = character_tables(script)      # R3：人物表
    g.character_registry = build_character_registry(cdefs)
    episodes = _parse_episodes(script, {t.trigger for t in g.lora_triggers}, warnings, used | cused)
    _scene_warnings(g, episodes, problems, warnings)
    _character_warnings(g, cproblems, warnings)
    for ep in episodes:
        state = decor_state_for(g.decor_schedule, ep.number)
        for s in ep.shots:
            s.decor_state = state
        if g.decor_schedule and state is None:
            warnings.append(f"ep{ep.number:02d} 在装饰状态总表里没有对应状态")
    _validate(episodes, warnings)
    return ScriptImport(sources=sources, globals=g, episodes=episodes, warnings=warnings)
