"""跨镜连贯参考（VQAGATE 11-01，项目方：“不连贯……一旦发现不对，叫系统重写做”）。

同一场（剧本 shot.scene_id 相同）的上一镜**当前且过契约**的首帧 → 作为看图请求的额外参考图
（label“上一镜首帧（同一场）”），本地看图模型据此判 checks.continuity_break（服装 / 外套有无 / 发色发型 / 场景 / 光线）。
上一镜和本镜造型（look_id）不同 = 剧本换装，spec 里写明“换装镜，只比场景光线”。只读文件，不改任何东西。
"""
from __future__ import annotations

import json
from pathlib import Path

from . import ROOT, cfg

# AUDIT-M I-14（总调度 11-02 18:5x）：剧本 / 出图目录按当前剧取（tools/autopilot/runs.py script_path / renders_dir），
# 不再写死 V1 的 data/import/script_v2.json、data/renders——V2 当上主剧时不会读到 V1 的镜头和首帧


def _runs():
    from ns_agent import run_paths as _RPA     # OVERSIGHT-F P2：runs 加载器只此一处
    m = _RPA.runs_module(ROOT)
    return m


def script_file() -> Path:
    return _runs().script_path()


def renders_dir() -> Path:
    return _runs().renders_dir()
LABEL = "上一镜首帧（同一场，同一人的服装 / 外套 / 发色发型、场景、光线应与它一致；原文写了换装换场除外）"
_cache: dict = {}


def _script() -> dict:
    sf = script_file()
    m = (str(sf), sf.stat().st_mtime if sf.exists() else 0)
    if _cache.get("m") != m:
        try:
            _cache.update(m=m, s=json.loads(sf.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            _cache.update(m=m, s={"episodes": []})
    return _cache["s"]


def shot_rec(sid: str) -> dict | None:
    ep, idx = int(sid[2:4]), int(sid.split("-S")[1])
    e = next((x for x in _script().get("episodes") or [] if x.get("number") == ep), None)
    return next((x for x in (e or {}).get("shots") or [] if x.get("index") == idx), None)


def prev_same_scene(sid: str) -> str | None:
    """同集、同 scene_id、紧挨着的上一镜 → 镜号；换场 / 第一镜 → None"""
    me = shot_rec(sid)
    if not me or not me.get("scene_id"):
        return None
    ep, idx = int(sid[2:4]), int(sid.split("-S")[1])
    prev = shot_rec(f"ep{ep:02d}-S{idx - 1}") if idx > 1 else None
    if prev and prev.get("scene_id") == me.get("scene_id"):
        return f"ep{ep:02d}-S{idx - 1}"
    return None


def current_first(sid: str) -> str | None:
    """镜的当前首帧（takes.json active.image，且过契约、不是驳回版）→ 绝对路径"""
    tj = renders_dir() / sid[:4] / sid.split("-")[1] / "takes.json"
    try:
        d = json.loads(tj.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    n = (d.get("active") or {}).get("image")
    t = next((t for t in d.get("takes") or [] if t.get("stage") == "image" and t.get("n") == n), None)
    if not t or not t.get("file") or t.get("master_reject"):
        return None
    f = ROOT / "data" / t["file"]
    return str(f) if f.exists() else None


def refs_for(sid: str, kind: str = "first") -> tuple[list[tuple[str, str]], str]:
    """→ (extra_refs, spec 里的“跨镜连贯”一行)。尾帧（kind=last）用本镜当前首帧作参考（同镜首尾必须连贯）"""
    if not (cfg().get("gate") or {}).get("continuity_prev_shot", True):
        return [], ""
    if kind == "last":
        f = current_first(sid)
        return ([(f, "本镜首帧（尾帧的服装 / 发色 / 场景必须与它一致，原文写了变化的除外）")], "对照本镜首帧") if f else ([], "")
    p = prev_same_scene(sid)
    if not p:
        return [], "（本镜是这一场第一镜：continuity_break 写 na）"
    f = current_first(p)
    if not f:
        return [], f"（同场上一镜 {p} 还没有过契约的首帧：continuity_break 写 na）"
    a, b = shot_rec(p) or {}, shot_rec(sid) or {}
    note = f"对照参考图里的“上一镜首帧”（{p}，同一场 {b.get('scene_id')}）"
    if (a.get("decor_state") or "") != (b.get("decor_state") or ""):
        note += f"；陈设状态原文有变化：{a.get('decor_state') or '无'} → {b.get('decor_state') or '无'}"
    return [(f, LABEL)], note
