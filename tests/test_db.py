from ns2 import db as D


def mk(tmp_path):
    c = D.connect(tmp_path / "q.db")
    c.execute("INSERT INTO runs(run_id) VALUES ('r')")
    return c


def enq(c, shot, step, **k):
    return D.enqueue(c, run_id="r", ep=1, shot=shot, step=step, sig={"x": k.pop("sig", 0)}, **k)[0]


def test_fifo_no_priority(tmp_path):
    c = mk(tmp_path)
    a = enq(c, "ep01-S1", "first"); b = enq(c, "ep01-S2", "first")
    assert D.claim(c, "gpu33", "gpu33:0", ["first"])["id"] == a["id"]
    assert D.claim(c, "gpu33", "gpu33:1", ["first"])["id"] == b["id"]
    assert D.claim(c, "gpu33", "gpu33:3", ["first"]) is None


def test_idempotent_enqueue(tmp_path):
    c = mk(tmp_path)
    j1, new1 = D.enqueue(c, run_id="r", ep=1, shot="s", step="first", sig={"p": 1})
    j2, new2 = D.enqueue(c, run_id="r", ep=1, shot="s", step="first", sig={"p": 1})
    assert new1 and not new2 and j1["id"] == j2["id"]
    assert D.enqueue(c, run_id="r", ep=1, shot="s", step="first", sig={"p": 2})[1]   # 输入变了才新起一行


def test_shot_order_and_latest_row(tmp_path):
    c = mk(tmp_path)
    f = enq(c, "s", "first"); enq(c, "s", "last"); v = enq(c, "s", "video")
    assert D.claim(c, "h", "h:0", ["last", "video"]) is None                       # 首帧没过不领尾帧 / 视频
    D.claim(c, "h", "h:0", ["first"]); D.finish(c, f["id"], "h", "failed")
    f2 = enq(c, "s", "first", sig=1)                                                # 重派新起一行
    D.claim(c, "h", "h:0", ["first"]); D.finish(c, f2["id"], "h", "done")
    l = D.claim(c, "h", "h:0", ["last", "video"]); assert l["step"] == "last"     # 最新首帧行 done → 可领尾帧
    D.finish(c, l["id"], "h", "na")
    assert D.claim(c, "h", "h:0", ["video"])["id"] == v["id"]                      # 尾帧 na 也放行视频


def test_qc_fail_blocks_next_step(tmp_path):
    c = mk(tmp_path)
    f = enq(c, "s", "first", max_tries=2); enq(c, "s", "last")
    for _ in range(2):
        D.claim(c, "h", "h:0", ["first"]); D.finish(c, f["id"], "h", "retry", {"has_output": True})
    assert D.jobs(c, shot="s")[0]["state"] == "best_effort"
    assert D.claim(c, "h", "h:0", ["last"]) is None                                # 尽力版首帧不进下一步（等人工）


def test_retry_without_output_fails(tmp_path):
    c = mk(tmp_path)
    f = enq(c, "s", "video", max_tries=1)
    D.enqueue(c, run_id="r", ep=1, shot="s", step="first", state="done")
    D.enqueue(c, run_id="r", ep=1, shot="s", step="last", state="na")
    D.claim(c, "h", "h:0", ["video"]); assert D.finish(c, f["id"], "h", "retry")["state"] == "failed"


def test_host_filter_pause_owner_trigger(tmp_path):
    c = mk(tmp_path)
    enq(c, "a", "first", want_host="gpu36")
    assert D.claim(c, "gpu33", "gpu33:0", ["first"]) is None
    c.execute("UPDATE runs SET status='paused'")
    assert D.claim(c, "gpu36", "gpu36:0", ["first"]) is None
    c.execute("UPDATE runs SET status='producing'")
    enq(c, "b", "first", owner_trigger=True)
    assert D.claim(c, "gpu36", "gpu36:0", ["first"])["shot"] == "a"
    assert D.claim(c, "gpu36", "gpu36:1", ["first"]) is None                     # 成人镜未触发不可领


def test_reap_and_finish_after_reap_rejected(tmp_path):
    c = mk(tmp_path)
    f = enq(c, "s", "first")
    D.claim(c, "h", "h:0", ["first"])
    assert D.reap(c, lease_s=0, now=1e12) == [f["id"]]
    j = D.jobs(c)[0]; assert (j["state"], j["tries"]) == ("queued", 1)
    assert D.finish(c, f["id"], "h", "done") is None                               # 被回收后旧 worker 上报无效
