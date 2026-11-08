"""DIRECTOR 导演层在合成里的落点（owner DIRECTOR；总调度 fredw-2b 11-02 批准；规则 K-EDT-804 / 813 / 814 / 816）。

开关 config/post/director.json（enabled=false → ns_post.assemble 行为与改动前完全一致）。
导演规格 data/director/epNN_cut_plan.json 由 tools/director/cut_plan.py 生成（night.finalize 合成前跑；缺了这里现生成）。
只用相对 ROOT 的路径，笔记本 / 服务器同一套（FACTORY 本机闭环，docs/master/FACTORY_ARCH.md）。
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CFG = ROOT / "config" / "post" / "director.json"
PLANS = ROOT / "data" / "director"


def cfg() -> dict:
    try:
        return json.loads(CFG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"enabled": False}


def enabled(key: str | None = None) -> bool:
    """环境变量 NS_DIRECTOR=1 / 0 覆盖总开关（A/B 用，不改配置文件）"""
    import os
    c = cfg()
    # 测试期（pytest）不读会变的真实开关（K-OPS-C02）：没显式设 NS_DIRECTOR 就按关——合成基础行为的测试不受导演层开关影响
    # （SYSARCH 11-02 报 test_fix_ep02_assemble 拆刀 2 段变 3 段，根因是 enabled=true 被测试读到）
    default = False if os.environ.get("PYTEST_CURRENT_TEST") else bool(c.get("enabled"))
    on = {"1": True, "0": False}.get(os.environ.get("NS_DIRECTOR", ""), default)
    return on and (key is None or bool(c.get(key, True)))


def _cut_plan_module():
    spec = importlib.util.spec_from_file_location("nsfs_cut_plan", ROOT / "tools" / "director" / "cut_plan.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def plan_for(episode: dict) -> dict:
    """→ {index: 导演规格}；按传进来的剧本集现算（和合成用的是同一份剧本，不读可能过期的文件）"""
    try:
        cp = _cut_plan_module()
        return {int(s["index"]): s for s in cp.plan_episode(episode)}
    except Exception:  # noqa: BLE001 —— 导演层出错不拦合成，退回原规则
        return {}


def shot_max_s(plan: dict, idx: int, default: float) -> float:
    """K-EDT-813：刀长上限 = 本镜目标刀长区间中点（强度 1 → 3.75、2 → 3.0、3 → 2.0），不超过全集 max_shot_s"""
    p = plan.get(int(idx))
    if not p or not enabled("pace_from_plan"):
        return default
    lo, hi = p["cut"]["target_s"]
    return round(min(default or 99.0, (lo + hi) / 2), 3)


def jcut_s(plan: dict, idx: int, default: float) -> float:
    """K-EDT-814：导演规格给了 j_cut_s（本镜开口 / 换场声音先行）就用它，否则用全集 jl_cut_s"""
    p = plan.get(int(idx))
    if not p or not enabled("jcut_from_plan"):
        return default
    return max(default, float(p.get("j_cut_s") or 0.0))


def _intensity(text) -> int | None:
    m = re.search(r"强度\s*([0-3])", str(text or ""))
    return int(m.group(1)) if m else None


def merge_cues(cues: list, shots: list[dict], starts: list[float]) -> tuple[list, list[dict]]:
    """K-EDT-804：BGM 时间轴的 cue 保留时间和描述，强度改用剧本【配乐】强度（整集统一 BGM：最低 music_min_intensity，不中途静默）；
    剧本强度变了而该镜没有 cue → 补一条（升 = swell）。→ (新 cue 列表, 改动记录)"""
    from .music import Cue
    if not enabled("music_intensity_from_script"):
        return cues, []
    floor = int(cfg().get("music_min_intensity", 1))
    lv = {int(s["index"]): _intensity(s.get("music_cue")) for s in shots}
    by_shot = {}
    for c in cues:
        by_shot.setdefault(int(c.shot or 0), []).append(c)
    out, log, last = [], [], None
    for k, idx in enumerate(sorted(lv)):
        x = lv[idx]
        x = max(floor, x) if x is not None else None
        here = by_shot.pop(idx, [])
        for c in here:
            if x is not None and c.intensity != x:
                log.append({"shot": idx, "from": c.intensity, "to": x, "action": c.action})
                c.intensity = x
                if c.action == "continue" and last is not None and x > last:
                    c.action = "swell"
            out.append(c)
        if not here and x is not None and last is not None and x != last and k < len(starts):
            c = Cue(shot=idx, start_s=starts[k], action="swell" if x > last else "continue", intensity=x,
                    source="director", text=f"导演层：剧本强度 {last}→{x}")
            out.append(c)
            log.append({"shot": idx, "from": last, "to": x, "action": c.action, "added": True})
        if x is not None:
            last = x
    for rest in by_shot.values():
        out += rest
    return sorted(out, key=lambda c: (c.start_s or 0.0)), log
