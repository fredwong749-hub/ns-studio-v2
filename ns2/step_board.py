"""M2 board 分镜步：一镜一次编译 + 生成契约预检（旧 compile_with_edits / first_job_for_rung / _contract，经 ns2/legacy_board.py）。

ok → done；成人镜 → done 且 result.adult（db.finish 把本镜排队的首 / 尾 / 视频行标 owner_trigger，等项目方触发）；
preflight 不过 → blocked（error 写缺什么，补齐后重派）；编译 / 连接出错 → infra。
"""
from __future__ import annotations

from ns2.step_frame import OFF, legacy_env
from ns2.step_video import PORTS
import json
import subprocess
from pathlib import Path

RUNNER = Path(__file__).resolve().parent / "legacy_board.py"


def sync_assets(host: str) -> dict | None:
    """R4 NS2_SPLIT_IMAGES：编分镜前先从另一台补齐资产包（并集、不覆盖本机，见 ns2/asset_sync.py）。另一台连不上 → 记下、不挡（本机资产够就照常编）"""
    from ns2 import migrate as M
    if not M.split_on():
        return None
    from ns2 import asset_sync as AS
    other = next((h for h in AS.FILE_PORTS if h != host), None)
    try:
        r = AS.pull(OFF / "repo", other)
        return {k: (v if not isinstance(v, list) else v[:20]) for k, v in r.items()} | {"from": other}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"[:300], "from": other}


def run(job: dict, lane: str):
    sync = sync_assets(lane.split(":")[0])
    args = {"sid": job["shot"], "run": job["run_id"], "lane": lane, "port": PORTS[lane]}
    r = subprocess.run([str(OFF / "venv/bin/python"), "-u", str(RUNNER), json.dumps(args, ensure_ascii=False)],
                       cwd=str(OFF / "repo"), env=legacy_env(lane.split(":")[0], job["run_id"]), capture_output=True, text=True, timeout=900)
    line = [ln for ln in (r.stdout or "").splitlines() if ln.startswith("@@NS2@@")]
    if not line:
        return "infra", None, f"分镜执行器无结果 rc={r.returncode}：{(r.stderr or r.stdout or '')[-600:]}"
    out = json.loads(line[-1][7:])
    if sync is not None:
        out["asset_sync"] = sync
    o = out.pop("outcome", "infra")
    if o in ("ok", "adult"):
        return "done", {**out, "has_output": True}, None
    if o == "blocked":
        return "blocked", out, out.get("error")
    return "infra", out, out.get("error")
