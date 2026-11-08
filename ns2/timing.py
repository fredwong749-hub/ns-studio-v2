"""M2 ④ 单集耗时归因（L-205：一集 ≤ 50 分钟；超时归因 逻辑 / 代码 / GPU 调度 / 检测 四类）。只读 jobs + events，不猜。

每次尝试（events finish）带 secs（这次执行墙钟）、gpu_s（ComfyUI 实际执行秒）、started_at。
  wall_min     本集墙钟 = 本集第一次开工 → 最近一次落定（迁移来的行没有开工时间，不计）
  busy_min     所有尝试执行秒之和（多道并行，可大于墙钟）
  gpu_min      其中出图 / 出片的 GPU 实际执行
  逻辑 logic   内容没过而重来的尝试耗时（retry：契约 / 视频门不过）——剧本 / 规则 / 提示词问题
  代码 code    infra 出错的尝试耗时（执行器崩 / 连接 / 打分异常）——系统代码问题
  检测 detect  成功尝试里“执行秒 − GPU 秒”（打分 / 看图 / 编译 / 上传等非出图时间）
  调度 sched   墙钟 × 可用道数 − 所有尝试执行秒（道空着 / 排队等前置）——GPU 调度问题
"""
from __future__ import annotations

from datetime import datetime

LANES = 5          # gpu33:0,1 + gpu36:0,1,2（gpu33:2 坏卡、gpu33:3 专审图）


def _t(s):
    try:
        return datetime.fromisoformat(s).timestamp() if s else None
    except ValueError:
        return None


def episode(evs: list[dict], ep: int, budget_min: float = 50.0) -> dict | None:
    xs = [e for e in evs if e["ep"] == ep and e.get("started_at")]
    if not xs:
        return None
    t0 = min(_t(e["started_at"]) for e in xs)
    t1 = max(_t(e["at"]) for e in xs)
    wall = max(0.0, (t1 - t0) / 60)
    busy = sum(float(e.get("secs") or 0) for e in xs) / 60
    gpu = sum(float(e.get("gpu_s") or 0) for e in xs) / 60
    logic = sum(float(e.get("secs") or 0) for e in xs if e.get("asked") == "retry") / 60
    code = sum(float(e.get("secs") or 0) for e in xs if e.get("asked") == "infra") / 60
    detect = sum(max(0.0, float(e.get("secs") or 0) - float(e.get("gpu_s") or 0)) for e in xs
                 if e.get("asked") not in ("retry", "infra")) / 60
    sched = max(0.0, wall * LANES - busy)
    by_step: dict = {}
    for e in xs:
        b = by_step.setdefault(e["step"], {"tries": 0, "min": 0.0, "retry": 0, "infra": 0})
        b["tries"] += 1; b["min"] += float(e.get("secs") or 0) / 60
        b["retry"] += e.get("asked") == "retry"; b["infra"] += e.get("asked") == "infra"
    r = lambda x: round(x, 1)
    cause = {"逻辑": logic, "代码": code, "检测": detect, "GPU调度": sched / LANES}
    return {"wall_min": r(wall), "budget_min": budget_min, "over_min": r(max(0.0, wall - budget_min)),
            "busy_min": r(busy), "gpu_min": r(gpu), "logic_min": r(logic), "code_min": r(code), "detect_min": r(detect),
            "sched_lane_min": r(sched), "top_cause": max(cause, key=cause.get) if wall > budget_min else None,
            "steps": {k: {**v, "min": r(v["min"])} for k, v in by_step.items()}, "attempts": len(xs)}
