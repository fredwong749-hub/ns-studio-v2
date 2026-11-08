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


def test_worker_file_server_range(tmp_path):
    import urllib.request
    from ns2 import worker as W
    (tmp_path / "data/x").mkdir(parents=True)
    (tmp_path / "data/x/a.bin").write_bytes(bytes(range(256)) * 4)
    srv = W.serve_files("127.0.0.1", 0, tmp_path)
    port = srv.server_address[1]
    r = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/files/x/a.bin", headers={"Range": "bytes=10-19"}))
    assert r.status == 206 and r.headers["Content-Range"] == "bytes 10-19/1024" and r.read() == bytes(range(10, 20))
    assert len(urllib.request.urlopen(f"http://127.0.0.1:{port}/files/x/a.bin").read()) == 1024
    import urllib.error, pytest
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/files/../../etc/passwd")
    srv.shutdown()


def test_dispatch_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("NS2_DB", str(tmp_path / "q.db"))
    import importlib
    from fastapi.testclient import TestClient
    import ns2.api as A
    importlib.reload(A)
    c = TestClient(A.app)
    c.put("/api/runs", json={"run_id": "r"})
    j = c.post("/api/shots/ep02-S3/jobs", json={"run_id": "r", "stage": "first", "mode": "edit", "edit": "她的手放下"}).json()
    assert j["created"] and j["job"]["want_host"] == "gpu36" and j["job"]["payload"] == {"edit": "她的手放下"} and not j["adult_hold"]
    assert c.post("/api/shots/ep03-S1/jobs", json={"run_id": "r", "stage": "video"}).json()["job"]["want_host"] == "gpu33"
    assert c.post("/api/shots/ep03-S1/jobs", json={"run_id": "r", "stage": "video", "mode": "edit", "edit": "x"}).status_code == 400
