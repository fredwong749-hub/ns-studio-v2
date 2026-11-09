"""P6 恢复 ep05：尽力版读视图（best_effort_view）+ build_jobs 入口（legacy_build_jobs）+ 尾帧 preflight 接线"""
import json
import sys
import types
from pathlib import Path

NS2 = Path(__file__).resolve().parents[1] / "ns2"
if str(NS2) not in sys.path:
    sys.path.insert(0, str(NS2))

import best_effort_view as BV  # noqa: E402


def _doc(active_img=2, mark_n=2):
    return {"active": {"image": active_img, "last": 1},
            "best_effort": {"image": {"n": mark_n, "by": "ns2-best-effort"}},
            "takes": [{"stage": "image", "n": 1, "file": "a1.png", "postflight": {"ok": False, "blocking": ["Q1", "G1"]}},
                      {"stage": "image", "n": 2, "file": "a2.png", "postflight": {"ok": False, "blocking": ["G1"], "items": [{"id": "Q9", "status": "fail"}]}},
                      {"stage": "last", "n": 1, "file": "l1.png", "postflight": {"ok": False, "blocking": ["FL"]}}]}


def test_view_marks_only_marked_active_take_and_does_not_mutate():
    d = _doc()
    raw = json.dumps(d, sort_keys=True)
    v = BV.view_doc(d)
    assert json.dumps(d, sort_keys=True) == raw                                   # 原文档不动（证据不改）
    t1, t2, l1 = v["takes"]
    assert t2["postflight"]["ok"] is True and t2["postflight"]["review_needed"] and t2["postflight"][BV.FLAG]
    assert t2["postflight"]["orig_ok"] is False and t2["postflight"]["orig_blocking"] == ["G1"]
    assert t1["postflight"]["ok"] is False and l1["postflight"]["ok"] is False     # 没标记的版本 / 阶段原样


def test_view_ignores_stale_mark_and_non_takes_docs():
    assert BV.view_doc(_doc(active_img=1, mark_n=2))["takes"][1]["postflight"]["ok"] is False   # 当前已换走 → 标记失效
    assert BV.marked(_doc(active_img=1, mark_n=2), "image") is None
    other = {"shots": [1]}
    assert BV.view_doc(other) is other
    nomark = {"active": {"image": 1}, "takes": [{"stage": "image", "n": 1, "postflight": {"ok": False}}]}
    assert BV.view_doc(nomark) is nomark


def test_legacy_build_jobs_patch_view_q9_anomaly_and_sop_gate():
    import legacy_build_jobs as LB
    BJ = types.SimpleNamespace(json=json, _q9_ok=lambda pf: False, anomaly_block=lambda sid, f, pf, rows: "重复人头")
    G = types.SimpleNamespace(filter_video_specs=lambda run, specs, root=None: {})
    LB.patch(BJ, G)
    d = BJ.json.loads(json.dumps(_doc()))                                          # build_jobs 读 takes.json 都走这里
    pf = d["takes"][1]["postflight"]
    assert pf["ok"] and BJ._q9_ok(pf) and BJ.anomaly_block("ep05-S1", "a2.png", pf, {}) is None
    raw_pf = d["takes"][0]["postflight"]                                           # 不是尽力版的照旧判
    assert not BJ._q9_ok(raw_pf) and BJ.anomaly_block("ep05-S1", "a1.png", raw_pf, {}) == "重复人头"
    assert G.filter_video_specs("series10_v2", {"ep05-S2": 1}) == {"ep05-S2": 1}   # 视频闸门由队列前置代替
    assert BJ.json.dumps({"a": 1}) == '{"a": 1}'


def test_step_video_compiles_through_ns2_wrapper(monkeypatch, tmp_path):
    from ns2 import step_video as SV
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")
    monkeypatch.setattr(SV.subprocess, "run", fake_run)
    monkeypatch.setattr(SV, "ROOT", tmp_path)
    pl, err = SV.compile_payload({"run_id": "series10_v2", "shot": "ep05-S2"}, "gpu33")
    assert pl is None and "build_jobs rc=1" in err
    assert seen["cmd"][1].endswith("legacy_build_jobs.py") and seen["cmd"][2:] == ["--run", "series10_v2"]


def test_legacy_frame_last_preflight_sees_marked_first_as_passed():
    """legacy_frame.main 里尾帧那段补丁的同一判定：只对带标记的当前首帧那一张放行"""
    d = _doc()
    fbe = BV.marked(d, "image")
    orig = BV.take_pf
    key = (fbe.get("n"), fbe.get("file"))
    take_pf = lambda t, _o=orig, _k=key: BV.as_passed(_o(t)) if t and t.get("stage") == "image" and (t.get("n"), t.get("file")) == _k else _o(t)  # noqa: E731
    assert take_pf(d["takes"][1])["ok"] is True
    assert take_pf(d["takes"][0])["ok"] is False
    src = (NS2 / "legacy_frame.py").read_text(encoding="utf-8")
    assert 'BV.marked(P.load_takes(a["sid"]), "image")' in src and "PR.take_pf = lambda" in src


def _jobs_takes(tmp_path, last_in_jobs, active_last):
    run, sid = "series10_v2", "ep05-S1"
    jd = tmp_path / "data/pipeline/runs" / run / "video"
    jd.mkdir(parents=True)
    sh = {"shot": sid, "first_frame_take": 2, "variants": [{"id": "v1"}]}
    if last_in_jobs:
        sh.update(last_frame="x.png", last_frame_take=last_in_jobs)
    (jd / "jobs.json").write_text(json.dumps({"shots": [sh]}), encoding="utf-8")
    td = tmp_path / "data/renders" / run / "ep05/S1"
    td.mkdir(parents=True)
    (td / "takes.json").write_text(json.dumps({"active": {"image": 2, "last": active_last}}), encoding="utf-8")
    return {"run_id": run, "shot": sid}


def test_compile_recompiles_when_takes_gained_a_last_frame(monkeypatch, tmp_path):
    """ep05-S1 11-09 21:43：takes 有当前尾帧、作业表这镜没尾帧 → 先重编，不拿旧表（旧表变体全 no_last_frame）"""
    from ns2 import step_video as SV
    from ns2 import enqueue_video as EV
    job = _jobs_takes(tmp_path, None, 3)
    calls = []
    monkeypatch.setattr(SV.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(SV, "ROOT", tmp_path)
    monkeypatch.setattr(EV, "build", lambda run, sid, host: (None, None, {"payload": {"variants": [1]}}))
    pl, err = SV.compile_payload(job, "gpu33")
    assert calls and err is None and pl == {"variants": [1]}       # 重编过；编后作业表仍无尾帧（I2VA）= 放宽接受


def test_compile_skips_build_when_jobs_table_matches(monkeypatch, tmp_path):
    from ns2 import step_video as SV
    from ns2 import enqueue_video as EV
    job = _jobs_takes(tmp_path, 3, 3)
    calls = []
    monkeypatch.setattr(SV.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(SV, "ROOT", tmp_path)
    monkeypatch.setattr(EV, "build", lambda run, sid, host: (None, None, {"payload": {"variants": [1]}}))
    assert SV.compile_payload(job, "gpu33") == ({"variants": [1]}, None) and not calls


def test_video_host_trusts_image_host_for_accept_and_regen_holds():
    """11-09 gpu36 出 ep05 视频：验收记录 / 驳回账是出图机本机的，视频机上旧的那份不再复判（只对交接来的镜）"""
    import legacy_build_jobs as LB
    BJ = types.SimpleNamespace(json=json, _q9_ok=lambda pf: True, anomaly_block=lambda *a: None,
                               accept_status=lambda sid, f, pf, rows: None, regen_holds=lambda ids: {i: "regen：旧驳回" for i in ids})
    G = types.SimpleNamespace(filter_video_specs=None)
    LB.patch(BJ, G, LB.remote_shot_fn("gpu36"))
    assert BJ.accept_status("ep05-S9", "a.png", {}, {}) == "pass"                   # ep05 出图在 gpu33 → 本机 gpu36 不复判
    assert BJ.accept_status("ep02-S1", "a.png", {}, {}) is None                     # ep02 出图就在 gpu36 → 照旧
    assert BJ.regen_holds(["ep05-S5", "ep02-S3"]) == {"ep02-S3": "regen：旧驳回"}
    assert LB.remote_shot_fn(None)("ep05-S1") is False
