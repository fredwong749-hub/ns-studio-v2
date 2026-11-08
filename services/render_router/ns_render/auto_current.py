"""过契约即设为当前——全系统唯一的“设为当前”规则（MASTER_PICTURE 缺口 G3，项目方 11-01 授权，PRODUCER 负责）。

规则：
  1. 一张首帧 / 尾帧过了生成契约（postflight ok=True）且不是坏图 → 自动设为当前；所有路径都一样
     （PRODUCER、ASSETMASTER register_takes、画布重出 FE.regen / frame_jobs → unified_frame、编辑链）。
  2. 没过契约（ok=False / 还没算完 ok=None）或有 bad_reason（坏图）→ 绝不设为当前；它若已经是当前，
     退回到这一阶段最近一个“过契约且不是坏图”的版本，没有就清空当前（不留一张不合格的当前图）。
  3. 例外：项目方自己定稿（takes.json locked[stage]，且 locked_by[stage].by 不是 "auto"）的版本，永远不被自动挤掉。

所有调用方只调 on_contract(shot_id, stage, n)（契约结果写进版本之后）；入库时一律 _add_take(activate=False)。
项目方在界面上手动“设为当前 / 采用”不走这里（那是项目方本人的决定）。
"""
from __future__ import annotations

from . import pipeline as P

STAGES = ("image", "last")


def take_pf(t: dict | None) -> dict:
    """版本上的契约结果：take.postflight（register_takes / PRODUCER）或 take.contract（ok / postflight，unified 路径）"""
    if not t:
        return {}
    p = t.get("postflight")
    if isinstance(p, dict) and (p.get("ok") is not None or p.get("status") or p.get("state")):
        return p
    c = t.get("contract") or {}
    if isinstance(c, dict):
        if c.get("ok") is not None:
            return c
        if isinstance(c.get("postflight"), dict):
            return c["postflight"]
    return {}


def _q9_ok(pf: dict) -> bool:
    """VQAGATE（项目方 11-01 16:10，D-VQA-01）：看图闸门开着时，没有 Q9（没看过图）或 Q9 不过的版本不能当“过契约”——
    否则撤下一张看图不过的当前版，会退回到更早、从没看过图的版本，系统就不重出了"""
    try:
        from ns_agent.vqa import contract as VQ
    except Exception:  # noqa: BLE001
        return True
    import os
    if not VQ.enforced() or (os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get("VQA_GATE_IN_TESTS")):
        return True          # 测试里的假版本没有 Q9；要测这条的用例设 VQA_GATE_IN_TESTS=1
    it = next((i for i in pf.get("items") or [] if i.get("id") == "Q9"), None)
    return bool(it) and it.get("status") in ("pass", "na")


def passes(t: dict | None) -> bool:
    return bool(t) and take_pf(t).get("ok") is True and not P.bad_reason(t) and _q9_ok(take_pf(t))


def owner_lock(data: dict, stage: str) -> int | None:
    """项目方本人定稿的版本号（自动定稿 locked_by.by == "auto" 不算）"""
    n = (data.get("locked") or {}).get(stage)
    if n is None:
        return None
    by = ((data.get("locked_by") or {}).get(stage) or {})
    if str(by.get("by") or "").lower() == "auto" and by.get("n") in (None, n):
        return None
    return n


def decide(data: dict, stage: str, n: int) -> tuple[str, int | None, str]:
    """纯函数：→ (动作, 目标版本号, 原因)。动作：activate | keep | revert | clear"""
    takes = [t for t in data.get("takes") or [] if t.get("stage") == stage]
    t = next((x for x in takes if x.get("n") == n), None)
    active = (data.get("active") or {}).get(stage)
    lock = owner_lock(data, stage)
    if t is None:
        return "keep", active, "没有这个版本"
    gate = (data.get("master_gate") or {}).get(stage)
    if passes(t) and gate and n != active:
        # 总调度驳回了当前版本（master_reject）：替换版过了契约也要等总调度复核才换上，当前版本先留着
        return "keep", active, f"总调度驳回了 v{gate.get('reject_n')}，替换版 v{n} 过了契约，等总调度复核"
    if passes(t):
        if lock is not None and lock != n:
            return "keep", active, f"项目方定稿了 v{lock}，不自动挤掉"
        return ("keep" if active == n else "activate"), n, "过契约且不是坏图，设为当前"
    why = "坏图：" + str(P.bad_reason(t)) if P.bad_reason(t) else ("契约没过" if take_pf(t).get("ok") is False else
                                                               "没过本地看图闸门（Q9 缺 / 不过，VQAGATE）" if take_pf(t).get("ok") is True
                                                               else "契约还没算完")
    if active != n or lock == n:
        return "keep", active, f"{why}，不设为当前"
    good = [x for x in takes if x.get("n") != n and passes(x)]
    if good:
        best = max(good, key=lambda x: x.get("n") or 0)
        return "revert", best["n"], f"{why}；当前退回到最近一个过契约的 v{best['n']}"
    return "clear", None, f"{why}；这一阶段没有过契约的版本，清空当前"


def audit_change(shot_id: str, stage: str, frm, to, reason: str, actor: str, trigger_n=None) -> None:
    """DATAALIGN 11-01：每次“当前版本”变化都追加一行 data/producer/current_changes.jsonl（谁、哪镜哪阶段、从几到几、为什么）"""
    import json
    import time
    if frm == to:
        return
    f = P.DATA / "producer" / "current_changes.jsonl"
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "shot": shot_id, "stage": stage, "from": frm, "to": to,
               "reason": reason, "actor": actor, "trigger_take": trigger_n}
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def on_contract(shot_id: str, stage: str, n: int, actor: str = "auto_current（G3 过契约即设为当前）") -> dict:
    """契约结果写进版本后调用（任何路径）。→ {action, n, why}；当前版本变了就记审计"""
    stage = "image" if stage in ("first", "image") else stage
    if stage not in STAGES:
        return {"action": "keep", "n": None, "why": f"{stage} 不归这条规则"}
    with P.shot_lock(shot_id):
        data = P.load_takes(shot_id)
        before = (data.get("active") or {}).get(stage)
        action, target, why = decide(data, stage, n)
        if action in ("activate", "revert") and target is not None:
            P.set_active(shot_id, stage, target)
        elif action == "clear":
            data = P.load_takes(shot_id)
            (data.get("active") or {}).pop(stage, None)
            P._save_takes(shot_id, data)
        after = (P.load_takes(shot_id).get("active") or {}).get(stage)
    audit_change(shot_id, stage, before, after, why, actor, n)
    return {"action": action, "n": target, "why": why}
