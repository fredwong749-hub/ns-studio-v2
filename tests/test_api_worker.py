"""API + worker 端到端（echo 步，不碰 GPU）：真起 uvicorn，worker 线程领活上报。"""
import socket
import threading
import time

import uvicorn


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def test_end_to_end_echo(tmp_path, api_mod):
    from conftest import write_script
    write_script(tmp_path, "test-x")
    A = api_mod
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
    urllib.request.urlopen(urllib.request.Request(api + "/api/runs", json.dumps({"run_id": "test-x"}).encode(), {"Content-Type": "application/json", "X-NS2-Key": "test-key"}, method="PUT"))
    for step in ("first", "last", "video"):
        W.post(api, "/api/jobs", {"run_id": "test-x", "ep": 1, "shot": "ep01-S1", "step": step, "payload": {"echo": True}})
    W.STOP.clear()
    from ns2.steps import STEP_FNS
    t = threading.Thread(target=W.lane_loop, args=(api, "gpu36", "gpu36:0", ["first", "last", "video"], STEP_FNS, 0.1), daemon=True)
    t.start()
    deadline = time.time() + 20
    while time.time() < deadline:
        js = json.loads(urllib.request.urlopen(urllib.request.Request(api + "/api/jobs?run=test-x", headers={"X-NS2-Key": "test-key"})).read())
        if all(j["state"] == "done" for j in js):
            break
        time.sleep(0.2)
    W.STOP.set()
    assert [j["state"] for j in js] == ["done", "done", "done"]
    assert [j["step"] for j in sorted(js, key=lambda j: j["started_at"])] == ["first", "last", "video"]
    srv.should_exit = True


def test_worker_file_server_range(tmp_path):
    import urllib.error
    import urllib.request
    import pytest
    from ns2 import worker as W
    (tmp_path / "data/x").mkdir(parents=True)
    (tmp_path / "data/x/a.bin").write_bytes(bytes(range(256)) * 4)
    srv = W.serve_files("127.0.0.1", 0, tmp_path)
    port = srv.server_address[1]
    r = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/files/x/a.bin", headers={"Range": "bytes=10-19", "X-NS2-Key": "test-key"}))
    assert r.status == 206 and r.headers["Content-Range"] == "bytes 10-19/1024" and r.read() == bytes(range(10, 20))
    assert len(urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/files/x/a.bin", headers={"X-NS2-Key": "test-key"})).read()) == 1024
    with pytest.raises(urllib.error.HTTPError) as e:                                   # B1：没带密钥 → 401
        urllib.request.urlopen(f"http://127.0.0.1:{port}/files/x/a.bin")
    assert e.value.code == 401
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/files/../../etc/passwd", headers={"X-NS2-Key": "test-key"}))
    srv.shutdown()


def test_dispatch_endpoint(tmp_path, api_mod):
    from conftest import write_script
    from fastapi.testclient import TestClient
    write_script(tmp_path, "r", adult_shots=("ep03-S1",))
    A = api_mod
    anon = TestClient(A.app)
    assert anon.post("/api/shots/ep02-S3/jobs", json={"run_id": "r", "stage": "first"}).status_code == 401   # B1：写要密钥
    assert anon.put("/api/shots/ep02-S3/review", json={"run_id": "r", "state": "passed"}).status_code == 401
    assert anon.post("/api/worker/claim", json={"host": "gpu33", "lane": "gpu33:0", "steps": ["first"]}).status_code == 401
    assert anon.post("/api/worker/claim", json={"host": "gpu33", "lane": "gpu33:0", "steps": ["first"]},
                     headers={"X-NS2-Key": "wrong"}).status_code == 401
    assert anon.get("/api/health").status_code == 200 and anon.get("/api/jobs").status_code == 200           # 只读开放
    assert anon.get("/files/gpu33/queue.db").status_code == 403 and anon.get("/files/gpu33/x/secret.key").status_code == 403
    c = TestClient(A.app, headers={"X-NS2-Key": "test-key"})
    c.put("/api/runs", json={"run_id": "r"})
    j = c.post("/api/shots/ep02-S3/jobs", json={"run_id": "r", "stage": "first", "mode": "edit", "edit": "她的手放下"}).json()
    assert j["created"] and j["job"]["want_host"] == "gpu36" and j["job"]["payload"] == {"edit": "她的手放下"} and not j["adult_hold"]
    assert c.post("/api/shots/ep03-S1/jobs", json={"run_id": "r", "stage": "video"}).json()["job"]["want_host"] == "gpu33"
    assert c.post("/api/shots/ep03-S1/jobs", json={"run_id": "r", "stage": "video", "mode": "edit", "edit": "x"}).status_code == 400


def test_adult_gate_server_side(tmp_path, api_mod):
    """B2 接线：成人镜无论谁入队 / 派单 / 重派都挡住，worker 领不到；触发后放行；驳回 / 定稿都不放行"""
    from conftest import write_script
    from fastapi.testclient import TestClient
    write_script(tmp_path, "r", adult_shots=("ep03-S1",))
    c = TestClient(api_mod.app, headers={"X-NS2-Key": "test-key"})
    c.put("/api/runs", json={"run_id": "r"})
    j = c.post("/api/jobs", json={"run_id": "r", "ep": 3, "shot": "ep03-S1", "step": "first", "owner_trigger": False}).json()["job"]
    assert j["owner_trigger"] == 1                                                     # 调用方说不挡也挡
    assert c.post("/api/shots/ep03-S1/jobs", json={"run_id": "r", "stage": "video"}).json()["adult_hold"] is True
    assert c.post("/api/worker/claim", json={"host": "gpu33", "lane": "gpu33:0", "steps": ["first", "video"]}).json()["job"] is None
    c.put("/api/shots/ep03-S1/review", json={"run_id": "r", "state": "passed"})
    c.put("/api/shots/ep03-S1/review", json={"run_id": "r", "state": "rejected"})
    assert c.post("/api/worker/claim", json={"host": "gpu33", "lane": "gpu33:0", "steps": ["first"]}).json()["job"] is None
    assert len(c.post("/api/shots/ep03-S1/trigger", json={"run_id": "r"}).json()["released"]) == 2
    got = c.post("/api/worker/claim", json={"host": "gpu33", "lane": "gpu33:0", "steps": ["first"]}).json()["job"]
    assert got["shot"] == "ep03-S1" and got["payload"]["owner_triggered"] is True
    # 触发后新起的行直接带触发；非成人镜不挡；剧本里没有的镜当成人
    assert c.post("/api/shots/ep03-S1/jobs", json={"run_id": "r", "stage": "last"}).json()["adult_hold"] is False
    assert c.post("/api/shots/ep03-S2/jobs", json={"run_id": "r", "stage": "first"}).json()["adult_hold"] is False
    assert c.post("/api/shots/ep09-S9/jobs", json={"run_id": "r", "stage": "first"}).json()["adult_hold"] is True
    # 自带幂等键、伪造 running、乱写 run 状态 → 400
    assert c.post("/api/jobs", json={"run_id": "r", "ep": 3, "shot": "ep03-S2", "step": "first", "key": "x"}).status_code == 400
    assert c.post("/api/jobs", json={"run_id": "r", "ep": 3, "shot": "ep03-S2", "step": "first", "state": "running"}).status_code == 400
    assert c.put("/api/runs", json={"run_id": "r", "status": "whatever"}).status_code == 400


def test_finish_durable(tmp_path, monkeypatch):
    """B3a：先落盘；transient 重试到接受；409 放弃进 rejected/；重启补报"""
    import io
    import urllib.error
    from ns2 import worker as W
    monkeypatch.setattr(W, "PENDING", tmp_path / "pending")
    calls = []

    def flaky(api, path, body, timeout=60):
        calls.append(body["job_id"])
        if len(calls) < 3:
            raise urllib.error.URLError("down")
        return {}
    monkeypatch.setattr(W, "post", flaky)
    assert W.finish_durable("x", "l", {"job_id": 7, "state": "done"}, sleep=lambda s: None) == "accepted"
    assert calls == [7, 7, 7] and not (tmp_path / "pending/7.json").exists()

    def gone(api, path, body, timeout=60):
        raise urllib.error.HTTPError("u", 409, "回收了", {}, io.BytesIO(b"x"))
    monkeypatch.setattr(W, "post", gone)
    assert W.finish_durable("x", "l", {"job_id": 8, "state": "done"}, sleep=lambda s: None) == "rejected"
    assert (tmp_path / "pending/rejected/8.json").exists()

    monkeypatch.setattr(W, "post", lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("down")))
    assert W.finish_durable("x", "l", {"job_id": 9, "state": "done"}, max_wait_s=0, sleep=lambda s: None) == "gave_up"
    assert (tmp_path / "pending/9.json").exists()                          # 留着等重启补报
    monkeypatch.setattr(W, "post", lambda *a, **k: {})
    assert W.resend_pending("x") == [(9, "accepted")] and not (tmp_path / "pending/9.json").exists()


def test_reaper_survives_errors(tmp_path, api_mod, monkeypatch):
    """M2：回收一轮出错不杀线程；health 报错；下一轮恢复"""
    from fastapi.testclient import TestClient
    A = api_mod
    c = TestClient(A.app)
    monkeypatch.setattr(A.D, "reap", lambda conn: (_ for _ in ()).throw(RuntimeError("坏行")))
    A.reap_once()
    h = c.get("/api/health").json()
    assert h["ok"] is False and "坏行" in h["reaper"]["err"]
    monkeypatch.setattr(A.D, "reap", lambda conn: [1, 2])
    A.reap_once()
    h = c.get("/api/health").json()
    assert h["ok"] is True and h["reaper"]["err"] is None and h["reaper"]["reaped"] == 2 and h["reaper"]["ok_age_s"] < 5
