from ns2 import migrate as M


def row(shot, ep, first="done", last="done", video="done"):
    return {"shot": shot, "ep": ep, "lane_images": M.host_for("images", ep), "lane_video": M.host_for("video", ep),
            "first": {"state": first}, "last": {"state": last}, "video": {"state": video}}


def test_partition_split_images_vs_video():
    assert (M.host_for("images", 1), M.host_for("video", 1)) == ("gpu33", "gpu36")
    assert (M.host_for("images", 3), M.host_for("video", 3)) == ("gpu33", "gpu33")


def test_merge_takes_each_step_from_its_own_host():
    g33 = [row("ep01-S1", 1, video="queued")]                       # 出图机：视频副本落后
    g36 = [row("ep01-S1", 1, first="queued", video="best_effort")]  # 视频机：首帧副本落后
    m = M.merge({"gpu33": g33, "gpu36": g36}, {"gpu33": g33, "gpu36": g36})[0]
    assert (m["first"]["state"], m["video"]["state"]) == ("done", "best_effort")


def test_last_na_when_video_exists_without_last():
    assert M.na_last(row("ep01-S4", 1, last="queued"))["last"]["state"] == "na"
    assert M.na_last(row("ep03-S4", 3, last="queued", video="queued"))["last"]["state"] == "queued"


def _shot(root, s, takes, active, review=None):
    import json
    d = root / "renders/r/ep01" / s
    (d / "takes").mkdir(parents=True)
    for t in takes:
        (root / t["file"]).write_bytes(b"x")
    (d / "takes.json").write_text(json.dumps({"active": active, "takes": takes}))
    if review:
        (d / "review.json").write_text(json.dumps({"state": review}))


def test_m9_review_json_and_master_reject_and_bad_file(tmp_path, monkeypatch):
    """M9：旧 review.json passed → 视频 done（postflight 过期也算）；rejected → needs_review；视频 master_reject → needs_review；
    坏 takes.json 不 abort 整份"""
    import ns2.gates as G
    monkeypatch.setattr(G, "frame_passed", lambda stage, t, locked=False: (True, ""))
    v = lambda s, ok, **k: {"stage": "video", "n": 1, "file": f"renders/r/ep01/{s}/takes/v.mp4", "postflight": {"ok": ok}, **k}
    _shot(tmp_path, "S1", [v("S1", False)], {"video": 1}, review="passed")
    _shot(tmp_path, "S2", [v("S2", True)], {"video": 1}, review="rejected")
    _shot(tmp_path, "S3", [v("S3", True, master_reject={"why": "x"})], {"video": 1})
    (tmp_path / "renders/r/ep01/S4").mkdir(parents=True)
    (tmp_path / "renders/r/ep01/S4/takes.json").write_text("{坏")
    rows = {r["shot"]: r for r in M.scan(tmp_path, "r")}
    assert rows["ep01-S1"]["video"]["state"] == "done"
    assert rows["ep01-S2"]["video"]["state"] == "needs_review" and "rejected" in rows["ep01-S2"]["video"]["why"]
    assert rows["ep01-S3"]["video"]["state"] == "needs_review"
    assert rows["ep01-S4"]["video"]["state"] == "missing_file"


def test_models_check(tmp_path):
    from ns2 import models_check as MC
    d = tmp_path / "loras"; d.mkdir()
    (d / "a.safetensors").write_bytes(b"aaa"); (d / "extra.safetensors").write_bytes(b"e")
    import hashlib
    good = hashlib.sha256(b"aaa").hexdigest()
    lock = {"loras": [{"file": "a.safetensors", "sha256": good}, {"file": "b.safetensors", "sha256": None},
                      {"file": "a.safetensors", "sha256": "f" * 64}]}
    r = MC.check(lock, d, "h")
    assert r["rows"][0]["match"] is True and r["rows"][1]["present"] is False and r["rows"][2]["match"] is False
    assert r["unlisted"] == ["extra.safetensors"] and "a.safetensors" in r["dup_names"]
    md = MC.merge({"h": r})
    assert "未校验（锁表 sha256 = null）：1" in md and "不一致：1" in md and "缺文件：1" in md
