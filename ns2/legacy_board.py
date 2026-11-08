"""board 分镜步的一次调用（M2）：在旧部署目录（cwd = $OFF/repo）里跑，只编译 + 预检，不出图。

    cd /root/work/fredw/offline_20261101/repo && python <本文件> '<json 参数>'
参数：{"sid": "ep05-S1", "run": "series10_v2", "lane": "gpu33:0", "port": 8188}
输出最后一行：@@NS2@@{...}
复用（PLAN 原则 5）：ns_render.pipeline.compile_with_edits（写 compiled.json / visual_spec.json，与旧画布同一函数）
+ producer.first_job_for_rung 第 1 档作业 + unified_frame._contract 生成契约 preflight + producer.refs_guard + 成人判定。
结果：ok（可出首帧）/ blocked（preflight 缺什么写清）/ adult（编译过了，首帧等项目方触发）。
"""
from __future__ import annotations

import hashlib
import json
import sys
import traceback
from pathlib import Path

ROOT = Path.cwd()
for p in ("", "tools/autopilot", "services/agent", "services/render_router", "packages/schema", "services/recipes", "services/post"):
    if str(ROOT / p) not in sys.path:
        sys.path.insert(0, str(ROOT / p))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main(a: dict) -> dict:
    import os
    os.environ.setdefault("NS_RUN", a["run"])
    import producer as PR
    from ns_render import pipeline as P
    from ns_render import unified_frame as UF
    from ns_render.comfy_client import ComfyClient
    from legacy_frame import _Stub
    sid = a["sid"]
    paths = PR.Paths(ROOT, run_id=a["run"])
    script = P.load_script()
    ep, shot = P.find_shot(script, sid)
    loras = P.available_loras(ComfyClient(f"http://127.0.0.1:{a['port']}"))
    c = P.compile_with_edits(script, sid, loras)               # → data/renders/<run>/epNN/SNN/compiled.json
    st = _Stub(PR, paths, script, [])
    o = {"id": f"first:{sid}", "shot": sid, "kind": "first"}
    rung = st.rung_ab(o, 1) or st.rung(o, 1)
    project = UF.project_for(script) or a["run"]
    job = {**PR.first_job_for_rung(project, ep, shot, script.globals, rung, "first", a["run"]), "lane": a["lane"]}
    pf = UF._contract(job)
    bad_refs = PR.refs_guard(paths, sid, job)
    reasons = ([] if pf.ok else list(pf.reasons())) + ([f"K-OPS-F03 refs 与目标状态不一致：{x}" for x in bad_refs])
    out = {"prompt_sha": hashlib.sha256((job.get("prompt") or "").encode("utf-8")).hexdigest()[:16],
           "has_last": bool(job.get("last_prompt") or getattr(c, "last_prompt", None)),
           "adult": bool(job.get("adult")), "adult_reasons": list(job.get("adult_reasons") or [])[:6],
           "refs": len(job.get("refs") or []), "rung": rung.get("name"), "compiled": f"renders/{a['run']}/{sid.split('-')[0]}/{sid.split('-')[1]}/compiled.json"}
    if reasons:
        return {**out, "outcome": "blocked", "error": "生成契约 preflight 不通过（缺什么补什么后重派）：" + "；".join(reasons)[:900]}
    return {**out, "outcome": "adult" if out["adult"] else "ok"}


if __name__ == "__main__":
    try:
        r = main(json.loads(sys.argv[1]))
    except Exception as e:  # noqa: BLE001
        r = {"outcome": "infra", "error": f"{type(e).__name__}: {e}"[:600], "tb": traceback.format_exc()[-1500:]}
    print("@@NS2@@" + json.dumps(r, ensure_ascii=False, default=str), flush=True)
