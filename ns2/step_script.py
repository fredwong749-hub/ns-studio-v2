"""M2 script 剧本步：把一集剧本（P4_epNN.md 正文在 payload 里，跟机器无关）并入本机导入稿。

一集两行（gpu33 / gpu36 各一行，两台 data 各自一份）。复用旧 tools/import_script.py（解析 / 成人判定 / v1_leak 闸门 / 时间线），
--merge-base 现有导入稿 = 只替换这一集。写之前把原导入稿复制成 .bak_ns2_<时间>（不删）。
payload：{"md_text": "...", "name": "P4_ep05.md", "version": "v3"}；解析 / 闸门不过 → failed（人工修剧本重派）。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time

from ns2.step_frame import OFF, legacy_env
from ns2.step_video import ROOT

LEGACY_PATHS = ("services/agent", "services/api", "packages/schema", "services/render_router", "services/recipes", "services/post", "packages/qc", "tools", ".")


def run(job: dict, lane: str):
    p = job.get("payload") or {}
    if not p.get("md_text"):
        return "blocked", None, "payload 没有剧本正文 md_text"
    run_id, ep = job["run_id"], int(job["ep"])
    md = ROOT / "data/pipeline/runs" / run_id / (p.get("name") or f"P4_ep{ep:02d}.md")
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(p["md_text"], encoding="utf-8")
    out = ROOT / "data/import/runs" / run_id / "script_v2.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(OFF / "venv/bin/python"), "tools/import_script.py", "--script", str(md), "-o", str(out)]
    if p.get("version"):
        cmd += ["--source-version", p["version"]]
    bak = None
    if out.is_file():
        bak = out.with_name(f"script_v2.json.bak_ns2_{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(out, bak)
        cmd += ["--merge-base", str(bak)]
    env = legacy_env(lane.split(":")[0], run_id)
    env["PYTHONPATH"] = ":".join(str(OFF / "repo" / x) for x in LEGACY_PATHS)     # 旧 import_script 靠包路径（笔记本是装进 venv 的，服务器没装）
    r = subprocess.run(cmd, cwd=str(OFF / "repo"), env=env, capture_output=True, text=True, timeout=900)
    tail = (r.stdout or "")[-1500:] + (r.stderr or "")[-500:]
    if r.returncode:
        return "failed", {"md": str(md.relative_to(ROOT)), "backup": bak and str(bak.relative_to(ROOT)), "log": tail}, f"导入不过（rc={r.returncode}）：{tail[-400:]}"
    res = {"md": str(md.relative_to(ROOT)), "out": str(out.relative_to(ROOT)), "backup": bak and str(bak.relative_to(ROOT)),
           "log": tail[-800:], "has_output": True}
    if p.get("enqueue_shots", True):
        try:
            res["enqueued"] = enqueue_shots(run_id, ep, out, hashlib.sha256(p["md_text"].encode("utf-8")).hexdigest()[:16])
        except Exception as e:  # noqa: BLE001 — 导入已成；入队失败写清，人工 python -m ns2.enqueue_episode 补
            return "blocked", res, f"剧本已导入，但镜头入队失败：{type(e).__name__}: {e}"[:400]
    return "done", res, None


def shot_rows(run_id: str, ep: int, shots: list[int], md_sha: str) -> list[dict]:
    """每镜 board / first / last（出图机）+ video（视频机，变体到时在本机编译）。sig = 剧本正文 sha：两台的剧本行入的是同一批行（幂等）"""
    from ns2.migrate import host_for
    ih, vh = host_for("images", ep), host_for("video", ep)
    rows = [{"run_id": run_id, "ep": ep, "shot": f"ep{ep:02d}", "step": "assets", "want_host": ih, "max_tries": 2,   # P6 R1a：本集前期资产
             "sig": {"script": md_sha}}]
    for i in shots:
        sid = f"ep{ep:02d}-S{i}"
        for step, host, mt in (("board", ih, 2), ("first", ih, 3), ("last", ih, 3), ("video", vh, 4)):
            rows.append({"run_id": run_id, "ep": ep, "shot": sid, "step": step, "want_host": host, "max_tries": mt, "sig": {"script": md_sha}})
    return rows


def enqueue_shots(run_id: str, ep: int, out, md_sha: str) -> dict:
    from ns2.enqueue_video import post
    d = json.loads(out.read_text(encoding="utf-8"))
    e = next((x for x in d.get("episodes") or [] if int(x.get("number") or 0) == ep), None)
    if e is None:
        raise ValueError(f"导入稿里没有第 {ep} 集")
    api = os.environ.get("NS2_API", "http://10.0.0.33:8900")
    n = {"new": 0, "existing": 0}
    for r in shot_rows(run_id, ep, [int(s["index"]) for s in e.get("shots") or []], md_sha):
        n["new" if post(api, "/api/jobs", r)["created"] else "existing"] += 1
    return n
