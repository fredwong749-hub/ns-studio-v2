"""六步函数注册表。echo（链路验收用，不碰 GPU）；video（M1b）/ first、last（M1c）/ film（M2）；script / board（M2）。"""
from __future__ import annotations


def echo(job: dict, lane: str):
    p = job.get("payload") or {}
    return p.get("echo_state", "done"), {"echo": True, "lane": lane, "has_output": bool(p.get("has_output"))}, p.get("echo_error")


STEP_FNS = {}
if True:  # M1a：测试 run 用 echo 顶所有步；真步函数注册后按 payload.echo 分流
    def _dispatch(step):
        def fn(job, lane):
            if (job.get("payload") or {}).get("echo"):
                return echo(job, lane)
            real = REAL.get(step)
            if real is None:
                return "blocked", None, f"{step} 步函数还没上线（M1b/M1c）"
            return real(job, lane)
        return fn
    from ns2 import step_frame as _SF
    from ns2 import step_assets as _SA
    from ns2 import step_board as _SB
    from ns2 import step_film as _SFL
    from ns2 import step_script as _SS
    from ns2 import step_video as _SV
    REAL: dict = {"script": _SS.run, "assets": _SA.run, "board": _SB.run, "video": _SV.run, "first": _SF.run, "last": _SF.run, "film": _SFL.run}
    STEP_FNS = {s: _dispatch(s) for s in ("script", "assets", "board", "first", "last", "video", "film")}
