import json

from ns2 import enqueue_video as EV


def setup(root, frames_ok=True):
    vd = root / "data/pipeline/runs/r/video"
    vd.mkdir(parents=True)
    (vd / "jobs.json").write_text(json.dumps({"shots": [
        {"shot": "ep01-S1", "ep": 1, "last_frame": None, "first_frame_take": 2,
         "variants": {"v1": {"status": "needs_writer", "reason": "缺动作描述（motion_prompt 空）"}}, "ladder_order": ["v1"]},
        {"shot": "ep01-S3", "ep": 1, "last_frame": None, "first_frame_take": 2,
         "variants": {"v1": {"status": "ready", "graph": {}, "frames": 121}}, "ladder_order": ["v1"]}]}))
    for s in ("S1", "S3"):
        d = root / "data/renders/r/ep01" / s
        d.mkdir(parents=True)
        (d / "takes.json").write_text(json.dumps({"active": {"image": 2}, "takes": [
            {"stage": "image", "n": 2, "postflight": {"ok": frames_ok}, "score": {"vqa": {"pass": True}}}]}))


def test_per_shot_isolation_and_blocked_reason(tmp_path, monkeypatch, capsys):
    """M8：一镜坏不连坐；B4 / M6：没有可派变体 → 视频行 blocked，原因带编译器 reason 原文"""
    setup(tmp_path)
    monkeypatch.setattr(EV, "ROOT", tmp_path)
    import ns2.gates as G
    monkeypatch.setattr(G, "frame_passed", lambda stage, t, locked=False: ((t or {}).get("postflight") or {}).get("ok", False) and (True, "") or (False, "x"))
    rc = EV.main(["--api", "x", "--run", "r", "--shots", "ep01-S1,ep01-S2,ep01-S3", "--host", "gpu36", "--dry"])
    out = capsys.readouterr().out
    assert rc == 1 and "ep01-S2 SKIP" in out and "ep01-S3 video queued" in out          # S2 不在表里，S3 照样出
    v = [r for r in EV.build("r", "ep01-S1", "gpu36") if r["step"] == "video"][0]
    assert v["state"] == "blocked" and "缺动作描述" in v["error"]
