import json
import sys
from pathlib import Path

from ns2 import step_video as V

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "services/render_router"), str(ROOT / "services/agent"), str(ROOT / "packages/schema")]
from ns_render import pipeline as P  # noqa: E402

VARS = {"v1": {"status": "ready", "frames": 124, "seed": 1, "graph": {}, "inputs": {}},
        "s2": {"status": "ready", "frames": 124, "seed": 2, "graph": {}, "inputs": {}},
        "r24": {"status": "lint_error", "frames": 124},
        "L": {"status": "ready", "frames": 300, "seed": 3}}


def test_pick_variant_ladder_and_long_lane():
    p = {"variants": VARS, "ladder": ["v1", "r24", "s2", "L"]}
    assert V.pick_variant(p, 0, "gpu33:0")[0] == "v1"
    assert V.pick_variant(p, 1, "gpu33:0")[0] == "s2"            # 跳过不可派的 r24、长镜头不进普通道
    assert V.pick_variant(p, 2, "gpu36:2")[0] == "L"
    assert V.pick_variant({"variants": {"x": {"status": "needs_writer"}}}, 0, "gpu36:0") is None


def _job(tries=0, max_tries=2, jid=7):
    return {"id": jid, "shot": "ep01-S1", "run_id": "r", "ep": 1, "tries": tries, "max_tries": max_tries, "lane": "gpu36:0"}


def _patch_takes(tmp_path, monkeypatch):
    tj = tmp_path / "takes.json"
    tj.write_text(json.dumps({"takes": [{"n": 3, "stage": "video", "file": "old.mp4"}], "active": {"video": 3}}), encoding="utf-8")
    monkeypatch.setattr(P, "takes_path", lambda sid, run=None: tj)
    return tj


def test_record_take_pass_sets_active_and_is_idempotent(tmp_path, monkeypatch):
    tj = _patch_takes(tmp_path, monkeypatch)
    s = {"gate": {"pass": True}, "penalty": 0.1, "mean": 2.7}
    r = V.record_take(_job(), "v1", VARS["v1"], "pipeline/x.mp4", s, final=False)
    V.record_take(_job(), "v1", VARS["v1"], "pipeline/x.mp4", s, final=False)      # 同 job 同次重报 → 不重复写
    d = json.loads(tj.read_text(encoding="utf-8"))
    assert r["n"] == 4 and d["active"]["video"] == 4 and len(d["takes"]) == 2


def test_record_take_final_fail_picks_min_penalty(tmp_path, monkeypatch):
    tj = _patch_takes(tmp_path, monkeypatch)
    V.record_take(_job(0), "v1", VARS["v1"], "a.mp4", {"gate": {"pass": False}, "penalty": 0.5}, final=False)
    d = json.loads(tj.read_text(encoding="utf-8")); assert d["active"]["video"] == 3          # 没过不动当前版
    r = V.record_take(_job(1), "s2", VARS["s2"], "b.mp4", {"gate": {"pass": False}, "penalty": 0.9}, final=True)
    d = json.loads(tj.read_text(encoding="utf-8"))
    assert r["best_effort_n"] == 4 and d["active"]["video"] == 4                              # penalty 最小的那条


def test_run_flow_with_fake_comfy(tmp_path, monkeypatch):
    _patch_takes(tmp_path, monkeypatch)
    monkeypatch.setattr(V, "ROOT", tmp_path)
    (tmp_path / "data").mkdir()
    out = tmp_path / "out.mp4"; out.write_bytes(b"x")
    monkeypatch.setattr(V, "submit", lambda lane, var, prefix, tag, seed=None: "pid1")
    monkeypatch.setattr(V, "wait", lambda lane, pid, timeout_s: {"path": str(out), "gpu_s": 400})
    monkeypatch.setattr(V, "score", lambda clip, meta, host="": {"gate": {"pass": False, "fails": ["动作不足"]}, "penalty": 0.4})
    st, res, err = V.run({**_job(), "payload": {"variants": VARS, "ladder": ["v1", "s2"]}}, "gpu36:0")
    assert st == "retry" and res["has_output"] and "动作不足" in err
    assert (tmp_path / "data/pipeline/runs/r/video/clips/ep01/ep01-S1__v1_s1.mp4").is_file()


def test_trial_keeps_active_and_reseed(tmp_path, monkeypatch):
    tj = _patch_takes(tmp_path, monkeypatch)
    V.record_take(_job(), "v1", VARS["v1"], "t.mp4", {"gate": {"pass": True}, "penalty": 0}, final=True, seed=99, trial=True)
    d = json.loads(tj.read_text(encoding="utf-8"))
    assert d["active"]["video"] == 3 and d["takes"][-1]["seed"] == 99 and d["takes"][-1]["trial"] is True
    g = {"1": {"class_type": "RandomNoise", "inputs": {"noise_seed": 1}}, "2": {"class_type": "X", "inputs": {}}}
    assert V.reseed(g, 7) == 1 and g["1"]["inputs"]["noise_seed"] == 7


def _fake_render(tmp_path, monkeypatch, score_fn):
    _patch_takes(tmp_path, monkeypatch)
    monkeypatch.setattr(V, "ROOT", tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    out = tmp_path / "out.mp4"; out.write_bytes(b"x")
    calls = {"submit": 0}

    def sub(lane, var, prefix, tag, seed=None):
        calls["submit"] += 1
        return "pid"
    monkeypatch.setattr(V, "submit", sub)
    monkeypatch.setattr(V, "wait", lambda lane, pid, timeout_s: {"path": str(out), "gpu_s": 1})
    monkeypatch.setattr(V, "score", score_fn)
    return calls


def test_score_infra_error_blocks_without_rerender_then_rescore_passes(tmp_path, monkeypatch):
    calls = _fake_render(tmp_path, monkeypatch, lambda clip, meta, host="": {"error": "ffmpeg not found"})
    job = {**_job(max_tries=4), "payload": {"variants": VARS, "ladder": ["v1", "s2"]}}
    st, res, err = V.run(job, "gpu33:1")
    assert st == "blocked" and "infra" in err and calls["submit"] == 1          # 打分崩 ≠ 没过门：不换变体重渲
    monkeypatch.setattr(V, "score", lambda clip, meta, host="": {"gate": {"pass": True}, "penalty": 0.1})
    st, res, err = V.run(job, "gpu33:1")
    assert st == "done" and res["rescored"][0]["passed"] and calls["submit"] == 1   # 重派 = 只补打分，不再渲
