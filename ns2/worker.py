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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", required=True)
    ap.add_argument("--host", required=True)
    ap.add_argument("--lanes", required=True)
    ap.add_argument("--steps", default="first,last,video,film")
    a = ap.parse_args(argv)
    from ns2.steps import STEP_FNS
    lanes = [x for x in a.lanes.split(",") if x]
    assert "gpu33:2" not in lanes, "gpu33:2 是坏卡，永不派"
    steps = a.steps.split(",")
    log("WORKER", a.host, lanes, steps, a.api)
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
