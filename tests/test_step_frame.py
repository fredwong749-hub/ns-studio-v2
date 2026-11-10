from ns2 import step_frame as F


def job(tries=0, result=None):
    return {"id": 1, "step": "last", "shot": "ep01-S1", "run_id": "smoke_v2", "tries": tries, "payload": {}, "result": result}


def test_mapping_and_prev_blocking_carried(monkeypatch):
    seen = {}

    def fake(args, host, timeout_s=3600):
        seen.update(args)
        return {"outcome": "fail", "take": 3, "blocking": ["FL"], "reasons": ["首尾太像"]}
    monkeypatch.setattr(F, "call_legacy", fake)
    st, res, err = F.run(job(), "gpu33:0")
    assert st == "retry" and res["prev_blocking"] == [["FL"]] and "FL" in err and seen["attempt"] == 1 and seen["port"] == 8188
    F.run(job(1, res), "gpu33:0")
    assert seen["prev_blocking"] == [["FL"]] and seen["attempt"] == 2                # 下一次带上上次的不过项（FL → 换构图）


def test_outcomes(monkeypatch):
    for o, want in (("pass", "done"), ("na", "na"), ("blocked", "blocked"), ("infra", "infra")):
        monkeypatch.setattr(F, "call_legacy", lambda a, h, timeout_s=3600, o=o: {"outcome": o, "error": "x"})
        assert F.run(job(), "gpu36:2")[0] == want


def test_infra_after_render_rescores_only(monkeypatch):
    seen = []
    outs = iter([{"outcome": "infra", "take": 7, "error": "score_infra"}, {"outcome": "pass", "take": 7, "rescored": True}])
    monkeypatch.setattr(F, "call_legacy", lambda a, h, timeout_s=3600: (seen.append(dict(a)), next(outs))[1])
    st, res, _ = F.run(job(), "gpu33:0")
    assert st == "infra" and res["infra_take"] == 7 and "rescore_take" not in seen[0]
    st, res, _ = F.run(job(0, res), "gpu33:0")
    assert seen[1]["rescore_take"] == 7 and st == "done" and "infra_take" not in res      # 第二次只重打分


def test_p6_last_all_too_similar_falls_back_to_i2va():
    """尾帧用完次数、候选全“与首帧太像”→ 不选雷同尾帧，写 last_frame_fallback=i2va（绑定当前首帧）"""
    import types
    from ns2 import legacy_frame as LF
    d = {"active": {"image": 4}, "takes": []}
    P = types.SimpleNamespace(mutate_takes=lambda sid, fn, **k: fn(d))
    r = LF.i2va_fallback(P, "ep05-S3", d)
    assert r["fallback"] == "i2va" and d["last_frame_fallback"]["first_n"] == 4
    assert LF.i2va_fallback(P, "ep05-S3", d)["why"] == "已有尾帧兜底标记"
    assert LF.i2va_fallback(P, "x", {"active": {}})["promoted"] is None


def test_no_board_fallback(monkeypatch):
    """R4 质量 7：NS2_NO_BOARD_FALLBACK=1 → 双人首帧第 3 次的“参考板（B）”改回“造型设定图（A）”换 seed；默认关原样"""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("lf", Path(__file__).resolve().parents[1] / "ns2" / "legacy_frame.py")
    LF = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(LF)
    b = {"name": "第 3 次：退到 两张脸 + 参考板（B）", "seed": 7, "plan_kw": {"two_person": "both_faces"}, "board": True, "variant": "faces_board"}
    monkeypatch.delenv("NS2_NO_BOARD_FALLBACK", raising=False)
    assert LF.no_board_fallback(b, 3) is b
    monkeypatch.setenv("NS2_NO_BOARD_FALLBACK", "1")
    r = LF.no_board_fallback(b, 3)
    assert "board" not in r and r["variant"] == "faces_look" and r["seed"] == 7 and r["plan_kw"] == {"two_person": "both_faces"}
    a = {"name": "第 1 次", "seed": 1, "plan_kw": {"two_person": "both_faces"}, "variant": "faces_look"}
    assert LF.no_board_fallback(a, 1) is a and LF.no_board_fallback(None, 3) is None
