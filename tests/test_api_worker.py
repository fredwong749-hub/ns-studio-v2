"""API + worker 端到端（echo 步，不碰 GPU）：真起 uvicorn，worker 线程领活上报。"""
import socket
import threading
import time

import uvicorn


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def test_end_to_end_echo(tmp_path, monkeypatch):
    monkeypatch.setenv("NS2_DB", str(tmp_path / "q.db"))
    import importlib
    from ns2 import api as A
    importlib.reload(A)
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(A.app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    api = f"http://127.0.0.1:{port}"
    from ns2 import worker as W
    import json, urllib.request
    for _ in range(50):
        try:
            urllib.request.urlopen(api + "/api/health"); break
        except Exception:
            time.sleep(0.1)
    urllib.request.urlopen(urllib.request.Request(api + "/api/runs", json.dumps({"run_id": "test-x"}).encode(), {"Content-Type": "application/json"}, method="PUT"))
    for step in ("first", "last", "video"):
        W.post(api, "/api/jobs", {"run_id": "test-x", "ep": 1, "shot": "ep01-S1", "step": step, "payload": {"echo": True}})
    W.STOP.clear()
    from ns2.steps import STEP_FNS
    t = threading.Thread(target=W.lane_loop, args=(api, "gpu36", "gpu36:0", ["first", "last", "video"], STEP_FNS, 0.1), daemon=True)
    t.start()
    deadline = time.time() + 20
    while time.time() < deadline:
        js = json.loads(urllib.request.urlopen(api + "/api/jobs?run=test-x").read())
        if all(j["state"] == "done" for j in js):
            break
        time.sleep(0.2)
    W.STOP.set()
    assert [j["state"] for j in js] == ["done", "done", "done"]
    assert [j["step"] for j in sorted(js, key=lambda j: j["started_at"])] == ["first", "last", "video"]
    srv.should_exit = True
