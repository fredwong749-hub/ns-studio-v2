"""M1c 首 / 尾帧步：一次认领 = 一次出图 + 契约打分（旧 FrameExec 原代码，经 ns2/legacy_frame.py 子进程在旧部署目录里跑）。

结果映射：pass → done（旧 auto_current 已按契约设当前）；fail → retry（has_output）；最后一次仍不过 → 尽力版自动用
（legacy_frame.promote_best 选不过项最少的一版设当前并打 best_effort 标记，项目方 11-09；成人镜未触发不提升）→ best_effort；
na（本镜无尾帧）→ na；preflight 不过 / 成人未触发 → blocked（写清原因）；打分 / 连接出错 → infra（回队不计次，连续 5 次 → blocked）。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from ns2.step_video import PORTS

OFF = Path(os.environ.get("NS2_LEGACY_OFF", "/root/work/fredw/offline_20261101"))
RUNNER = Path(__file__).resolve().parent / "legacy_frame.py"


def legacy_env(host: str, run: str) -> dict:
    """与旧 start.sh / resident_restart.sh 同一套环境（OFFLINE_SELF 决定打分走本机、人数检测走本机常驻等）"""
    vision = OFF / "repo/models_cache/vision"
    return dict(os.environ, TZ="Asia/Tokyo", OFFLINE_SELF=host, OFFLINE_ALIASES="gpu33" if host == "gpu36" else "",
                PATH=f"{OFF}/bin:" + os.environ.get("PATH", ""), PYTHONUTF8="1", PYTHONIOENCODING="utf-8", NS_RUN=run,
                NS_QC_VISION_DIR=str(vision if vision.is_dir() else "/root/work/fredw/video10_srv/models_cache/vision"),
                PYTHONPATH="")


RENDERED = "@@NS2RENDERED@@"      # legacy_frame：图已入库、开始等契约打分（R4 NS2_ASYNC_SCORE：worker 收到就放道）


def call_legacy(args: dict, host: str, timeout_s: float = 3600, on_rendered=None) -> dict:
    """跑旧执行器子进程；逐行读 stdout，见到 RENDERED 行回调 on_rendered()（放道），最后一行 @@NS2@@ 是结果"""
    import threading
    p = subprocess.Popen([str(OFF / "venv/bin/python"), "-u", str(RUNNER), json.dumps(args, ensure_ascii=False)],
                         cwd=str(OFF / "repo"), env=legacy_env(host, args["run"]), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True)
    err: list[str] = []
    te = threading.Thread(target=lambda: err.append(p.stderr.read() or ""), daemon=True)
    te.start()
    timer = threading.Timer(timeout_s, p.kill)
    timer.start()
    out, res = [], None
    try:
        for ln in p.stdout:
            ln = ln.rstrip(chr(10))
            out.append(ln)
            if ln.startswith(RENDERED) and on_rendered is not None:
                try:
                    on_rendered()
                except Exception:  # noqa: BLE001 — 放道失败不影响这单（按旧口径占着道）
                    pass
            elif ln.startswith("@@NS2@@"):
                res = ln
        p.wait()
    finally:
        timer.cancel()
    te.join(5)
    if res is None:
        tail = chr(10).join(out)[-300:] + "".join(err)[-300:]
        return {"outcome": "infra", "error": f"旧执行器无结果 rc={p.returncode}：{tail}"}
    return json.loads(res[7:])


def run(job: dict, lane: str):
    kind = job["step"]
    p = job.get("payload") or {}
    res = (job.get("result") or {})
    prev = list(res.get("prev_blocking") or [])
    args = {"kind": kind, "sid": job["shot"], "run": job["run_id"], "lane": lane, "port": PORTS[lane],
            "attempt": int(job.get("tries") or 0) + 1, "prev_blocking": prev,
            "final_attempt": int(job.get("tries") or 0) + 1 >= int(job.get("max_tries") or 3)}   # 最后一次不过 → 尽力版自动用
    if p.get("edit"):
        args["edit"] = p["edit"]
    if p.get("owner_triggered"):
        args["owner_triggered"] = True
    from ns2.step_video import shot_adult
    args["adult"] = shot_adult(job["run_id"], job["shot"])        # 尽力版提升的成人拒绝要它（读不到剧本 = 当成人）
    if res.get("infra_take"):                       # 上次图已出、打分 infra → 这次只重打分（不占新版本号、不耗 GPU 出图）
        args["rescore_take"] = res["infra_take"]
    kw = {"on_rendered": job["_release"]} if job.get("_release") else {}     # R4 NS2_ASYNC_SCORE：图出完就放道
    out = call_legacy(args, lane.split(":")[0], float(p.get("timeout_s") or 3600), **kw)
    o = out.pop("outcome", "infra")
    keep = {**out, "prev_blocking": prev + ([out.get("blocking") or []] if o in ("pass", "fail") else []),
            "has_output": out.get("take") is not None}
    if o == "infra" and out.get("take") is not None:
        keep["infra_take"] = out["take"]
    if o == "pass":
        return "done", keep, None
    if o == "na":
        return "na", keep, None
    if o == "fail":
        return "retry", keep, "契约没过：" + "/".join(out.get("blocking") or []) + "；" + "；".join(out.get("reasons") or [])[:300]
    if o == "blocked":
        return "blocked", keep, out.get("error")
    if o == "content_bad":
        return "retry", keep, out.get("error")
    return "infra", keep, out.get("error")
