import json
import sys
from pathlib import Path

from ns2 import step_video as V
import pytest


@pytest.fixture(autouse=True)
def _no_real_handoff(monkeypatch, request):
    """其余用例不连真出图机：交接打桩成“无变化”（交接本身由 test_handoff_pulls_image_host_frames 测）"""
    if request.node.name not in ("test_handoff_pulls_image_host_frames", "test_p3_recheck_remote_frames_and_handoff_fixes"):
        from ns2 import handoff as HO
        monkeypatch.setattr(HO, "sync_frames", lambda *a, **k: {"changed": False})

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


class _AllLoras(set):
    def __contains__(self, x):
        return True


def _frames_takes(tmp_path, monkeypatch):
    """审查 3 M5 之后 preflight 读不到 takes.json / 问不到 LoRA = infra：流程测试给一份本机首尾帧账本 + 假 LoRA 清单"""
    d = tmp_path / "data/renders/r/ep01/S1"
    d.mkdir(parents=True, exist_ok=True)
    (d / "takes.json").write_text(json.dumps({"active": {}, "takes": []}), encoding="utf-8")
    monkeypatch.setattr(V, "comfy_loras", lambda lane: _AllLoras())


def test_run_flow_with_fake_comfy(tmp_path, monkeypatch):
    monkeypatch.setattr(V, "shot_adult", lambda run, sid: False)
    _patch_takes(tmp_path, monkeypatch)
    monkeypatch.setattr(V, "ROOT", tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    _frames_takes(tmp_path, monkeypatch)
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
    _frames_takes(tmp_path, monkeypatch)
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
    monkeypatch.setattr(V, "shot_adult", lambda run, sid: False)
    calls = _fake_render(tmp_path, monkeypatch, lambda clip, meta, host="": {"error": "ffmpeg not found"})
    job = {**_job(max_tries=4), "payload": {"variants": VARS, "ladder": ["v1", "s2"]}}
    st, res, err = V.run(job, "gpu33:1")
    assert st == "blocked" and "infra" in err and calls["submit"] == 1          # 打分崩 ≠ 没过门：不换变体重渲
    monkeypatch.setattr(V, "score", lambda clip, meta, host="": {"gate": {"pass": True}, "penalty": 0.1})
    st, res, err = V.run(job, "gpu33:1")
    assert st == "done" and res["rescored"][0]["passed"] and calls["submit"] == 1   # 重派 = 只补打分，不再渲


def test_adult_untriggered_refused_by_worker(monkeypatch):
    """B2 第二道：队列放错了也不渲——未触发的成人镜 worker 直接 blocked，不提交 ComfyUI"""
    monkeypatch.setattr(V, "shot_adult", lambda run, sid: True)
    monkeypatch.setattr(V, "submit", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该提交")))
    st, _, err = V.run({"id": 1, "run_id": "r", "shot": "ep03-S1", "ep": 3, "tries": 0, "payload": {"variants": {"v1": {}}}}, "gpu33:0")
    assert st == "blocked" and "成人" in err


def test_long_variant_on_short_lane_asks_reroute(monkeypatch):
    monkeypatch.setattr(V, "shot_adult", lambda run, sid: False)
    monkeypatch.setattr(V, "rescore_pending", lambda *a: [])
    monkeypatch.setattr(V, "rendered_count", lambda job: 0)
    job = {"id": 1, "run_id": "r", "shot": "ep01-S1", "ep": 1, "tries": 0,
           "payload": {"variants": {"v1": {"status": "ready", "frames": 300}}, "ladder": ["v1"]}}
    st, res, err = V.run(job, "gpu36:0")
    assert st == "infra" and res["want_lane"] == "gpu36:2"


def test_video_preflight(tmp_path, monkeypatch):
    """M5 / M7：输入缺、LoRA 没装、首帧已换版、尾帧比首帧旧 → 逐条写出来；都齐 → 空"""
    monkeypatch.setattr(V, "ROOT", tmp_path)
    d = tmp_path / "data/renders/r/ep01/S1"; d.mkdir(parents=True)
    (tmp_path / "in.png").write_bytes(b"x")
    def takes(fn, ft, ln, lt):
        (d / "takes.json").write_text(json.dumps({"active": {"image": fn, "last": ln}, "takes": [
            {"stage": "image", "n": fn, "created_at": ft}, {"stage": "last", "n": ln, "created_at": lt}]}))
    job = {"run_id": "r", "shot": "ep01-S1"}
    var = {"inputs": {"@a": "in.png"}, "graph": {"1": {"class_type": "LoraLoaderModelOnly", "inputs": {"lora_name": "x.safetensors"}}}}
    monkeypatch.setattr(V, "comfy_loras", lambda lane: {"x.safetensors"})
    takes(3, "2026-11-08T10:00", 2, "2026-11-08T11:00")
    assert V.preflight(job, var, "gpu36:0", {"first_take": 3, "last_take": 2}) == []
    bad = V.preflight(job, {**var, "inputs": {"@a": "nope.png"}}, "gpu36:0", {"first_take": 2, "last_take": 2})
    assert any("不在本机" in x for x in bad) and any("首帧当前版已变" in x for x in bad)
    monkeypatch.setattr(V, "comfy_loras", lambda lane: set())
    assert any("没装 LoRA" in x for x in V.preflight(job, var, "gpu36:0", {}))
    monkeypatch.setattr(V, "comfy_loras", lambda lane: {"x.safetensors"})
    takes(4, "2026-11-08T12:00", 2, "2026-11-08T11:00")                            # 首帧重出了、尾帧还是旧的
    assert any("比首帧" in x for x in V.preflight(job, var, "gpu36:0", {}))
    import pytest
    monkeypatch.setattr(V, "comfy_loras", lambda lane: None)                       # 审查 3 M5：问不到 ComfyUI → infra，不再跳过
    with pytest.raises(V.PreflightInfra):
        V.preflight(job, var, "gpu36:0", {})
    assert not any("LoRA" in x for x in V.preflight(job, {**var, "graph": {}}, "gpu36:0", {}))   # 不用 LoRA 的变体不问、不报 infra
    (d / "takes.json").unlink()                                                    # takes.json 读不到 → infra
    with pytest.raises(V.PreflightInfra):
        V.preflight(job, {**var, "graph": {}}, "gpu36:0", {})


def test_handoff_pulls_image_host_frames(tmp_path, monkeypatch):
    """P3 真机 bug：视频机用的是本机旧首帧。交接：拉出图机当前首 / 尾帧文件 + take，本机 active 指过去；同号不同文件 → 冲突不覆盖"""
    import pytest
    from ns2 import handoff as HO
    from ns2 import worker as W
    src = tmp_path / "src"; dst = tmp_path / "dst"
    sd = src / "data/renders/r/ep01/S2"; (sd / "takes").mkdir(parents=True)
    (sd / "takes/image_014.png").write_bytes(b"new-first"); (sd / "takes/last_004.png").write_bytes(b"new-last")
    (sd / "takes.json").write_text(json.dumps({"active": {"image": 14, "last": 4}, "takes": [
        {"stage": "image", "n": 14, "file": "renders/r/ep01/S2/takes/image_014.png"},
        {"stage": "last", "n": 4, "file": "renders/r/ep01/S2/takes/last_004.png"}]}))
    srv = W.serve_files("127.0.0.1", 0, src)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    tj = dst / "takes.json"; dst.mkdir()
    tj.write_text(json.dumps({"active": {"image": 1, "video": 1}, "takes": [{"stage": "image", "n": 1, "file": "renders/r/ep01/S2/takes/image_001.png"}]}))
    monkeypatch.setattr(P, "takes_path", lambda sid, run=None: tj)
    out = HO.sync_frames("r", "ep01-S2", "gpu33", dst, base=base)
    d = json.loads(tj.read_text())
    assert out["changed"] and d["active"]["image"] == 14 and d["active"]["last"] == 4 and d["active"]["video"] == 1
    assert (dst / "data/renders/r/ep01/S2/takes/image_014.png").read_bytes() == b"new-first"
    assert any(t.get("synced_from") == "gpu33" for t in d["takes"])
    assert HO.sync_frames("r", "ep01-S2", "gpu33", dst, base=base)["changed"] is False          # 再跑一遍不变
    d["takes"][-1]["file"] = "renders/r/ep01/S2/takes/other.png"; tj.write_text(json.dumps(d))     # 本机同号不同文件
    monkeypatch.setattr(P, "mutate_takes", lambda sid, fn, **k: fn(json.loads(tj.read_text())))
    with pytest.raises(HO.HandoffConflict):
        HO.sync_frames("r", "ep01-S2", "gpu33", dst, base=base)
    srv.shutdown()


def test_no_variant_reason_never_empty():
    """审查 3 §4：pick_variant 为 None 的线上路径原因不再是空的"""
    assert "0 个变体" in V.no_variant_reason({"variants": {}}, "gpu33:0")
    r = V.no_variant_reason({"variants": {"v1": {"status": "needs_writer", "reason": "缺动作描述"}, "s2": {}}, "ladder": ["v1", "s2"]}, "gpu33:0")
    assert "v1=needs_writer（缺动作描述）" in r and "s2=无状态" in r and "ready 的：无" in r
    long_ = V.no_variant_reason({"variants": {"v1": {"status": "ready", "frames": V.LONG_FRAMES + 1}}}, "gpu33:0")
    assert f"只能 {V.LONG_LANE}" in long_


def test_m7_seed_ledger_never_reuses(tmp_path, monkeypatch):
    """审查 3 M7：同镜用过的种子不再用——第一次用变体自带种子，之后（含 infra 重试）换新；不同镜互不影响；没有种子节点不记账"""
    monkeypatch.setattr(V, "ROOT", tmp_path)
    var = {"seed": 42, "graph": {"1": {"class_type": "RandomNoise", "inputs": {"noise_seed": 42}}}}
    a = V.pick_seed("r", "ep01-S1", var, None)
    b = V.pick_seed("r", "ep01-S1", var, None)
    c = V.pick_seed("r", "ep01-S1", var, a)                    # fresh 撞上用过的 → 也换
    assert a == 42 and b != 42 and c not in (a, b)
    assert V.pick_seed("r", "ep01-S2", var, None) == 42
    led = json.loads(V.seed_ledger_path("r").read_text(encoding="utf-8"))
    assert led["ep01-S1"] == [a, b, c] and led["ep01-S2"] == [42]
    assert V.pick_seed("r", "ep01-S3", {"seed": 5, "graph": {}}, None) is None and "ep01-S3" not in json.loads(V.seed_ledger_path("r").read_text(encoding="utf-8"))


def test_p3_recheck_remote_frames_and_handoff_fixes(tmp_path, monkeypatch):
    """审查 3 §4：提交前复核出图机当前版（变了 → infra 回队）；交接时出图机没有首帧 → 本机也去掉；同名不同大小 → 冲突；op_id 不再确定性"""
    import pytest
    from ns2 import handoff as HO
    job = {"run_id": "r", "ep": 2, "shot": "ep02-S1"}                   # ep02 出图在 gpu36
    monkeypatch.setattr(HO, "remote_active", lambda run, sid, src, base=None: {"image": 14, "last": 4})
    assert V.recheck_remote_frames(job, {"first_take": 14, "last_take": 4}, "gpu33") is None
    assert "v13 变成 v14" in V.recheck_remote_frames(job, {"first_take": 13, "last_take": 4}, "gpu33")
    assert V.recheck_remote_frames(job, {"first_take": 13}, "gpu36") is None          # 本机就是出图机
    assert V.recheck_remote_frames(job, {"echo": True, "first_take": 1}, "gpu33") is None
    # 交接：出图机 active 没有 image → 本机去掉旧首帧；同名文件大小不同 → 冲突
    root = tmp_path
    doc = {"active": {"image": 3, "last": 2}, "takes": [{"stage": "image", "n": 3, "file": "a.png"}]}
    import sys, types
    P = types.SimpleNamespace(mutate_takes=lambda sid, fn, **k: (ops.append(k["op_id"]), fn(doc)))
    ops = []
    mod = types.ModuleType("ns_render.pipeline"); mod.mutate_takes = P.mutate_takes
    pkg = types.ModuleType("ns_render"); pkg.pipeline = mod
    monkeypatch.setitem(sys.modules, "ns_render", pkg); monkeypatch.setitem(sys.modules, "ns_render.pipeline", mod)
    remote = {"active": {"last": 2}, "takes": [{"stage": "last", "n": 2, "file": "renders/r/ep02/S1/takes/last_002.png"}]}
    (root / "data/renders/r/ep02/S1/takes").mkdir(parents=True)
    (root / "data/renders/r/ep02/S1/takes/last_002.png").write_bytes(b"12345")
    monkeypatch.setattr(HO, "_get", lambda base, rel, timeout=120: json.dumps(remote).encode())
    monkeypatch.setattr(HO, "remote_size", lambda base, rel: 5)
    out = HO.sync_frames("r", "ep02-S1", "gpu36", root, base="http://x")
    assert ops, ("mutate 没走假模块", out)
    assert "image" not in doc["active"] and out["changed"] and ops[0].startswith("ns2-handoff-gpu36-None-2-")
    HO.sync_frames("r", "ep02-S1", "gpu36", root, base="http://x")
    assert ops[0] != ops[1]                                                             # 每次唯一，回摆不被吞
    monkeypatch.setattr(HO, "remote_size", lambda base, rel: 999)
    with pytest.raises(HO.HandoffConflict):
        HO.sync_frames("r", "ep02-S1", "gpu36", root, base="http://x")
