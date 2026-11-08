"""M1b 视频步：一次认领 = 一个变体一次渲染（阶梯 v1→s2→s3→s4→mot，按 job.tries 取第 tries 个可用变体）。

payload（ns2.enqueue_video 从本机 build_jobs 产物 video/jobs.json 取，原样不改配方）：
  {"run", "variants": {名: {graph, inputs, seed, prompt, status}}, "ladder": [...], "score_meta": {...}}
流程：上传 @输入 → 提交本机该道 ComfyUI → 等 /history → 拷到 video/clips/epNN/<sid>__<变体>_s<seed>.mp4
     → score_clip.py 打分（与旧 vidgen 同一脚本、同一门）→ mutate_takes 写一条 stage=video 的 take（op_id 幂等）。
过门 → done + 设当前；没过 → retry（has_output）；最后一次也没过 → 本镜 ns2 视频里 penalty 最小的设当前，交 best_effort（PLAN §1.4 #4）。
修正 ⑥：视频结果从这里起写进 takes.json（旧 vidgen 只写 state_<host>.json）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(os.environ.get("NS2_ROOT") or Path(__file__).resolve().parents[1])   # 服务器上 data → 旧部署 data 的软链
PORTS = {"gpu33:0": 8188, "gpu33:1": 8189, "gpu33:3": 8191, "gpu36:0": 8188, "gpu36:1": 8189, "gpu36:2": 8190}
OUTDIR = {"gpu33:0": "/root/claude-kimi-ns-studio-v1/ComfyUI/output/gpu0", "gpu33:1": "/root/claude-kimi-ns-studio-v1/ComfyUI/output/gpu1",
          "gpu33:3": "/root/claude-kimi-ns-studio-v1/ComfyUI/output/gpu3", "gpu36:0": "/root/work/fredw/comfy/output/gpu0",
          "gpu36:1": "/root/work/fredw/comfy/output/gpu1", "gpu36:2": "/root/work/fredw/comfy/output/gpu2"}
SCORE_PY = "/root/work/fredw/venv_idm/bin/python"
FFMPEG = {"gpu33": "/root/claude-kimi-ns-studio-v1/env/ffmpeg/bin", "gpu36": "/root/work/fredw/tools_bin"}   # 同旧 vidgen self.env
SCORE_SCRIPT = "/root/work/fredw/offline_20261101/repo/tools/series10_video/score_clip.py"   # 旧部署里的同一份（不复制，口径不分叉）
CLIENT = "ns2-"
LONG_LANE, LONG_FRAMES = "gpu36:2", 260
_uploads: dict[tuple[str, str], str] = {}


def api(port: int, path: str, data: bytes | None = None, ctype: str = "application/json", timeout: float = 60) -> bytes:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers={"Content-Type": ctype} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def upload(lane: str, rel: str) -> str:
    key = (lane, rel)
    if key in _uploads:
        return _uploads[key]
    p = ROOT / rel
    b = "----ns2" + uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"ns2_{p.name}\"\r\nContent-Type: image/png\r\n\r\n").encode() \
        + p.read_bytes() + (f"\r\n--{b}\r\nContent-Disposition: form-data; name=\"subfolder\"\r\n\r\nns2\r\n"
                            f"--{b}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n--{b}--\r\n").encode()
    r = json.loads(api(PORTS[lane], "/upload/image", body, f"multipart/form-data; boundary={b}", timeout=180))
    _uploads[key] = (r.get("subfolder") + "/" if r.get("subfolder") else "") + r["name"]
    return _uploads[key]


def reseed(graph: dict, seed: int) -> int:
    """换新种子（种子永不复用，PLAN §1.4 #6）：改所有 RandomNoise.noise_seed；返回改了几处"""
    k = 0
    for n in graph.values():
        if n.get("class_type") == "RandomNoise" and "noise_seed" in (n.get("inputs") or {}):
            n["inputs"]["noise_seed"] = seed
            k += 1
    return k


def submit(lane: str, var: dict, prefix: str, tag: dict, seed: int | None = None) -> str:
    g = json.loads(json.dumps(var["graph"]))
    if seed is not None and not reseed(g, seed):
        raise RuntimeError("要换种子但图里没有 RandomNoise.noise_seed")
    for n in g.values():
        for k, v in list((n.get("inputs") or {}).items()):
            if isinstance(v, str) and v.startswith("@"):
                n["inputs"][k] = upload(lane, var["inputs"][v])
        if n.get("class_type") == "SaveVideo":
            n["inputs"]["filename_prefix"] = prefix
    body = {"prompt": g, "client_id": CLIENT + uuid.uuid4().hex[:8], "extra_data": {"ns_tag": tag}}
    r = json.loads(api(PORTS[lane], "/prompt", json.dumps(body).encode(), timeout=120))
    if r.get("node_errors"):
        raise RuntimeError("node_errors " + json.dumps(r["node_errors"], ensure_ascii=False)[:600])
    return r["prompt_id"]


def wait(lane: str, pid: str, timeout_s: float, poll_s: float = 10) -> dict:
    """→ {"path", "gpu_s"} 或 {"error"}；超时 = {"error": "timeout"}（不重派同一 pid）"""
    t_end = time.time() + timeout_s
    while time.time() < t_end:
        try:
            h = json.loads(api(PORTS[lane], f"/history/{pid}", timeout=30))
        except OSError:
            time.sleep(poll_s); continue
        if pid in h:
            st = h[pid].get("status") or {}
            ts = {m[0]: (m[1] or {}).get("timestamp") for m in st.get("messages") or [] if len(m) > 1}
            gpu_s = round((ts.get("execution_success") or ts.get("execution_error") or 0) / 1000 - (ts.get("execution_start") or 0) / 1000, 1) \
                if ts.get("execution_start") else None
            if st.get("status_str") == "error":
                return {"error": "comfy_error " + json.dumps([m for m in st.get("messages") or [] if m[0] == "execution_error"], ensure_ascii=False)[-500:],
                        "gpu_s": gpu_s}
            for node in (h[pid].get("outputs") or {}).values():
                for key in ("images", "videos", "gifs"):
                    for im in node.get(key) or []:
                        if str(im.get("filename", "")).endswith(".mp4"):
                            return {"path": "/".join(x for x in (OUTDIR[lane], im.get("subfolder"), im["filename"]) if x), "gpu_s": gpu_s}
            if st.get("completed"):
                return {"error": "no_output", "gpu_s": gpu_s}
        time.sleep(poll_s)
    return {"error": "timeout"}


def score(clip: Path, meta: dict, host: str = "", tries: int = 2) -> dict:
    """与旧 vidgen 同脚本、同环境（PATH 带本机 ffmpeg，M1 实测缺它 motion_judder 崩）。打分出错 = infra：原地重打，不当没过门"""
    env = dict(os.environ, PATH=FFMPEG.get(host, "") + ":" + os.environ.get("PATH", ""), OMP_NUM_THREADS="4",
               OPENBLAS_NUM_THREADS="4", PYTHONUTF8="1")
    s: dict = {}
    for _ in range(tries):
        r = subprocess.run([SCORE_PY, SCORE_SCRIPT, str(clip), str(clip.with_suffix(".sheet.jpg")), json.dumps(meta, ensure_ascii=False)],
                           capture_output=True, text=True, timeout=1500, cwd=str(ROOT), env=env)
        line = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
        s = json.loads(line[-1]) if line else {"error": (r.stderr or "")[-800:]}
        if not s.get("error"):
            break
    return s


def rescore_pending(job: dict, meta: dict, host: str) -> list[dict]:
    """本 job 已出片、打分出错的 take → 重打分并写回（不重渲）。返回重打后的 [{n, passed}]"""
    import sys
    for p in ("services/render_router", "services/agent", "packages/schema"):
        if str(ROOT / p) not in sys.path:
            sys.path.insert(0, str(ROOT / p))
    from ns_render import pipeline as P
    doc = json.loads(P.takes_path(job["shot"], job["run_id"]).read_text(encoding="utf-8"))
    todo = [t for t in doc.get("takes") or [] if t.get("stage") == "video" and t.get("job_id") == job["id"]
            and (t.get("score") or {}).get("error") and (ROOT / "data" / t["file"]).is_file()]
    out = []
    for t in todo:
        s = score(ROOT / "data" / t["file"], meta, host)
        passed = bool((s.get("gate") or {}).get("pass")) and not s.get("error")

        def fn(d, n=t["n"], s=s, passed=passed):
            x = next(x for x in d["takes"] if x.get("stage") == "video" and x.get("n") == n)
            x["score"] = {k: s.get(k) for k in ("mean", "penalty", "gate", "J4", "error") if k in s}
            x["postflight"] = {"ok": passed, "gate": s.get("gate") or {}, "ts": P._iso_jst(), "rescored": True}
            if passed and not x.get("trial"):
                d.setdefault("active", {})["video"] = n
            return d
        P.mutate_takes(job["shot"], fn, op="ns2_rescore", by="ns2-worker", op_id=f"ns2-rescore-{job['id']}-{t['n']}", run=job["run_id"])
        out.append({"n": t["n"], "variant": t.get("variant"), "passed": passed, "error": s.get("error"),
                    "penalty": s.get("penalty"), "fails": (s.get("gate") or {}).get("fails")})
    return out


def rendered_count(job: dict) -> int:
    import sys
    if str(ROOT / "services/render_router") not in sys.path:
        sys.path.insert(0, str(ROOT / "services/render_router"))
    from ns_render import pipeline as P
    try:
        doc = json.loads(P.takes_path(job["shot"], job["run_id"]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return sum(1 for t in doc.get("takes") or [] if t.get("stage") == "video" and t.get("job_id") == job["id"])


def pick_variant(payload: dict, tries: int, lane: str) -> tuple[str, dict] | None:
    vs = payload.get("variants") or {}
    ladder = [v for v in payload.get("ladder") or list(vs) if (vs.get(v) or {}).get("status") == "ready"]
    if lane != LONG_LANE:
        ladder = [v for v in ladder if int(vs[v].get("frames") or 0) <= LONG_FRAMES]      # 长镜头只派 gpu36:2，不截短
    if not ladder:
        return None
    name = ladder[tries % len(ladder)]
    return name, vs[name]


def record_take(job: dict, var_name: str, var: dict, clip_rel: str, s: dict, *, final: bool, seed=None, trial: bool = False) -> dict:
    """写一条 video take；过门设当前；final 且没过 → ns2 视频里 penalty 最小的设当前。op_id 幂等（同 job 同次只写一次）。
    trial（验收 / 试跑）：只记 take，不动当前版"""
    import sys
    for p in ("services/render_router", "services/agent", "packages/schema"):
        if str(ROOT / p) not in sys.path:
            sys.path.insert(0, str(ROOT / p))
    from ns_render import pipeline as P
    sid, run = job["shot"], job["run_id"]
    gate = s.get("gate") or {}
    passed = bool(gate.get("pass")) and not s.get("error")
    out = {}

    def fn(doc):
        takes = doc.setdefault("takes", [])
        n = 1 + max([t.get("n") or 0 for t in takes if t.get("stage") == "video"] or [0])
        t = {"n": n, "stage": "video", "file": clip_rel, "seed": var.get("seed") if seed is None else seed, "trial": trial, "prompt": var.get("prompt", ""),
             "workflow": var.get("recipe"), "variant": var_name, "gpu": job.get("lane"), "created_at": P._iso_jst(),
             "source": "ns2", "job_id": job["id"], "score": {k: s.get(k) for k in ("mean", "penalty", "gate", "J4", "error") if k in s},
             "postflight": {"ok": passed, "gate": gate, "ts": P._iso_jst()}}
        takes.append(t)
        out["n"] = n
        if trial:
            pass
        elif passed:
            doc.setdefault("active", {})["video"] = n
        elif final:
            mine = [x for x in takes if x.get("stage") == "video" and x.get("source") == "ns2" and x.get("job_id") == job["id"]
                    and not (x.get("score") or {}).get("error")]
            if mine:
                best = min(mine, key=lambda x: (x.get("score") or {}).get("penalty", 9))
                doc.setdefault("active", {})["video"] = best["n"]
                out["best_effort_n"] = best["n"]
        return doc

    P.mutate_takes(sid, fn, op="ns2_video", by="ns2-worker", op_id=f"ns2-{job['id']}-{job['tries']}", run=run)
    return {**out, "passed": passed}


def run(job: dict, lane: str):
    p = job.get("payload") or {}
    host = lane.split(":")[0]
    re_ = rescore_pending(job, p.get("score_meta") or {}, host)          # 先补打分（M1：打分崩过的片不重渲）
    if any(r["passed"] for r in re_):
        best = next(r for r in re_ if r["passed"])
        return "done", {"rescored": re_, "take_n": best["n"], "variant": best["variant"], "has_output": True}, None
    if any(r.get("error") for r in re_):
        return "blocked", {"rescored": re_, "has_output": True}, "打分仍出错（infra，不重渲）：" + str(re_[0]["error"])[:300]
    k = rendered_count(job)                                              # 变体按“本 job 已出几条”取，不按 tries（重启 / 回收不跳号也不重出）
    pv = pick_variant(p, k, lane)
    if pv is None:
        vs = p.get("variants") or {}
        why = {k: v.get("status") for k, v in vs.items()}
        return "blocked", None, f"没有可派的变体（各变体状态：{why}）"     # 修正：写清缺什么，不只一个状态词
    name, var = pv
    sid, ep = job["shot"], int(job["ep"])
    prefix = f"ns2/{sid}__{name}"
    tag = {"agent": "NS2", "kind": "video", "shot": sid, "job": job["id"], "label": name}
    seed = int.from_bytes(os.urandom(4), "big") if p.get("fresh_seed") else None
    pid = submit(lane, var, prefix, tag, seed)
    frames = int(var.get("frames") or 125)
    res = wait(lane, pid, timeout_s=max(1800, frames * 12))
    if res.get("error"):
        return "retry", {"variant": name, "pid": pid, "has_output": False, **res}, res["error"]
    used = var.get("seed") if seed is None else seed
    dst = ROOT / "data/pipeline/runs" / job["run_id"] / "video/clips" / f"ep{ep:02d}" / f"{sid}__{name}_s{used}.mp4"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(res["path"], dst)
    s = score(dst, p.get("score_meta") or {}, host)
    final = k + 1 >= int(job.get("max_tries") or 3)
    rec = record_take(job, name, var, str(dst.relative_to(ROOT / "data")), s, final=final, seed=used, trial=bool(p.get("trial")))
    result = {"variant": name, "seed": used, "trial": bool(p.get("trial")), "pid": pid, "gpu_s": res.get("gpu_s"), "clip": str(dst.relative_to(ROOT)), "take_n": rec["n"],
              "score": {k: s.get(k) for k in ("mean", "penalty", "gate", "error") if k in s}, "has_output": True}
    if rec["passed"]:
        return "done", result, None
    if s.get("error"):                                                   # 打分 infra 错：片留着，下次领到先补打分，不重渲
        return "blocked", result, "打分出错（infra，片已出、不重渲；修好后重派即补打分）：" + str(s["error"])[:300]
    return "retry", {**result, "rescored": re_},  "视频门没过：" + "/".join((s.get("gate") or {}).get("fails") or [s.get("error") or "?"])[:300]
