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


def test_infra_requeues_without_counting_then_blocks(tmp_path):
    c = mk(tmp_path)
    f = enq(c, "s", "first")
    import time as _t
    for i in range(D.INFRA_MAX - 1):
        D.claim(c, "h", "h:0", ["first"])
        j = D.finish(c, f["id"], "h", "infra", error="gpu33 OOM")
        assert (j["state"], j["tries"]) == ("queued", 0)
        want = min(D.INFRA_BACKOFF_MAX_S, D.INFRA_BACKOFF_S * 2 ** i)                 # M13：退避 30 / 60 / 120 / 240 s…
        assert abs(j["ready_at"] - _t.time() - want) < 5
        assert D.claim(c, "h", "h:0", ["first"]) is None                                # 退避期间不领（不霸占队首）
        c.execute("UPDATE jobs SET ready_at=0")                                         # 快进到退避结束
    D.claim(c, "h", "h:0", ["first"])
    j = D.finish(c, f["id"], "h", "infra", error="gpu33 OOM")
    assert j["state"] == "blocked" and "连续" in j["error"]


def test_soft_prereq_script_board(tmp_path):
    c = mk(tmp_path)
    sc = D.enqueue(c, run_id="r", ep=1, shot="ep01", step="script", want_host="g")[0]
    b = D.enqueue(c, run_id="r", ep=1, shot="ep01-S1", step="board", want_host="g")[0]
    f = D.enqueue(c, run_id="r", ep=1, shot="ep01-S1", step="first", want_host="g")[0]
    D.enqueue(c, run_id="r", ep=1, shot="ep01-S2", step="first", want_host="g")          # 没有 board 行（迁移镜）→ 不挡
    assert D.claim(c, "g", "g:0", ["board", "first"])["shot"] == "ep01-S2"
    assert D.claim(c, "g", "g:1", ["board", "first"]) is None                             # 剧本没导入 → board 不领；board 没过 → first 不领
    D.claim(c, "g", "g:2", ["script"]); D.finish(c, sc["id"], "g", "done")      # g:0 还占着（每道并发 1）
    assert D.claim(c, "g", "g:1", ["board", "first"])["id"] == b["id"]
    D.finish(c, b["id"], "g", "done")
    assert D.claim(c, "g", "g:1", ["first"])["id"] == f["id"]


def test_shot_rows_partition():
    from ns2.step_script import shot_rows
    rows = shot_rows("r", 5, [1, 2], "abc")
    assert [(r["shot"], r["step"], r["want_host"]) for r in rows[:4]] == [
        ("ep05-S1", "board", "gpu33"), ("ep05-S1", "first", "gpu33"), ("ep05-S1", "last", "gpu33"), ("ep05-S1", "video", "gpu33")]
    assert rows[3]["max_tries"] == 4 and len(rows) == 8


def test_lane_guard_one_running_per_lane(tmp_path):
    """B3d / M1：同一道上有在跑的（含没回收的旧活）就不领新活，库强制，不靠约定"""
    c = mk(tmp_path)
    a = enq(c, "ep01-S1", "first"); enq(c, "ep01-S2", "first")
    assert D.claim(c, "g", "g:0", ["first"])["id"] == a["id"]
    assert D.claim(c, "g", "g:0", ["first"]) is None                     # 同道第二个线程 / 进程
    assert D.claim(c, "g", "g:1", ["first"])["shot"] == "ep01-S2"        # 别的道照领
    D.finish(c, a["id"], "g", "done")
    enq(c, "ep01-S3", "first")
    assert D.claim(c, "g", "g:0", ["first"])["shot"] == "ep01-S3"


def test_m3_predicates_without_api_lock(tmp_path):
    """M3：不靠 API 锁也对——两个连接（≈两个进程）并发同键入队不 500；被回收后旧上报作废；回收不动刚心跳的"""
    c1 = mk(tmp_path)
    c2 = D.connect(tmp_path / "q.db")
    a, n1 = D.enqueue(c1, run_id="r", ep=1, shot="s", step="first", sig={"x": 1})
    b, n2 = D.enqueue(c2, run_id="r", ep=1, shot="s", step="first", sig={"x": 1})
    assert n1 and not n2 and a["id"] == b["id"]
    D.claim(c1, "h", "h:0", ["first"])
    assert D.reap(c2, lease_s=0, now=1e12) == [a["id"]]                  # 回收
    assert D.finish(c1, a["id"], "h", "done") is None                    # 旧 worker 的上报作废
    D.claim(c1, "h2", "h2:0", ["first"])
    D.heartbeat(c1, a["id"], "h2")
    assert D.reap(c2, lease_s=3600) == []                                # 刚心跳的不动
    assert D.finish(c2, a["id"], "h", "done") is None                    # 不是本机的活
    assert D.finish(c2, a["id"], "h2", "done")["state"] == "done"


def test_long_shot_rerouted_to_lane(tmp_path):
    """M6：长镜头领错道 → 回队并锁定 gpu36:2，只有那条道领得到（不 blocked 堵死）"""
    c = mk(tmp_path)
    v = enq(c, "s", "video", want_host="gpu36")
    D.enqueue(c, run_id="r", ep=1, shot="s", step="first", state="done"); D.enqueue(c, run_id="r", ep=1, shot="s", step="last", state="na")
    assert D.claim(c, "gpu36", "gpu36:0", ["video"])["id"] == v["id"]
    j = D.finish(c, v["id"], "gpu36", "infra", {"want_lane": "gpu36:2"}, "长镜头只派 gpu36:2")
    assert (j["state"], j["tries"], j["want_lane"]) == ("queued", 0, "gpu36:2")
    c.execute("UPDATE jobs SET ready_at=0")                                             # 快进退避
    assert D.claim(c, "gpu36", "gpu36:1", ["video"]) is None
    assert D.claim(c, "gpu36", "gpu36:2", ["video"])["id"] == v["id"]


def test_manual_requeue_clears_backoff(tmp_path):
    c = mk(tmp_path)
    f = enq(c, "s", "first", max_tries=1)
    D.claim(c, "h", "h:0", ["first"]); D.finish(c, f["id"], "h", "infra")
    c.execute("UPDATE jobs SET state='failed'")                                         # 人工判死后重派
    assert D.requeue(c, f["id"], "x")["ready_at"] is None
    assert D.claim(c, "h", "h:0", ["first"])["id"] == f["id"]
