import json
from pathlib import Path

from ns2 import db as D
from ns2 import step_film as F


def mk(tmp_path):
    c = D.connect(tmp_path / "q.db")
    c.execute("INSERT INTO runs(run_id) VALUES ('r')")
    return c


def vid(c, shot, state="queued", **k):
    return D.enqueue(c, run_id="r", ep=1, shot=shot, step="video", sig={"s": shot, **k}, state=state)[0]


def test_film_waits_for_all_videos_then_only_latest(tmp_path):
    c = mk(tmp_path)
    for s in ("ep01-S1", "ep01-S2"):
        D.enqueue(c, run_id="r", ep=1, shot=s, step="first", state="done")
        D.enqueue(c, run_id="r", ep=1, shot=s, step="last", state="na")
    v1 = vid(c, "ep01-S1"); vid(c, "ep01-S2", state="best_effort")
    f1, _ = D.enqueue_film(c, "r", 1)
    assert D.claim(c, "h", "h:0", ["film"]) is None                    # S1 视频还在排队 → 不合成
    D.claim(c, "h", "h:0", ["video"]); D.finish(c, v1["id"], "h", "done")   # 定一镜 → 自动新起 film 行
    films = [j for j in D.jobs(c) if j["step"] == "film"]
    assert len(films) == 2 and films[0]["state"] == "na" and "取代" in films[0]["error"]
    got = D.claim(c, "h", "h:1", ["film"])
    assert got["id"] == films[1]["id"] and got["shot"] == "ep01"
    assert D.enqueue_film(c, "r", 1)[1] is False                          # 同签名幂等


def test_trial_video_does_not_trigger_film(tmp_path):
    c = mk(tmp_path)
    D.enqueue(c, run_id="r", ep=1, shot="s", step="first", state="done")
    D.enqueue(c, run_id="r", ep=1, shot="s", step="last", state="na")
    v = D.enqueue(c, run_id="r", ep=1, shot="s", step="video", payload={"trial": True})[0]
    D.claim(c, "h", "h:0", ["video"]); D.finish(c, v["id"], "h", "done")
    assert not [j for j in D.jobs(c) if j["step"] == "film"]


def test_pick_sources(tmp_path):
    root = tmp_path
    def takes(sid, stage, n, f):
        d = root / "data/renders/r" / sid.split("-")[0] / sid.split("-")[1]
        (d / "takes").mkdir(parents=True, exist_ok=True)
        (root / "data" / f).write_bytes(b"x")
        tj = d / "takes.json"
        cur = json.loads(tj.read_text()) if tj.exists() else {"takes": [], "active": {}}
        cur["takes"].append({"stage": stage, "n": n, "file": f}); cur["active"][stage] = n
        tj.write_text(json.dumps(cur))
    takes("ep01-S1", "video", 2, "renders/r/ep01/S1/takes/v2.mp4")
    takes("ep01-S2", "image", 1, "renders/r/ep01/S2/takes/i1.png")
    be = root / "data/pipeline/runs/r/video/clips/ep01/ep01-S3__s3.mp4"; be.parent.mkdir(parents=True); be.write_bytes(b"x")
    rows = [
        {"id": 1, "ep": 1, "shot": "ep01-S1", "step": "video", "state": "done", "result": {}},
        {"id": 2, "ep": 1, "shot": "ep01-S2", "step": "first", "state": "done", "result": {}},
        {"id": 3, "ep": 1, "shot": "ep01-S2", "step": "video", "state": "failed", "result": {}, "error": "没过"},
        {"id": 4, "ep": 1, "shot": "ep01-S3", "step": "video", "state": "best_effort",
         "result": {"file": "data/pipeline/runs/r/video/clips/ep01/ep01-S3__s3.mp4"}},
        {"id": 5, "ep": 1, "shot": "ep01-S10", "step": "first", "state": "blocked", "result": {}},
        {"id": 6, "ep": 1, "shot": "ep01-S10", "step": "video", "state": "blocked", "result": {}},
        {"id": 7, "ep": 2, "shot": "ep02-S1", "step": "video", "state": "done", "result": {}},
    ]
    s = F.pick_sources("r", 1, rows, root)
    assert [x["shot"] for x in s] == ["ep01-S1", "ep01-S2", "ep01-S3", "ep01-S10"]     # 按镜号数字序，只本集
    assert s[0]["kind"] == "video" and s[0]["path"].endswith("v2.mp4")
    assert s[1]["kind"] == "still" and s[1]["missing"]
    assert s[2]["kind"] == "video" and s[2]["video_state"] == "best_effort"
    assert s[3]["kind"] == "black"                                                      # 首帧没过门不进片


def test_run_end_to_end_ffmpeg(tmp_path):
    import shutil
    import subprocess
    import pytest
    if not shutil.which("ffmpeg"):
        pytest.skip("no ffmpeg")
    root = tmp_path
    clips = root / "data/pipeline/runs/r/video/clips/ep01"; clips.mkdir(parents=True)
    a = clips / "a.mp4"; b = clips / "b.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=768x1344:r=24:d=5", "-f", "lavfi", "-i", "sine=d=4.6",
                    "-c:v", "libx264", "-c:a", "aac", str(a)], check=True, capture_output=True)   # 画面 5 秒、声轨 4.6 秒
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=768x1344:r=24:d=4", "-c:v", "libx264", str(b)], check=True, capture_output=True)
    (root / "data/import/runs/r").mkdir(parents=True)
    (root / "data/import/runs/r/script_v2.json").write_text(json.dumps({"episodes": [{"number": 1, "shots": [
        {"index": 1, "duration": 5}, {"index": 2, "duration": 4}, {"index": 3, "duration": 2}]}]}))
    rows = [{"id": 1, "ep": 1, "shot": "ep01-S1", "step": "video", "state": "done", "result": {"clip": "data/pipeline/runs/r/video/clips/ep01/a.mp4"}},
            {"id": 2, "ep": 1, "shot": "ep01-S2", "step": "video", "state": "best_effort", "result": {"file": "data/pipeline/runs/r/video/clips/ep01/b.mp4"}},
            {"id": 3, "ep": 1, "shot": "ep01-S3", "step": "video", "state": "blocked", "result": {}}]
    st, res, err = F.run({"id": 9, "run_id": "r", "ep": 1}, "local:0", rows=rows, root=root)
    assert st == "done", err
    assert res["file"] == "data/episodes/r/ns2/ep01_v1.mp4" and res["placeholders"] == ["ep01-S3"]
    assert abs(res["dur_s"] - 11) < 0.3 and abs(res["delta_s"]) < 0.3                      # 黑场占位按剧本 2 秒
    assert F.run({"id": 10, "run_id": "r", "ep": 1}, "local:0", rows=rows, root=root)[1]["version"] == 2   # 不覆盖
