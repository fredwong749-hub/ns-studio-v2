"""ns2 worker：每台 GPU 机器一个进程，每条道（卡）一个线程，每道并发 1。

    python -m ns2.worker --api http://10.0.0.33:8900 --host gpu36 --lanes gpu36:0,gpu36:1,gpu36:2 [--steps first,last,video]

每道循环：claim → 跑步函数（ns2.steps.STEP_FNS[step]）→ finish；跑的时候每 30 s 心跳。
步函数签名：fn(job: dict, lane: str) -> (state, result, error)；state ∈ done|retry|blocked|failed|best_effort|na。
步函数抛异常 = infra 错误 → retry（error 带异常文本）。不写 queue.db（只有 API 写）。
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import traceback
import urllib.request

STOP = threading.Event()


def post(api: str, path: str, body: dict, timeout: float = 60) -> dict:
    req = urllib.request.Request(api.rstrip("/") + path, json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def log(*a):
    print(time.strftime("%m-%d %H:%M:%S"), *a, flush=True)


def run_job(job: dict, lane: str, fns: dict) -> tuple[str, dict | None, str | None]:
    fn = fns.get(job["step"])
    if fn is None:
        return "blocked", None, f"本 worker 没有 {job['step']} 步函数"
    try:
        return fn(job, lane)
    except Exception as e:  # noqa: BLE001 — 步函数崩 = infra：回队不计次（连续 5 次转 blocked）
        return "infra", None, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}"


def lane_loop(api: str, host: str, lane: str, steps: list[str], fns: dict, idle_s: float = 5):
    while not STOP.is_set():
        try:
            job = post(api, "/api/worker/claim", {"host": host, "lane": lane, "steps": steps}).get("job")
        except Exception as e:  # noqa: BLE001
            log(lane, "CLAIM_ERR", repr(e)[:200]); STOP.wait(idle_s * 3); continue
        if not job:
            STOP.wait(idle_s); continue
        log(lane, "START", job["id"], job["shot"], job["step"])
        done = threading.Event()

        def beat():
            while not done.wait(30):
                try:
                    post(api, "/api/worker/heartbeat", {"host": host, "job_id": job["id"], "lanes": [lane]})
                except Exception:  # noqa: BLE001 — 心跳丢一次不致命（45 分钟才回收）
                    pass
        threading.Thread(target=beat, daemon=True).start()
        t0 = time.time()
        state, result, error = run_job(job, lane, fns)
        done.set()
        result = {**(result or {}), "secs": round(time.time() - t0, 1)}
        for _ in range(10):
            try:
                post(api, "/api/worker/finish", {"host": host, "job_id": job["id"], "state": state, "result": result, "error": error})
                break
            except Exception as e:  # noqa: BLE001
                log(lane, "FINISH_ERR", repr(e)[:200]); time.sleep(10)
        log(lane, "END", job["id"], state, (error or "")[:160])


def serve_files(bind: str, port: int, root):
    """只读文件口（内网，给 API 转发出图 / 视频预览）：只给 <root>/data 下的文件，不列目录、不出 data 外"""
    import http.server
    import mimetypes
    import urllib.parse
    from pathlib import Path
    base = (Path(root) / "data").resolve()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            rel = urllib.parse.unquote(u.path)
            if not rel.startswith("/files/"):
                return self.send_error(404)
            try:
                f = (base / rel[len("/files/"):]).resolve()
                f.relative_to(base)
            except (ValueError, OSError):
                return self.send_error(400)
            if not f.is_file():
                return self.send_error(404)
            size = f.stat().st_size
            a, b = 0, size - 1
            rng = self.headers.get("Range") or ""
            import re
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if m and (m[1] or m[2]):                       # 视频拖动 / 只取头：Range 必须支持，否则整片走隧道
                if m[1]:
                    a, b = int(m[1]), min(size - 1, int(m[2])) if m[2] else size - 1
                else:
                    a = max(0, size - int(m[2]))
                if a > b or a >= size:
                    self.send_response(416); self.send_header("Content-Range", f"bytes */{size}"); self.end_headers(); return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
            else:
                self.send_response(200)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", mimetypes.guess_type(f.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(b - a + 1))
            self.end_headers()
            with open(f, "rb") as fh:
                fh.seek(a)
                left = b - a + 1
                while left > 0 and (chunk := fh.read(min(1 << 16, left))):
                    self.wfile.write(chunk)
                    left -= len(chunk)

    srv = http.server.ThreadingHTTPServer((bind, port), H)
    threading.Thread(target=srv.serve_forever, daemon=True, name="files").start()
    return srv


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", required=True)
    ap.add_argument("--host", required=True)
    ap.add_argument("--lanes", required=True)
    ap.add_argument("--steps", default="first,last,video,film")
    ap.add_argument("--files", default="", help="只读文件口 bind:port（如 10.0.0.36:8901）；空 = 不开")
    a = ap.parse_args(argv)
    import os
    os.environ["NS2_API"] = a.api          # film 步要读本集各镜最新行
    from ns2.steps import STEP_FNS
    lanes = [x for x in a.lanes.split(",") if x]
    assert "gpu33:2" not in lanes, "gpu33:2 是坏卡，永不派"
    steps = a.steps.split(",")
    log("WORKER", a.host, lanes, steps, a.api)
    if a.files:
        from pathlib import Path
        b, pt = a.files.rsplit(":", 1)
        serve_files(b, int(pt), Path(__file__).resolve().parents[1])
        log("FILES", a.files)
    ts = [threading.Thread(target=lane_loop, args=(a.api, a.host, l, steps, STEP_FNS), name=l, daemon=True) for l in lanes]
    for t in ts:
        t.start()
    try:
        while any(t.is_alive() for t in ts):
            try:
                post(a.api, "/api/worker/heartbeat", {"host": a.host, "lanes": lanes})
            except Exception:  # noqa: BLE001
                pass
            time.sleep(30)
    except KeyboardInterrupt:
        STOP.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
