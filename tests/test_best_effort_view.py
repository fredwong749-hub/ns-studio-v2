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
