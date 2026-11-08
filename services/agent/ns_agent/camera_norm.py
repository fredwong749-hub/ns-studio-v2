"""编译层运镜规范化 + 运镜词表读取（DIRECTOR；K-REN-801 / K-EDT-811 / L-107）。

单一事实来源：config/director/camera_moves.yaml（每种运镜的中文名、别名、Pika 提示句模板、适用景别 / 情绪、冲突、拆分、enabled + A/B 结果）。
本模块只依赖标准库 + PyYAML，ns_recipes / tools 都按文件路径载入。

规范化（K-REN-801，按词表 enabled 判定，不再写死）：视频提示词里的官方运镜句 “The camera …” →
  识别成词表里的运镜 → enabled 的按模板重写（只留小幅、去速度词，方向保留）；disabled / 词表外的 → 退回 fallback
  （环绕：本镜没有大动作且推近 enabled → 推近；其余 → 固定），每处改动记 {rule, from, to, move, why, vocab}。
句子后半（as … 动作）不动。开关：config/post/director.json camera_norm（默认开）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VOCAB = ROOT / "config" / "director" / "camera_moves.yaml"
BIG_ACTION = re.compile(r"站起|起身|走向|走到|走开|转身|推门|开门|关门|披上|脱下|抖开|蹲|跑|扑|拥抱|抱住|rises|stands up|walks|turns away|runs", re.I)
CAM = re.compile(r"The camera ([^.,;]*?)(?=( as |,|\.|;|$))")
# 英文运镜句 → 词表 id（顺序 = 优先级）
EN_MOVES = [
    # V2 按剧运镜（runs:[series10_v2]，总调度 21:0x 定）：模板原句识别成自己的 id，不退回 arc / 固定；放在通用规则前
    (r"\brobotic high-speed camera move\b|\bwhips in a fast, tight arc\b", "bolt_push_settle"),
    (r"\borbits? slowly around\b|\bslow-motion orbit\b", "orbit_slowmo"),
    (r"\bhandheld documentary camera\b|\bfollows her at shoulder height\b", "handheld_follow"),
    (r"\bwhip[- ]pans?\b", "whip_pan"), (r"\bfocus (?:pulls?|racks?)\b|\brack focus\b", "focus_pull"),
    (r"\bslow motion\b", "slow_motion"), (r"\bforeground\b", "foreground_pass"),
    (r"\b(?:arcs?|orbits?|arc shot)\b", "arc"), (r"\b(?:pulls? out|dollies out|zooms? out)\b", "pull_out"),
    (r"\bzooms? in\b|\bcrash zoom\b", "zoom_in"), (r"\b(?:pushes|push|dollies) in\b", "push_in"),
    (r"\bpans?\b", "pan"), (r"\btilts? up\b", "tilt_up"), (r"\btilts? down\b", "tilt_down"),
    (r"\b(?:trucks?|tracks?|tracking)\b", "truck"), (r"\bpedestals? up\b|\bcranes? up\b", "pedestal_up"),
    (r"\bpedestals? down\b", "pedestal_down"), (r"\b(?:shakes?|handheld)\b", "handheld"), (r"\brolls?\b", "roll"),
    (r"\bholds? a static shot\b|\bstatic\b", "static"),
]
_cache: dict = {}


def vocab() -> dict:
    m = VOCAB.stat().st_mtime if VOCAB.exists() else 0
    if _cache.get("m") != m:
        import yaml
        d = yaml.safe_load(VOCAB.read_text(encoding="utf-8")) if VOCAB.exists() else {"moves": []}
        _cache.update(m=m, d=d, by={x["id"]: x for x in d.get("moves") or []})
    return _cache["d"]


def move(mid: str) -> dict | None:
    vocab()
    return _cache["by"].get(mid)


WALK = re.compile(r"走|行走|走动|走向|走到|走过|迈步|跟着她|walks?|walking|strides?", re.I)


def current_run() -> str | None:
    """当前剧（NS_RUN / runs.json primary，经 tools/autopilot/runs.py；没有登记表 = None）"""
    import os
    if os.environ.get("NS_RUN"):
        return os.environ["NS_RUN"]
    p = ROOT / "tools" / "autopilot" / "runs.py"
    if not (ROOT / "config" / "pipeline" / "runs.json").is_file() or not p.is_file():
        return None
    import importlib.util
    sp = importlib.util.spec_from_file_location("runs_for_camera_norm", p)
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m.current_run(ROOT)


def run_ok(m: dict | None, run: str | None = None) -> bool:
    """按剧运镜（runs: [...]）只在列出的剧里可用；没写 runs = 所有剧"""
    if not m:
        return False
    rs = m.get("runs")
    # OVERSIGHT-F 第 12 轮：只认调用方显式传入的剧（不读进程上下文），没传 = 按剧运镜不可用（失败关闭）
    return not rs or (run is not None and run in rs)


def run_scoped(mid: str | None) -> bool:
    """按剧例外（总调度 21:0x）：标了 runs 的运镜模板允许带速度词、可不以 The camera 开头（K-PRM-101 只对其余运镜）"""
    m = move(mid) if mid else None
    return bool(m and m.get("runs"))


def enabled_ids(run: str | None = None) -> list[str]:
    return [m["id"] for m in vocab().get("moves") or [] if m.get("enabled") and run_ok(m, run)]


def is_enabled(mid: str | None, run: str | None = None, motion: str | None = None) -> bool:
    """enabled + 有模板 + 本剧可用；requires: [走动] 的（handheld_follow）只在动作里有走动时可用（给了 motion 才判）"""
    m = move(mid) if mid else None
    if not (m and m.get("enabled") and m.get("template") and run_ok(m, run)):
        return False
    if "走动" in (m.get("requires") or []) and motion is not None and not WALK.search(motion):
        return False
    return True


def match_zh(text: str) -> str | None:
    """剧本 motion 开头的运镜写法（中文 / 英文别名）→ 词表 id；最长别名优先"""
    t = (text or "").lower()
    best = None
    for m in vocab().get("moves") or []:
        for a in m.get("aliases") or []:
            if a.lower() in t and (best is None or len(a) > best[1]):
                best = (m["id"], len(a))
    return best[0] if best else None


def match_en(c: str) -> str | None:
    for rx, mid in EN_MOVES:
        if re.search(rx, c or "", re.I):
            return mid
    return None


def clause(mid: str, direction: str = "right") -> str:
    """词表模板 → “The camera …” 句（不含 as 之后）"""
    m = move(mid) if is_enabled(mid) else move("static")
    return ((m or {}).get("template") or "The camera holds a static shot").replace("{dir}", direction)


def fallback(mid: str | None, big_action: bool) -> tuple[str, str]:
    """disabled / 词表外 → (退回的 id, 原因)"""
    m = move(mid) if mid else None
    why = f"{mid}：" + ((m or {}).get("reason") or "词表外运镜") if mid else "没认出运镜"
    if mid in ("arc", "orbit_slowmo") and not big_action and is_enabled("push_in"):   # 别的剧用不了 V2 环绕 → 同 arc 退回
        return "push_in", why
    return "static", why


def enabled() -> bool:
    try:
        return bool(json.loads((ROOT / "config/post/director.json").read_text(encoding="utf-8")).get("camera_norm", True))
    except (OSError, ValueError):
        return True


def multi_person_moves() -> list[str]:
    """L-192：双人及以上在画只允许的运镜（camera_moves.yaml multi_person_moves；没写 = 不限）"""
    return list(vocab().get("multi_person_moves") or [])


def multi_person_ok(mid: str | None, n_people: int | None) -> bool:
    allow = multi_person_moves()
    return not (n_people and n_people >= 2 and allow) or (mid in allow)


def multi_person_fallback(mid: str | None, n_people: int | None, run: str | None = None) -> tuple[str | None, str | None]:
    """不合规 → (退回 id, 原因)；合规 → (mid, None)。退回 push_in（本剧 enabled）否则 static"""
    if multi_person_ok(mid, n_people):
        return mid, None
    why = f"{mid}：L-192 {n_people} 人在画只允许 {' / '.join(multi_person_moves())}"
    return ("push_in" if is_enabled("push_in", run) else "static"), why


def norm_clause_info(c: str, big_action: bool, run: str | None = None, n_people: int | None = None) -> tuple[str, str, str | None]:
    """→ (新句子不含 “The camera ” 前缀, 词表 id, 退回原因或 None)。n_people：在画人数（L-192 多人镜白名单；None = 不判）"""
    raw = c.strip()
    mid = match_en(raw)
    d = "left" if re.search(r"\bleft\b", raw, re.I) else "right"
    why = None
    if mid and run_scoped(mid) and is_enabled(mid, run) and multi_person_ok(mid, n_people):
        return raw, mid, None                    # 本剧可用的按剧运镜：写作方照词表模板写的原句保留（速度词是它的一部分）
    if mid is None or not is_enabled(mid, run):
        mid, why = fallback(mid, big_action)
    mid, why2 = multi_person_fallback(mid, n_people, run)
    return clause(mid, d)[len("The camera "):], mid, why2 or why


def norm_clause(c: str, big_action: bool, run: str | None = None) -> str:
    return norm_clause_info(c, big_action, run)[0]


def denormalize(text: str, changes: list[dict]) -> str:
    """把规范化改动倒回去（测试守“除带规则号的规范化外 H3 正文不许改”）"""
    for c in reversed(changes or []):
        text = text.replace(c["to"], c["from"], 1)
    return text


def normalize_video_prompt(text: str, motion_body: str = "", run: str | None = None, n_people: int | None = None) -> tuple[str, list[dict]]:
    """→ (规范化后的提示词, 改动记录 [{rule, from, to, move, why, vocab}])。n_people：在画人数（L-192；None = 不判，旧调用不变）"""
    if not text or not enabled():
        return text, []
    big = bool(BIG_ACTION.search(motion_body or "") or BIG_ACTION.search(text))
    changes = []

    def sub(m):
        new, mid, why = norm_clause_info(m.group(1), big, run, n_people)
        if new != m.group(1).strip():
            changes.append({"rule": "K-REN-801", "from": "The camera " + m.group(1).strip(), "to": "The camera " + new,
                            "move": mid, "why": why or "按词表模板（小幅、去速度词）", "vocab": vocab().get("version")})
        return "The camera " + new
    return CAM.sub(sub, text), changes
