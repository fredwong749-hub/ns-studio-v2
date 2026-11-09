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


_loras: dict = {}


def comfy_loras(lane: str) -> set | None:
    """本道 ComfyUI 装了哪些 LoRA（/object_info，按道缓存）；问不到 → None（preflight 不因查询失败误拦，提交时 node_errors 兜底）"""
    if lane not in _loras:
        try:
            info = json.loads(api(PORTS[lane], "/object_info/LoraLoaderModelOnly", timeout=60))
            _loras[lane] = set(info["LoraLoaderModelOnly"]["input"]["required"]["lora_name"][0])
        except (OSError, ValueError, KeyError):
            return None
    return _loras[lane]


def _active_takes(run: str, sid: str) -> dict:
    try:
        d = json.loads((ROOT / "data/renders" / run / sid.split("-")[0] / sid.split("-")[1] / "takes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    act = d.get("active") or {}
    by = {(t.get("stage"), t.get("n")): t for t in d.get("takes") or []}
    return {st: by.get((st, act.get(st))) for st in ("image", "last") if act.get(st) is not None}


def preflight(job: dict, var: dict, lane: str, p: dict) -> list[str]:
    """M5 / M7：视频提交前检查，返回缺什么（空 = 可提交）。
    ① 输入文件都在本机 ② 图里用到的 LoRA 本道 ComfyUI 都装了 ③ 编译时绑的首 / 尾帧仍是当前版（首帧重出过 → 旧变体作废）
    ④ 尾帧不能比首帧旧（首帧重出后旧尾帧还挂着 = 首尾错配）"""
    out = []
    for k, rel in (var.get("inputs") or {}).items():
        if not (ROOT / str(rel)).is_file():
            out.append(f"输入 {k} 不在本机：{rel}")
    have = comfy_loras(lane)
    if have is not None:
        for n in (var.get("graph") or {}).values():
            name = (n.get("inputs") or {}).get("lora_name")
            if n.get("class_type", "").startswith("Lora") and isinstance(name, str) and name not in have:
                out.append(f"{lane} 没装 LoRA {name}")
    act = _active_takes(job["run_id"], job["shot"])
    ft, lt = act.get("image"), act.get("last")
    if p.get("first_take") is not None and (ft or {}).get("n") != p["first_take"]:
        out.append(f"首帧当前版已变（编译时 v{p['first_take']}，现在 v{(ft or {}).get('n')}），变体要重编")
    if p.get("last_take") is not None and (lt or {}).get("n") != p["last_take"]:
        out.append(f"尾帧当前版已变（编译时 v{p['last_take']}，现在 v{(lt or {}).get('n')}），变体要重编")
    if ft and lt and str(lt.get("created_at") or "") < str(ft.get("created_at") or ""):
        out.append(f"尾帧 v{lt.get('n')}（{lt.get('created_at')}）比首帧 v{ft.get('n')}（{ft.get('created_at')}）旧，首尾错配，先重出尾帧")
    return out


def upload(lane: str, rel: str) -> str:
    p = ROOT / rel
    st = p.stat()
    key = (lane, rel, st.st_mtime_ns, st.st_size)          # M5：同路径重渲后指纹变 → 重传，不复用旧的 Comfy 文件
    if key in _uploads:
        return _uploads[key]
    b = "----ns2" + uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"ns2_{uuid.uuid4().hex[:8]}_{p.name}\"\r\nContent-Type: image/png\r\n\r\n").encode() \
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


def cancel(lane: str, pid: str) -> str:
    """B3：超时 / 放弃时把我们自己的 prompt 从 ComfyUI 撤掉（排队中 → 删；正在跑的是它 → interrupt），不留着占卡"""
    out = []
    try:
        api(PORTS[lane], "/queue", json.dumps({"delete": [pid]}).encode(), timeout=30)
        out.append("deleted")
        q = json.loads(api(PORTS[lane], "/queue", timeout=30))
        if any(len(x) > 1 and x[1] == pid for x in q.get("queue_running") or []):
            api(PORTS[lane], "/interrupt", b"{}", timeout=30)
            out.append("interrupted")
    except OSError as e:
        out.append(f"cancel_err {e!r}"[:120])
    return ",".join(out)


def wait(lane: str, pid: str, timeout_s: float, poll_s: float = 10, grace_s: float = 300) -> dict:
    """→ {"path", "gpu_s"} 或 {"error", "infra": True}。M4：ComfyUI 连不上给 grace_s 宽限，HTTP 错误立即返回；超时先撤掉自己的 prompt"""
    import urllib.error
    t_end = time.time() + timeout_s
    down_since = None
    while time.time() < t_end:
        try:
            h = json.loads(api(PORTS[lane], f"/history/{pid}", timeout=30))
            down_since = None
        except urllib.error.HTTPError as e:
            return {"error": f"comfy_http {e.code}", "infra": True, "cancel": cancel(lane, pid)}
        except OSError as e:
            down_since = down_since or time.time()
            if time.time() - down_since > grace_s:
                return {"error": f"comfy_down {grace_s:.0f}s：{e!r}"[:200], "infra": True}
            time.sleep(poll_s)
            continue
        if pid in h:
            st = h[pid].get("status") or {}
            ts = {m[0]: (m[1] or {}).get("timestamp") for m in st.get("messages") or [] if len(m) > 1}
            gpu_s = round((ts.get("execution_success") or ts.get("execution_error") or 0) / 1000 - (ts.get("execution_start") or 0) / 1000, 1) \
                if ts.get("execution_start") else None
            if st.get("status_str") == "error":
                return {"infra": True, "error": "comfy_error " + json.dumps([m for m in st.get("messages") or [] if m[0] == "execution_error"], ensure_ascii=False)[-500:],
                        "gpu_s": gpu_s}
            for node in (h[pid].get("outputs") or {}).values():
                for key in ("images", "videos", "gifs"):
                    for im in node.get(key) or []:
                        if str(im.get("filename", "")).endswith(".mp4"):
                            return {"path": "/".join(x for x in (OUTDIR[lane], im.get("subfolder"), im["filename"]) if x), "gpu_s": gpu_s}
            if st.get("completed"):
                return {"error": "no_output", "gpu_s": gpu_s, "infra": True}
        time.sleep(poll_s)
    return {"error": "timeout", "infra": True, "cancel": cancel(lane, pid)}


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
        P.mutate_takes(job["shot"], fn, op="ns2_rescore", by="ns2-worker", op_id=f"ns2-rescore-{job['id']}-{t['n']}-{uuid.uuid4().hex[:8]}", run=job["run_id"])
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


def record_take(job: dict, var_name: str, var: dict, clip_rel: str, s: dict, *, final: bool, seed=None, trial: bool = False,
                attempt: str | None = None) -> dict:
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
             "source": "ns2", "job_id": job["id"], "attempt": attempt, "score": {k: s.get(k) for k in ("mean", "penalty", "gate", "J4", "error") if k in s},
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

    # B3：op_id 绑这一次渲染（job + 第几条 + 变体 + 种子），回收 / 重派都不会撞号或重复写
    attempt = attempt or f"{job['id']}-{job['tries']}"
    P.mutate_takes(sid, fn, op="ns2_video", by="ns2-worker", op_id=f"ns2-{attempt}", run=run)
    if "n" not in out:                    # 重放（同一次渲染已写过）：找回那一条，不 KeyError
        doc = json.loads(P.takes_path(sid, run).read_text(encoding="utf-8"))
        t = next((x for x in doc.get("takes") or [] if x.get("stage") == "video" and x.get("attempt") == attempt), None)
        if t is None:
            raise RuntimeError(f"takes.json 判重放但找不到 attempt={attempt} 的 take")
        return {"n": t["n"], "passed": bool((t.get("postflight") or {}).get("ok")), "replayed": True}
    return {**out, "passed": passed}


def compile_payload(job: dict, host: str) -> tuple[dict | None, str | None]:
    """M2：整集开工时视频行先入队、没有变体 → 到这一步（首尾帧都过了）才在本机编译。
    本机 video/jobs.json 里这镜的 first/last_frame_take 与 takes.json 当前版一致 → 直接用；否则跑旧 build_jobs（纯编译，不提交）"""
    from ns2 import enqueue_video as EV
    from ns2.step_frame import OFF, legacy_env
    run_id, sid = job["run_id"], job["shot"]
    def current():
        try:
            jobs = json.loads((ROOT / "data/pipeline/runs" / run_id / "video/jobs.json").read_text(encoding="utf-8"))
            sh = next((x for x in jobs.get("shots") or [] if x.get("shot") == sid), None)
            tj = json.loads((ROOT / "data/renders" / run_id / sid.split("-")[0] / sid.split("-")[1] / "takes.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        act = tj.get("active") or {}
        return bool(sh) and sh.get("first_frame_take") == act.get("image") and (sh.get("last_frame") is None or sh.get("last_frame_take") == act.get("last"))
    log = ""
    if not current():
        r = subprocess.run([str(OFF / "venv/bin/python"), "tools/series10_video/build_jobs.py", "--run", run_id], cwd=str(OFF / "repo"),
                           env=legacy_env(host, run_id), capture_output=True, text=True, timeout=1800)
        log = ((r.stdout or "")[-300:] + (r.stderr or "")[-300:]).strip()
        if r.returncode or not current():
            return None, f"视频变体编译不过 / 与当前首尾帧对不上（build_jobs rc={r.returncode}）：{log[-400:]}"
    try:
        pl = EV.build(run_id, sid, host)[2]["payload"]
    except SystemExit as e:
        return None, f"视频变体编译后仍找不到本镜：{e}"
    if not pl.get("variants"):        # P6 11-09 ep05-S2/S12/S13：build_jobs 给这镜 0 个变体、报错原因是空的 → 把作业表里这镜的状态 / 原因带出来
        try:
            jobs = json.loads((ROOT / "data/pipeline/runs" / run_id / "video/jobs.json").read_text(encoding="utf-8"))
            sh = next((x for x in jobs.get("shots") or [] if x.get("shot") == sid), {}) or {}
        except (OSError, ValueError):
            sh = {}
        info = {k: v for k, v in sh.items() if k != "variants" and (isinstance(v, (str, int, float, bool)) or k in ("reason", "missing", "needs_writer", "skip", "blocked"))}
        return None, f"build_jobs 给本镜 0 个视频变体；作业表本镜字段：{json.dumps(info, ensure_ascii=False)[:600]}；编译日志：{log[-200:]}"
    return pl, None


def shot_adult(run_id: str, sid: str) -> bool:
    """B2 第二道：本机导入稿判成人（读不到 = 当成人）"""
    try:
        d = json.loads((ROOT / "data/import/runs" / run_id / "script_v2.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    for e in d.get("episodes") or []:
        for x in e.get("shots") or []:
            if f"ep{int(e['number']):02d}-S{int(x.get('index') or 0)}" == sid:
                return bool(x.get("adult"))
    return True


def run(job: dict, lane: str):
    p = job.get("payload") or {}
    host = lane.split(":")[0]
    if not p.get("echo") and not p.get("owner_triggered") and shot_adult(job["run_id"], job["shot"]):
        return "blocked", None, "成人镜未经项目方触发（L-128）：worker 拒渲；请在前端点“触发”"
    if not p.get("echo"):
        from ns2 import handoff as HO
        from ns2.migrate import host_for
        src = host_for("images", int(job["ep"]))
        if src and src != host:          # P3 真机发现：出图机 ≠ 视频机时先把出图机当前首 / 尾帧交接过来，否则用的是本机旧帧
            try:
                ho = HO.sync_frames(job["run_id"], job["shot"], src, ROOT)
            except HO.HandoffConflict as e:
                return "blocked", None, f"首尾帧交接冲突：{e}"
            except OSError as e:
                return "infra", None, f"首尾帧交接失败（{src} 文件口）：{e!r}"[:400]
            if ho["changed"] and p.get("variants"):
                p = {k: v for k, v in p.items() if k not in ("variants", "ladder", "score_meta", "first_take", "last_take")}   # 帧变了 → 旧变体作废，下面重编
                job = {**job, "payload": p}
    if not p.get("variants") and not p.get("echo"):
        cp, why = compile_payload(job, host)
        if cp is None:
            return "blocked", None, why
        p = {**cp, **{k: v for k, v in p.items() if k in ("trial", "fresh_seed")}}
        job = {**job, "payload": p}
    re_ = rescore_pending(job, p.get("score_meta") or {}, host)          # 先补打分（M1：打分崩过的片不重渲）
    if any(r["passed"] for r in re_):
        best = next(r for r in re_ if r["passed"])
        return "done", {"rescored": re_, "take_n": best["n"], "variant": best["variant"], "has_output": True}, None
    if any(r.get("error") for r in re_):
        return "blocked", {"rescored": re_, "has_output": True}, "打分仍出错（infra，不重渲）：" + str(re_[0]["error"])[:300]
    k = rendered_count(job)                                              # 变体按“本 job 已出几条”取，不按 tries（重启 / 回收不跳号也不重出）
    pv = pick_variant(p, k, lane)
    if pv is None and lane != LONG_LANE and pick_variant(p, k, LONG_LANE) is not None:
        # M6：长镜头（>260 帧）只能 gpu36:2 → 回队并锁定那条道（infra 不计次），不再 blocked 堵死
        return "infra", {"want_lane": LONG_LANE, "has_output": False}, f"长镜头只派 {LONG_LANE}，已改派"
    if pv is None:
        vs = p.get("variants") or {}
        why = "；".join(f"{k}={v.get('status')}" + (f"（{v.get('reason')}）" if v.get("reason") else "") for k, v in vs.items())
        return "blocked", None, f"没有可派的变体：{why}"[:900]     # B4：编译器给的缺什么原文带出来（A14 needs_writer）
    name, var = pv
    sid, ep = job["shot"], int(job["ep"])
    bad = preflight(job, var, lane, p)
    if bad:                           # M5：提交前查，缺什么写清，不白跑 GPU
        return "blocked", {"variant": name, "preflight": bad, "has_output": False}, "视频 preflight 不过：" + "；".join(bad)[:900]
    prefix = f"ns2/{sid}__{name}"
    tag = {"agent": "NS2", "kind": "video", "shot": sid, "job": job["id"], "label": name}
    seed = int.from_bytes(os.urandom(4), "big") if p.get("fresh_seed") else None
    pid = submit(lane, var, prefix, tag, seed)
    frames = int(var.get("frames") or 125)
    res = wait(lane, pid, timeout_s=max(1800, frames * 12))
    if res.get("error"):              # M4：ComfyUI 出错 / 超时 / 没产出 / 连不上 = infra（不烧变体机会；连续 5 次转 blocked）
        return "infra", {"variant": name, "pid": pid, "has_output": False, **res}, res["error"]
    used = var.get("seed") if seed is None else seed
    dst = ROOT / "data/pipeline/runs" / job["run_id"] / "video/clips" / f"ep{ep:02d}" / f"{sid}__{name}_s{used}.mp4"
    dst.parent.mkdir(parents=True, exist_ok=True)
    for i in range(3):                # M5：拷贝重试 3 次，先写临时名再改名，不留半截文件
        try:
            tmp = dst.with_suffix(".part")
            shutil.copy2(res["path"], tmp)
            tmp.replace(dst)
            break
        except OSError:
            if i == 2:
                raise
            time.sleep(5)
    s = score(dst, p.get("score_meta") or {}, host)
    final = k + 1 >= int(job.get("max_tries") or 3)
    rec = record_take(job, name, var, str(dst.relative_to(ROOT / "data")), s, final=final, seed=used, trial=bool(p.get("trial")),
                      attempt=f"{job['id']}-r{k}-{name}-s{used}")
    result = {"variant": name, "seed": used, "trial": bool(p.get("trial")), "pid": pid, "gpu_s": res.get("gpu_s"), "clip": str(dst.relative_to(ROOT)), "take_n": rec["n"],
              "score": {k: s.get(k) for k in ("mean", "penalty", "gate", "error") if k in s}, "has_output": True}
    if rec["passed"]:
        return "done", result, None
    if s.get("error"):                                                   # 打分 infra 错：片留着，下次领到先补打分，不重渲
        return "blocked", result, "打分出错（infra，片已出、不重渲；修好后重派即补打分）：" + str(s["error"])[:300]
    return "retry", {**result, "rescored": re_},  "视频门没过：" + "/".join((s.get("gate") or {}).get("fails") or [s.get("error") or "?"])[:300]
