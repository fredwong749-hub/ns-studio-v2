"""R4 质量账：口径（原始 postflight，不过尽力版视图）"""
import json


def _take(n, stage, fails=(), q1=None, fl=None):
    special = {"Q1"} if q1 else set()
    special |= {"FL"} if fl else set()
    items = [{"id": f, "title": f"t{f}", "status": "fail"} for f in fails if f not in special] + [{"id": "Q5", "status": "pass"}]
    if q1:
        items.append({"id": "Q1", "title": "tQ1", "status": "fail" if "Q1" in fails else "pass",
                      "evidence": {"per_face": [{"as": q1[0], "sim": q1[1], "verdict": q1[2]}]}})
    if fl:
        items.append({"id": "FL", "status": "fail" if "FL" in fails else "pass", "evidence": {"struct": fl[0], "ssim": fl[1]}})
    return {"n": n, "stage": stage, "postflight": {"ok": not fails, "items": items}}


def test_frame_stats_first_try_and_fail_ranking():
    from ns2 import quality_report as Q
    docs = {
        "ep05-S1": {"takes": [_take(1, "image", ["Q1"], q1=("lead_f", 0.55, "复核带")), _take(2, "image", q1=("lead_f", 0.7, "过")),
                              _take(3, "last", ["FL"], fl=(0.9, 0.6)), _take(4, "last", fl=(0.5, 0.4))],
                    "best_effort": {"image": {"n": 1}}, "active": {"image": 1}},        # 尽力版标记不改口径
        "ep05-S2": {"takes": [_take(2, "image"), _take(1, "image", ["G1", "Q1"], q1=("ch_x", 0.3, "不像")), {"n": 9, "stage": "video"}]},
    }
    r = Q.frame_stats(docs)
    im = r["image"]
    assert im["takes"] == 4 and im["pass"] == 2 and im["first_try_pass"] == 0 and im["first_try_rate"] == 0.0
    assert im["first_try_by_shot"] == {"ep05-S1": False, "ep05-S2": False}      # 按 n 排序：S2 第一版是 n=1
    assert im["fails"][0] == {"id": "Q1", "n": 2, "share": 0.5, "first_try": 2, "title": "tQ1"}
    assert r["last"]["first_try_rate"] == 0.0 and r["last"]["fails"][0]["id"] == "FL"
    assert {(x["who"], x["verdict"]) for x in r["q1"]} == {("lead_f", "复核带"), ("lead_f", "过"), ("ch_x", "不像")}
    assert r["fl"]["struct"]["max"] == 0.9 and r["fl"]["struct"]["n"] == 2


def test_video_stats_and_load(tmp_path):
    from ns2 import quality_report as Q
    sd = tmp_path / "data/renders/r/ep05/S1"
    sd.mkdir(parents=True)
    (sd / "takes.json").write_text(json.dumps({"takes": [_take(1, "image")]}), encoding="utf-8")
    vd = tmp_path / "data/pipeline/runs/r/video/scores"
    vd.mkdir(parents=True)
    (vd / "ep05-S1__v1.json").write_text(json.dumps({"mean": 0.6, "still": 0.5, "gate": {"pass": True, "fails": []}}), encoding="utf-8")
    (vd / "ep05-S1__s2.json").write_text(json.dumps({"mean": 2.0, "gate": {"pass": False, "fails": ["脸部泛红"]}}), encoding="utf-8")
    (vd / "ep06-S1__v1.json").write_text(json.dumps({"mean": 9}), encoding="utf-8")          # 别的集不算
    (vd / "ep05-S2__v1.json").write_text("{坏", encoding="utf-8")
    r = Q.report(tmp_path, "r", [5])
    assert r["shots"] == 1 and r["frames"]["image"]["first_try_rate"] == 1.0
    v = r["video"]
    assert v["clips"] == 2 and v["gate_pass"] == 1 and v["low_motion_lt_1"] == 1 and v["fails"] == {"脸部泛红": 1}
    assert v["motion_mean"]["min"] == 0.6
