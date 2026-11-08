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
