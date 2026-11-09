"""P6 11-09 项目方“把 gpu36 也用起来”：视频行两台谁空谁领——交接带尽力版标记、拼片取对方的片、新视频行任一台、已排队的行可改目标"""
import json
import sys
import types


def _fake_pipeline(monkeypatch, doc):
    mod = types.ModuleType("ns_render.pipeline")
    mod.mutate_takes = lambda sid, fn, **k: fn(doc)
    pkg = types.ModuleType("ns_render"); pkg.pipeline = mod
    monkeypatch.setitem(sys.modules, "ns_render", pkg)
    monkeypatch.setitem(sys.modules, "ns_render.pipeline", mod)


def test_handoff_carries_best_effort_mark_and_i2va_fallback(tmp_path, monkeypatch):
    from ns2 import handoff as HO
    doc = {"active": {}, "takes": [], "best_effort": {"last": {"n": 9}}}      # 本机有过期的尾帧标记
    _fake_pipeline(monkeypatch, doc)
    remote = {"active": {"image": 2}, "takes": [{"stage": "image", "n": 2, "file": "renders/r/ep05/S1/takes/image_002.png"}],
              "best_effort": {"image": {"n": 2, "by": "ns2-best-effort"}}, "last_frame_fallback": {"mode": "i2va", "first_n": 2}}
    monkeypatch.setattr(HO, "_get", lambda base, rel, timeout=120: json.dumps(remote).encode() if rel.endswith("takes.json") else b"png")
    out = HO.sync_frames("r", "ep05-S1", "gpu33", tmp_path, base="http://x")
    assert out["changed"] and doc["active"]["image"] == 2
    assert doc["best_effort"] == {"image": {"n": 2, "by": "ns2-best-effort"}}            # 带过来；尾帧旧标记去掉
    assert doc["last_frame_fallback"] == {"mode": "i2va", "first_n": 2}
    assert HO.sync_frames("r", "ep05-S1", "gpu33", tmp_path, base="http://x")["changed"] is False


def test_film_fetches_clip_from_other_host(tmp_path):
    from ns2 import step_film as F
    calls = []
    get = lambda base, rel, timeout=120: calls.append((base, rel)) or b"mp4-bytes"      # noqa: E731
    rows = [{"id": 1, "ep": 5, "shot": "ep05-S1", "step": "video", "state": "done", "host": "gpu36",
             "result": {"file": "renders/r/ep05/S1/takes/video_001.mp4"}},
            {"id": 2, "ep": 5, "shot": "ep05-S2", "step": "video", "state": "done", "host": "gpu33", "result": {"file": "renders/r/ep05/S2/takes/x.mp4"}}]
    s = F.pick_sources("r", 5, rows, tmp_path, host="gpu33", get=get)
    assert s[0]["kind"] == "video" and s[0]["path"] == "data/episodes/_remote/gpu36/renders/r/ep05/S1/takes/video_001.mp4"
    assert (tmp_path / s[0]["path"]).read_bytes() == b"mp4-bytes" and calls[0][0].endswith(":8901")
    assert s[1]["kind"] == "black"                                       # 本机的片却不在本机 → 不去别处取，照旧占位
    F.pick_sources("r", 5, rows, tmp_path, host="gpu33", get=get)
    assert len(calls) == 1                                               # 取过的不再取
    bad = lambda base, rel, timeout=120: (_ for _ in ()).throw(OSError("down"))   # noqa: E731
    s2 = F.pick_sources("r", 5, [{**rows[0], "shot": "ep05-S3", "result": {"file": "renders/r/ep05/S3/v.mp4"}}], tmp_path, host="gpu33", get=bad)
    assert s2[0]["kind"] == "black"                                      # 取不到 = 占位，不挡整集


def test_new_video_rows_any_host_images_still_partitioned():
    from ns2 import step_script as SS
    rows = SS.shot_rows("r", 5, [1], "sha")
    by = {r["step"]: r["want_host"] for r in rows}
    assert by["video"] is None and by["first"] == "gpu33" and by["last"] == "gpu33" and by["assets"] == "gpu33"


def test_retarget_queued_video_rows(tmp_path, api_mod):
    from fastapi.testclient import TestClient
    from ns2 import db as D
    c = TestClient(api_mod.app, headers={"X-NS2-Key": "test-key"})
    db = api_mod.conn()
    q, _ = D.enqueue(db, run_id="r", ep=5, shot="ep05-S1", step="video", want_host="gpu33", sig={"a": 1})
    run_, _ = D.enqueue(db, run_id="r", ep=5, shot="ep05-S2", step="video", want_host="gpu33", sig={"a": 1})
    db.execute("UPDATE jobs SET state='running' WHERE id=?", (run_["id"],))
    r = c.post("/api/retarget", json={"run_id": "r", "ep": 5, "step": "video", "want_host": None}).json()
    assert r["retargeted"] == [q["id"]]                                  # 在跑的不动
    assert db.execute("SELECT want_host FROM jobs WHERE id=?", (q["id"],)).fetchone()[0] is None
    assert db.execute("SELECT count(*) FROM events WHERE job_id=? AND kind='retarget'", (q["id"],)).fetchone()[0] == 1
    assert c.post("/api/retarget", json={"run_id": "r", "ep": 5, "want_host": "gpu99"}).status_code == 400
    assert TestClient(api_mod.app).post("/api/retarget", json={"run_id": "r", "ep": 5}).status_code == 401
