"""尽力版自动用的“读视图”（项目方 11-09“尽力版一律自动用”的接线；P6 恢复 ep05 时实测发现缺口）。

promote_best 只把尽力版设成当前并在 takes.json 顶层打 best_effort[stage] 标记；但旧系统下游三处只认“postflight.ok”：
  1. producer 尾帧 preflight：“首帧没有过契约的当前版本，不能做尾帧”（take_pf(active image).ok）
  2. build_jobs.passed_frames / passed_lasts：首 / 尾帧 ok + Q9 + anomaly 才编视频变体
  3. sop_gate 视频闸门（ns2 下由队列前置代替，见 legacy_build_jobs.py）
→ 7 镜尽力版首帧做不了尾帧、整集视频 0 变体。

这里给“带标记且仍是当前版”的那一版一个读视图：postflight 视作 ok + review_needed（旧系统 L-183 / L-196 “用尽自动采用、待项目方复核”的同一约定），
原始结果留在 orig_ok / orig_blocking。只在内存里给旧代码看，**不写回 takes.json**（证据不改）。"""
from __future__ import annotations

KEY = "best_effort"
FLAG = "ns2_best_effort"


def take_pf(t: dict | None) -> dict:
    """同 producer.take_pf：postflight 优先，其次 contract"""
    if not t:
        return {}
    p = t.get("postflight")
    if isinstance(p, dict) and (p.get("ok") is not None or p.get("state")):
        return p
    c = t.get("contract") or {}
    if isinstance(c, dict):
        if c.get("ok") is not None:
            return c
        if isinstance(c.get("postflight"), dict):
            return c["postflight"]
    return {}


def marked(td: dict | None, stage: str) -> dict | None:
    """这一阶段带尽力版标记、且标记的版本仍是当前版 → 那一版（否则 None：换过当前 / 没标记都不认）"""
    td = td or {}
    n = ((td.get(KEY) or {}).get(stage) or {}).get("n")
    if n is None or (td.get("active") or {}).get(stage) != n:
        return None
    return next((t for t in td.get("takes") or [] if isinstance(t, dict) and t.get("stage") == stage and t.get("n") == n), None)


def as_passed(pf: dict) -> dict:
    if pf.get("ok"):
        return pf
    return {**pf, "ok": True, "review_needed": True, FLAG: True, "orig_ok": pf.get("ok"), "orig_blocking": list(pf.get("blocking") or [])}


def view_take(td: dict, t: dict | None, stage: str) -> dict | None:
    m = marked(td, stage)
    if not t or m is None or t is not m and (t.get("stage"), t.get("n")) != (m.get("stage"), m.get("n")):
        return t
    pf = take_pf(t)
    if pf.get("ok"):
        return t
    return {**t, "postflight": as_passed(pf)}


def view_doc(d):
    """takes.json 文档 → 读视图副本（带标记的当前版视作过；其余原样）。不是 takes 文档 / 没标记 → 原对象"""
    if not isinstance(d, dict) or not d.get(KEY) or not isinstance(d.get("takes"), list):
        return d
    stages = [s for s in (d.get(KEY) or {}) if marked(d, s) is not None]
    if not stages:
        return d
    takes = [view_take(d, t, t.get("stage")) if isinstance(t, dict) and t.get("stage") in stages else t for t in d["takes"]]
    return {**d, "takes": takes}
