from ns2 import db as D
from ns2 import overview as OV


def test_overview_metrics(tmp_path):
    c = D.connect(tmp_path / "q.db")
    c.execute("INSERT INTO runs(run_id) VALUES ('r')")
    e = lambda shot, step, state, **k: D.enqueue(c, run_id="r", ep=1, shot=shot, step=step, state=state,
                                                  sig={"t": k.pop("t", 0)}, want_host="gpu36", **k)[0]
    for s in ("ep01-S1", "ep01-S2", "ep01-S3"):
        e(s, "first", "done", result={"file": f"data/renders/r/ep01/{s[5:]}/takes/i.png", "take_n": 3})
        e(s, "last", "na")
    e("ep01-S1", "video", "done", result={"clip": "data/pipeline/x/a.mp4", "has_output": True, "take_n": 2})
    e("ep01-S2", "video", "best_effort", result={"file": "data/pipeline/x/b.mp4", "has_output": True})
    e("ep01-S3", "video", "queued")
    script = {1: {"title": "T", "shots": ["ep01-S1", "ep01-S2", "ep01-S3", "ep01-S4"]}}     # 分母 = 剧本镜数
    rows = D.jobs(c, "r", limit=10**6)
    d = OV.build("r", rows, {}, script, "now")
    v = d["videos"]
    assert (v["total"], v["final"], v["pending_review"], v["best_effort"], v["queued"], v["need_frames"]) == (4, 0, 1, 1, 1, 1)
    ep = d["episodes"][0]
    assert ep["total"] == 4 and ep["first_done"] == 3 and ep["phase"] == "frames"
    s1 = ep["shots"][0]
    assert s1["first"]["url"] == "/files/gpu36/renders/r/ep01/S1/takes/i.png" and s1["video"]["state"] == "pending_review"
    assert s1["first"]["state"] == "ready" and s1["last"]["state"] == "fallback" and s1["last"]["url"] == s1["first"]["url"]
    assert ep["shots"][2]["video"]["queue_pos"] == 1
    D.set_review(c, "r", "ep01-S2", "passed")                                            # 尽力版人工定 → final
    d = OV.build("r", D.jobs(c, "r", limit=10**6), D.reviews(c, "r"), script, "now")
    assert d["videos"]["final"] == 1 and d["videos"]["best_effort"] == 0 and d["episodes"][0]["shots"][1]["video"]["state"] == "final"
    D.set_review(c, "r", "ep01-S1", "rejected")                                          # 驳回 → todo + 说明
    d = OV.build("r", D.jobs(c, "r", limit=10**6), D.reviews(c, "r"), script, "now")
    s1 = d["episodes"][0]["shots"][0]
    assert s1["video"]["state"] == "todo" and s1["note"] == "已驳回，等重出"


def test_overview_need_frames_and_adult_hold(tmp_path):
    """首帧尽力版等人 → need_frames；成人镜排队视频 → blocked + “等项目方触发”，不算 queued"""
    c = D.connect(tmp_path / "q.db")
    c.execute("INSERT INTO runs(run_id) VALUES ('r')")
    D.enqueue(c, run_id="r", ep=1, shot="ep01-S1", step="first", state="best_effort")
    D.enqueue(c, run_id="r", ep=1, shot="ep01-S1", step="video")
    D.enqueue(c, run_id="r", ep=1, shot="ep01-S2", step="first", state="done"); D.enqueue(c, run_id="r", ep=1, shot="ep01-S2", step="last", state="na")
    D.enqueue(c, run_id="r", ep=1, shot="ep01-S2", step="video", adult=True)
    d = OV.build("r", D.jobs(c, "r", limit=10**6), {}, {1: {"title": "", "shots": ["ep01-S1", "ep01-S2"]}}, "now")
    a, b = d["episodes"][0]["shots"]
    assert a["video"]["state"] == "need_frames" and a["first"]["state"] == "best_effort"
    assert b["video"]["state"] == "blocked" and "触发" in b["note"] and b["owner_trigger"]
    assert d["videos"]["queued"] == 0 and d["videos"]["blocked"] == 1 and d["videos"]["need_frames"] == 1


def test_trigger_releases_and_cancel(tmp_path):
    c = D.connect(tmp_path / "q.db")
    c.execute("INSERT INTO runs(run_id) VALUES ('r')")
    j = D.enqueue(c, run_id="r", ep=1, shot="s", step="first", adult=True)[0]
    assert j["owner_trigger"] == 1 and D.claim(c, "h", "h:0", ["first"]) is None
    D.set_review(c, "r", "s", "passed")                                              # 定稿 ≠ 触发
    assert D.claim(c, "h", "h:0", ["first"]) is None
    assert D.trigger(c, "r", "s") == [j["id"]]
    D.enqueue(c, run_id="r", ep=1, shot="t", step="first")
    assert len(D.cancel(c, "r", "t")) == 1 and D.jobs(c, shot="t")[0]["state"] == "na"
    assert D.claim(c, "h", "h:0", ["first"])["shot"] == "s"


def test_requeue_rowcount_and_adult_rehold(tmp_path):
    c = D.connect(tmp_path / "q.db")
    c.execute("INSERT INTO runs(run_id) VALUES ('r')")
    j = D.enqueue(c, run_id="r", ep=1, shot="s", step="first")[0]
    assert D.requeue(c, j["id"], "x") is None                                         # queued 行不能“重派”→ 不假装成功
    D.claim(c, "h", "h:0", ["first"]); D.finish(c, j["id"], "h", "failed", {"x": 1})
    r = D.requeue(c, j["id"], "x", adult_of=lambda run, shot: True)
    assert r["state"] == "queued" and r["owner_trigger"] == 1 and r["result"] is None


def test_timing_attribution(tmp_path):
    c = D.connect(tmp_path / "q.db")
    c.execute("INSERT INTO runs(run_id) VALUES ('r')")
    f = D.enqueue(c, run_id="r", ep=1, shot="ep01-S1", step="first", max_tries=3)[0]
    D.claim(c, "h", "h:0", ["first"]); D.finish(c, f["id"], "h", "retry", {"secs": 120, "gpu_s": 50, "has_output": True})
    D.claim(c, "h", "h:0", ["first"]); D.finish(c, f["id"], "h", "infra", {"secs": 30})
    c.execute("UPDATE jobs SET ready_at=0")                                             # 快进 infra 退避
    D.claim(c, "h", "h:0", ["first"]); D.finish(c, f["id"], "h", "done", {"secs": 90, "gpu_s": 40})
    t = OV.TM.episode(D.finish_events(c, "r"), 1)
    assert t["attempts"] == 3 and t["logic_min"] == 2.0 and t["code_min"] == 0.5 and round(t["detect_min"], 1) == 0.8
    assert t["steps"]["first"] == {"tries": 3, "min": 4.0, "retry": 1, "infra": 1}
