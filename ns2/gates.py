"""帧“过了”的唯一口径（与旧 build_jobs.passed_frames / passed_lasts 对齐，M1 实测 ep03-S4 教训：只看 postflight.ok 会放过 Q9 没过的尽力版首帧）。
首帧：postflight.ok 且 Q9 看图过（ns_agent.vqa.contract.q9_passed，失败关闭）且未被项目方驳回。
尾帧：定稿锁（locked.last）或 postflight.ok，且未被驳回。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _vq():
    for p in ("services/agent", "packages/schema"):
        if str(ROOT / p) not in sys.path:
            sys.path.insert(0, str(ROOT / p))
    try:
        from ns_agent.vqa import contract as VQ
        return VQ
    except Exception:  # noqa: BLE001 — 看图模块载不进 = 判不过（失败关闭，同旧 build_jobs._q9_ok）
        return None


def frame_passed(stage: str, take: dict | None, locked: bool = False) -> tuple[bool, str]:
    if not take:
        return False, "无当前版"
    if take.get("master_reject"):
        return False, "项目方驳回"
    pf = take.get("postflight") or take.get("contract") or {}
    if stage == "last":
        return (True, "定稿锁") if locked or pf.get("ok") else (False, "postflight 未过")
    if not pf.get("ok"):
        return False, "postflight 未过"
    VQ = _vq()
    if VQ is None:                    # M11：看图模块载不进是 infra，不是“Q9 没过”——照样判不过（失败关闭），但原因写对
        return False, "Q9 无法判定（看图模块载不进，infra）"
    try:
        ok = VQ.q9_passed(pf)
    except Exception as e:  # noqa: BLE001 — 离线工具（migrate / enqueue_video）里也不让一镜异常炸掉整批
        return False, f"Q9 无法判定（{type(e).__name__}，infra）"
    return (True, "过") if ok else (False, "Q9 看图未过（尽力版待复核）")
