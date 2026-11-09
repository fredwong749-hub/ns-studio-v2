"""审查 3 §5b：promote_best 与 auto_current 对账（规则 4）+ 成人拒绝 + promote_only 接上线"""
import sys
import types


def _fake_legacy(monkeypatch, takes_doc, passes=lambda t: False, lock=None, too_similar=lambda pf: False):
    """legacy_frame.promote_best 用到的旧模块：ns_render.auto_current / ns_agent.gen_contract.first_last 换成假的"""
    AC = types.ModuleType("ns_render.auto_current")
    AC.passes = passes
    AC.owner_lock = lambda d, stage: lock
    AC.take_pf = lambda t: t.get("postflight") or {}
    FL = types.ModuleType("ns_agent.gen_contract.first_last")
    FL.too_similar = too_similar
    nr, na, ng = types.ModuleType("ns_render"), types.ModuleType("ns_agent"), types.ModuleType("ns_agent.gen_contract")
    nr.auto_current, ng.first_last = AC, FL
    for name, mod in (("ns_render", nr), ("ns_render.auto_current", AC), ("ns_agent", na),
                      ("ns_agent.gen_contract", ng), ("ns_agent.gen_contract.first_last", FL)):
        monkeypatch.setitem(sys.modules, name, mod)
    calls = []

    def set_active(sid, stage, n, **k):
        calls.append(("set_active", stage, n))
        takes_doc.setdefault("active", {})[stage] = n

    P = types.SimpleNamespace(load_takes=lambda sid: takes_doc, ineligible_reason=lambda t: t.get("bad"),
                              set_active=set_active, mutate_takes=lambda sid, fn, **k: fn(takes_doc))
    return P, calls


def _tk(n, blocking, **k):
    return {"stage": "image", "n": n, "postflight": {"ok": False, "blocking": blocking}, **k}


def test_promote_best_order_lock_ineligible_adult(monkeypatch):
    """不过项最少优先、同等取最新；坏图 / 驳回不选；项目方定稿锁不动；已有合格当前不动；成人未触发拒绝；打来源标记"""
    from ns2 import legacy_frame as LF
    doc = {"active": {"image": 1}, "takes": [_tk(1, ["Q1", "Q6"]), _tk(2, ["Q1"]), _tk(3, ["Q6"]), _tk(4, [], bad="坏图")]}
    P, calls = _fake_legacy(monkeypatch, doc)
    assert LF.promote_best(P, "ep05-S1", "image", adult=True, triggered=False)["refused"] == "adult" and calls == []
    r = LF.promote_best(P, "ep05-S1", "image", adult=False)
    assert r["promoted"] == 3 and calls == [("set_active", "image", 3)]          # v2 / v3 各 1 项不过 → 取较新的 v3；v4 坏图不选
    assert doc["best_effort"]["image"]["n"] == 3 and doc["best_effort"]["image"]["blocking"] == ["Q6"]
    assert LF.promote_best(P, "ep05-S1", "image", adult=True, triggered=True)["promoted"] == 3   # 成人镜已触发 → 照常
    P2, calls2 = _fake_legacy(monkeypatch, {"active": {"image": 1}, "takes": [_tk(1, ["Q1"]), _tk(2, [])]}, lock=1)
    assert LF.promote_best(P2, "s", "image", adult=False)["why"] == "项目方定稿锁着，不动" and calls2 == []
    P3, calls3 = _fake_legacy(monkeypatch, {"active": {"image": 2}, "takes": [_tk(2, [])]}, passes=lambda t: t["n"] == 2)
    assert LF.promote_best(P3, "s", "image", adult=False)["promoted"] is None and calls3 == []
    P4, _ = _fake_legacy(monkeypatch, {"active": {}, "takes": [_tk(1, ["Q1"], bad="驳回")]})
    assert LF.promote_best(P4, "s", "image", adult=False)["promoted"] is None


def test_promote_best_default_is_adult_fail_closed(monkeypatch):
    """调用方没说是否成人 → 当成人（拒绝提升）"""
    from ns2 import legacy_frame as LF
    P, calls = _fake_legacy(monkeypatch, {"active": {}, "takes": [_tk(1, ["Q1"])]})
    assert LF.promote_best(P, "s", "image")["refused"] == "adult" and calls == []


def test_rule4_rescore_does_not_clear_promoted_best_effort(monkeypatch):
    """auto_current 规则 4：当前是带标记的尽力版 → 重打分仍不过时不清空；没标记照旧清空；有过契约版本照常退回"""
    AC = types.ModuleType("ns_render.auto_current")
    AC.decide = lambda data, stage, n: ("clear", None, "契约没过；这一阶段没有过契约的版本，清空当前")
    nr = types.ModuleType("ns_render")
    nr.auto_current = AC
    monkeypatch.setitem(sys.modules, "ns_render", nr)
    monkeypatch.setitem(sys.modules, "ns_render.auto_current", AC)
    from ns2 import legacy_rules as LR
    LR._best_effort_rule()
    marked = {"active": {"image": 3}, "best_effort": {"image": {"n": 3}}}
    assert AC.decide(marked, "image", 3)[0] == "keep"
    assert AC.decide({"active": {"image": 3}}, "image", 3)[0] == "clear"                       # 没标记：原规则 2
    assert AC.decide({"active": {"image": 4}, "best_effort": {"image": {"n": 3}}}, "image", 4)[0] == "clear"   # 当前已不是那版
    AC.decide = lambda data, stage, n: ("revert", 2, "退回过契约的 v2")
    LR._best_effort_rule()
    assert AC.decide(marked, "image", 3) == ("revert", 2, "退回过契约的 v2")                   # 更好的版本优先


def test_promote_best_effort_cli_selects_latest_rows_and_passes_adult(tmp_path):
    from ns2 import db as D
    from ns2 import promote_best_effort as PB
    c = D.connect(tmp_path / "q.db")
    for shot, step, st in (("ep05-S1", "first", "best_effort"), ("ep05-S2", "first", "done"), ("ep05-S3", "last", "best_effort")):
        j, _ = D.enqueue(c, run_id="r", ep=5, shot=shot, step=step)
        c.execute("UPDATE jobs SET state=? WHERE id=?", (st, j["id"]))
    D.enqueue(c, run_id="r", ep=5, shot="ep05-S3", step="last", sig={"again": 1})             # S3 最新一行是 queued → 不补选
    rows = PB.best_effort_rows(c, "r", 5)
    assert [(r["shot"], r["step"]) for r in rows] == [("ep05-S1", "first")]
    got = []
    out = PB.promote(rows, "r", call=lambda a, host, t: got.append(a) or {"outcome": "promoted", "promoted": 2},
                     adult_of=lambda run, s: True, triggered_of=lambda s: False)
    assert got[0]["promote_only"] and got[0]["adult"] is True and got[0]["owner_triggered"] is False
    assert out[0]["result"]["promoted"] == 2 and out[0]["job"] == rows[0]["id"]
