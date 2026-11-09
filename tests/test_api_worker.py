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
    (tmp_path / "data/x/a.mp4").write_bytes(bytes(range(256)) * 4)
    srv = W.serve_files("127.0.0.1", 0, tmp_path)
    port = srv.server_address[1]
    r = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/files/x/a.mp4", headers={"Range": "bytes=10-19", "X-NS2-Key": "test-key"}))
    assert r.status == 206 and r.headers["Content-Range"] == "bytes 10-19/1024" and r.read() == bytes(range(10, 20))
    assert len(urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/files/x/a.mp4", headers={"X-NS2-Key": "test-key"})).read()) == 1024
    with pytest.raises(urllib.error.HTTPError) as e:                                   # B1：没带密钥 → 401
        urllib.request.urlopen(f"http://127.0.0.1:{port}/files/x/a.mp4")
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


def test_rollback_script_syntax():
    """M10：回滚脚本至少语法正确（真演练记录在 reports/p0/r9_M10.md）"""
    import shutil
    import subprocess
    import pytest
    b = shutil.which("bash")
    if not b:
        pytest.skip("no bash")
    r = subprocess.run([b, "-n", "ns2/rollback.sh"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_frontend_same_origin(tmp_path, monkeypatch):
    """P2：前端与 API 同源；只读页不要密钥；写操作页面发 X-NS2-Key"""
    import importlib
    from pathlib import Path
    from fastapi.testclient import TestClient
    monkeypatch.setenv("NS2_DB", str(tmp_path / "q.db"))
    monkeypatch.delenv("NS2_ROOT", raising=False)                  # 用仓库根：frontend/ 在这里
    import ns2.api as A
    importlib.reload(A)
    c = TestClient(A.app)
    assert c.get("/", follow_redirects=False).headers["location"] == "/ui/index.html"
    for f in ("index.html", "review.html", "app.js", "review.js", "style.css"):
        assert c.get(f"/ui/{f}").status_code == 200
    js = (Path(A.ROOT) / "frontend/review.js").read_text(encoding="utf-8")
    assert "X-NS2-Key" in js and "/trigger" in js and "8800" not in js


def test_review_bulk(tmp_path, api_mod):
    """11-09 项目方：一键用这版。要密钥；只定有片的镜；重复点不重写；和逐镜同一张 reviews 表"""
    from conftest import write_script
    from fastapi.testclient import TestClient
    from ns2 import db as D
    write_script(tmp_path, "r")
    A = api_mod
    anon = TestClient(A.app)
    assert anon.post("/api/reviews/bulk", json={"run_id": "r", "shots": ["ep01-S1"]}).status_code == 401
    assert anon.post("/api/auth/check").status_code == 401
    c = TestClient(A.app, headers={"X-NS2-Key": "test-key"})
    assert c.post("/api/auth/check").json() == {"ok": True}
    k = A.conn()
    D.enqueue(k, run_id="r", ep=1, shot="ep01-S1", step="video", state="done", result={"has_output": True})
    D.enqueue(k, run_id="r", ep=1, shot="ep01-S2", step="video", state="best_effort", result={"has_output": True})
    D.enqueue(k, run_id="r", ep=1, shot="ep01-S3", step="video")
    r = c.post("/api/reviews/bulk", json={"run_id": "r", "shots": ["ep01-S1", "ep01-S2", "ep01-S3", "ep01-S9"]}).json()
    assert r["done"] == ["ep01-S1", "ep01-S2"]
    assert set(r["skipped"]) == {"ep01-S3", "ep01-S9"}
    assert {s: v["state"] for s, v in D.reviews(k, "r").items()} == {"ep01-S1": "passed", "ep01-S2": "passed"}
    again = c.post("/api/reviews/bulk", json={"run_id": "r", "shots": ["ep01-S1"]}).json()
    assert again["done"] == [] and "ep01-S1" in again["skipped"]
    assert c.post("/api/reviews/bulk", json={"run_id": "r", "shots": ["ep01-S1"], "state": "rejected"}).status_code == 400
    ov = c.get("/api/overview?run=r").json()
    st = {s["id"]: s["video"]["state"] for e in ov["episodes"] for s in e["shots"]}
    assert st["ep01-S1"] == "final" and st["ep01-S2"] == "final"


def test_enqueue_episode_payload_has_name_and_version(tmp_path, monkeypatch):
    """P6 R0 发现：注释吞掉了 name / version，剧本步拿不到 --source-version"""
    from ns2 import enqueue_episode as EE
    sent = []
    monkeypatch.setattr(EE, "post", lambda api, path, body: sent.append(body) or {"job": {"id": len(sent)}, "created": True})
    md = tmp_path / "P4_ep05.final.md"
    md.write_text("x", encoding="utf-8")
    assert EE.main(["--api", "http://x", "--run", "r", "--ep", "5", "--md", str(md), "--version", "v3"]) == 0
    assert len(sent) == 2 and {b["want_host"] for b in sent} == {"gpu33", "gpu36"}
    for b in sent:
        assert b["payload"]["name"] == "P4_ep05.final.md" and b["payload"]["version"] == "v3"
    assert sum(b["payload"]["enqueue_shots"] for b in sent) == 1


def test_b1_file_port_whitelist_and_import_split(tmp_path, api_mod, monkeypatch):
    """审查 3 B1：持密钥取 queue.db / 密钥 / 日志 → 403，媒体照给；live 入队拒伪造终态与自带 result；导入口默认关"""
    import urllib.error
    import urllib.request
    import pytest
    from fastapi.testclient import TestClient
    from ns2 import worker as W
    (tmp_path / "data/x").mkdir(parents=True)
    for n in ("queue.db", "secret.key", "worker.log", "a.png"):
        (tmp_path / "data/x" / n).write_bytes(b"0123456789")
    srv = W.serve_files("127.0.0.1", 0, tmp_path)
    port = srv.server_address[1]
    H = {"X-NS2-Key": "test-key"}
    for n in ("queue.db", "secret.key", "worker.log"):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/files/x/{n}", headers=H))
        assert e.value.code == 403
    assert urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/files/x/a.png", headers=H)).status == 200
    srv.shutdown()
    c = TestClient(api_mod.app, headers=H)
    row = {"run_id": "r", "ep": 2, "shot": "ep02-S1", "step": "first"}
    assert c.post("/api/jobs", json={**row, "state": "done", "result": {"take_n": 9}}).status_code == 403
    assert c.post("/api/jobs", json={**row, "state": "best_effort"}).status_code == 403
    assert c.post("/api/jobs", json={**row, "result": {"x": 1}}).status_code == 403
    assert c.post("/api/jobs", json=row).status_code == 200
    monkeypatch.delenv("NS2_ALLOW_IMPORT", raising=False)
    assert c.post("/api/import/jobs", json={**row, "step": "last", "state": "done", "result": {"take_n": 1}}).status_code == 403
    monkeypatch.setenv("NS2_ALLOW_IMPORT", "1")
    assert c.post("/api/import/jobs", json={**row, "step": "last", "state": "done", "result": {"take_n": 1}}).json()["job"]["state"] == "done"
    from ns2.enqueue_video import job_path
    assert job_path({"state": "done", "result": {}}) == "/api/import/jobs" and job_path({"state": "queued"}) == "/api/jobs"


def test_b1_key_rotation_and_expiry(tmp_path, monkeypatch):
    from ns2 import auth as AU
    f = tmp_path / "k"
    f.write_text("newkey\noldkey 2026-11-30\n# 注释\ndead 2020-01-01\n", encoding="utf-8")
    monkeypatch.delenv("NS2_KEY", raising=False)
    monkeypatch.setattr(AU, "KEY_FILE", str(f))
    assert AU.load_key() == "newkey"
    assert AU.ok("newkey") and AU.ok("dead") is False
    assert AU.valid_keys("2026-11-09") == ["newkey", "oldkey"] and AU.valid_keys("2026-12-01") == ["newkey"]
    f.write_text("", encoding="utf-8")
    assert AU.ok("newkey") is False and AU.load_key() is None          # 读不到密钥 = 全拒（fail-closed）


def test_adult_trigger_checks_is_adult_and_can_be_revoked(tmp_path, api_mod):
    """审查 3 堵点 3：非成人镜触发被拒；误触发可撤销（排队行重新挡住、worker 领不到）；总览带 adult / triggered 给前端 gating"""
    from conftest import write_script
    from fastapi.testclient import TestClient
    write_script(tmp_path, "r", adult_shots=("ep03-S1",))
    c = TestClient(api_mod.app, headers={"X-NS2-Key": "test-key"})
    c.put("/api/runs", json={"run_id": "r"})
    assert c.post("/api/shots/ep02-S1/trigger", json={"run_id": "r"}).status_code == 400          # 非成人镜
    assert c.post("/api/shots/ep09-S9/trigger", json={"run_id": "r"}).status_code == 200          # 剧本里没有 = 当成人（fail-closed，允许项目方触发）
    c.post("/api/jobs", json={"run_id": "r", "ep": 3, "shot": "ep03-S1", "step": "first"})
    assert len(c.post("/api/shots/ep03-S1/trigger", json={"run_id": "r"}).json()["released"]) == 1
    sh = {s["id"]: s for e in c.get("/api/overview?run=r").json()["episodes"] for s in e["shots"]}
    assert sh["ep03-S1"]["adult"] is True and sh["ep03-S1"]["triggered"] is True and sh["ep02-S1"]["adult"] is False
    u = c.delete("/api/shots/ep03-S1/trigger?run_id=r").json()
    assert u["had_trigger"] is True and len(u["held"]) == 1 and u["running"] == []
    assert c.post("/api/worker/claim", json={"host": "gpu33", "lane": "gpu33:0", "steps": ["first"]}).json()["job"] is None
    sh = {s["id"]: s for e in c.get("/api/overview?run=r").json()["episodes"] for s in e["shots"]}
    assert sh["ep03-S1"]["triggered"] is False
    assert c.delete("/api/shots/ep03-S1/trigger?run_id=r", headers={"X-NS2-Key": ""}).status_code == 401


def test_assemble_default_idempotent_and_frontend_no_force(tmp_path, api_mod):
    """审查 3 堵点 4：成片默认不带 force → 同输入点两次只有一行（第二次 existing，不出新版本）；前端默认请求体不含 force:true"""
    from pathlib import Path
    from fastapi.testclient import TestClient
    c = TestClient(api_mod.app, headers={"X-NS2-Key": "test-key"})
    a = c.post("/api/episodes/1/assemble", json={"run_id": "r"}).json()
    b = c.post("/api/episodes/1/assemble", json={"run_id": "r"}).json()
    assert a["created"] is True and b["created"] is False and a["job"]["id"] == b["job"]["id"]
    f = c.post("/api/episodes/1/assemble", json={"run_id": "r", "force": True}).json()
    assert f["created"] is True and f["job"]["id"] != a["job"]["id"]                       # 只有明确强制才出新一版
    js = (Path(__file__).resolve().parents[1] / "frontend/review.js").read_text(encoding="utf-8")
    assert "force: true })" not in js and "if (force) body.force = true;" in js
    assert "assemble(false)" in js.split("btn-assemble')")[1][:80]


def test_m10_rollback_set_e_and_status_survives_empty_queries():
    """审查 3 M10：set -e；没有进程 / pid 文件（查询为空）时 status 不被 set -e 误杀"""
    import shutil
    import subprocess
    from pathlib import Path
    import pytest
    src = (Path(__file__).resolve().parents[1] / "ns2/rollback.sh").read_text(encoding="utf-8")
    assert "\nset -eu" in src and "|| true" in src
    b = shutil.which("bash")
    if not b:
        pytest.skip("no bash")
    r = subprocess.run([b, "ns2/rollback.sh", "status"], capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1])
    assert r.returncode == 0 and "旧 vidgen" in r.stdout
    assert subprocess.run([b, "ns2/rollback.sh", "bogus"], capture_output=True, text=True,
                          cwd=Path(__file__).resolve().parents[1]).returncode == 2
