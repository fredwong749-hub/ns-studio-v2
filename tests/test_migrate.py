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
