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
    # 项目方 11-09“尽力版一律自动用”（取代原“尽力版首帧不进下一步、等人工”）：帧步最后一次已把最好一版设当前 → 尾帧照常领
    assert D.claim(c, "h", "h:0", ["last"])["step"] == "last"


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
    assert (rows[0]["shot"], rows[0]["step"], rows[0]["want_host"]) == ("ep05", "assets", "gpu33")      # P6 R1a
    assert [(r["shot"], r["step"], r["want_host"]) for r in rows[1:5]] == [
        ("ep05-S1", "board", "gpu33"), ("ep05-S1", "first", "gpu33"), ("ep05-S1", "last", "gpu33"), ("ep05-S1", "video", "gpu33")]
    assert rows[4]["max_tries"] == 4 and len(rows) == 9


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
    assert D.claim(c1, "h2", "h2:0", ["first"]) is None                  # 审查 3 B3：回收后退避期内不许再领（旧 prompt 可能还在跑）
    c1.execute("UPDATE jobs SET ready_at=NULL WHERE id=?", (a["id"],))     # 退避期过了
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


def test_enqueue_episode_one_script_row_per_host(tmp_path, monkeypatch):
    from ns2 import enqueue_episode as EE
    md = tmp_path / "P4_ep05.md"; md.write_text("# 第5集", encoding="utf-8")
    sent = []
    monkeypatch.setattr(EE, "post", lambda api, path, body: (sent.append(body), {"job": {"id": len(sent)}, "created": True})[1])
    EE.main(["--api", "x", "--run", "r", "--ep", "5", "--md", str(md)])
    hosts = sorted(b["want_host"] for b in sent)
    assert hosts == ["gpu33", "gpu36"] and len({D.idem_key("r", b["shot"], "script", b["sig"]) for b in sent}) == 2
    assert [b["payload"]["enqueue_shots"] for b in sent if b["want_host"] == "gpu33"] == [True]     # ep05 出图机 gpu33 负责入队镜头


def test_p6_assets_between_script_and_board(tmp_path):
    """P6 R1a：board 等本集本机 assets 做完；assets 等 script；assets done → 本集 blocked 的 board 自动回队"""
    c = D.connect(tmp_path / "q.db")
    c.execute("INSERT INTO runs(run_id) VALUES ('r')")
    D.enqueue(c, run_id="r", ep=5, shot="ep05", step="script", want_host="gpu33")
    a, _ = D.enqueue(c, run_id="r", ep=5, shot="ep05", step="assets", want_host="gpu33")
    b, _ = D.enqueue(c, run_id="r", ep=5, shot="ep05-S1", step="board", want_host="gpu33")
    assert D.claim(c, "gpu33", "gpu33:0", ["assets", "board"]) is None          # 剧本没导入：都不领
    s = D.claim(c, "gpu33", "gpu33:0", ["script"])
    D.finish(c, s["id"], "gpu33", "done")
    got = D.claim(c, "gpu33", "gpu33:0", ["board", "assets"])
    assert got["step"] == "assets"                                              # board 还在等资产
    assert D.claim(c, "gpu33", "gpu33:1", ["board"]) is None
    D.finish(c, got["id"], "gpu33", "done")
    bb = D.claim(c, "gpu33", "gpu33:0", ["board"])
    assert bb["id"] == b["id"]
    D.finish(c, bb["id"], "gpu33", "blocked", error="预检不过：缺设定图")
    a2, created = D.enqueue(c, run_id="r", ep=5, shot="ep05", step="assets", want_host="gpu33", key="assets-again")
    got = D.claim(c, "gpu33", "gpu33:0", ["assets"])
    D.finish(c, got["id"], "gpu33", "done")
    row = c.execute("SELECT state FROM jobs WHERE id=?", (b["id"],)).fetchone()
    assert row["state"] == "queued"                                             # 资产补齐 → 卡住的分镜回队


def test_p6_shot_rows_have_assets_row():
    from ns2.step_script import shot_rows
    rows = shot_rows("r", 5, [1, 2], "sha")
    assert [r["step"] for r in rows if r["shot"] == "ep05"] == ["assets"]
    assert sum(r["step"] == "board" for r in rows) == 2


def test_p6_board_waits_only_for_its_own_assets(tmp_path):
    """P6 R1a：资产一个一行、各道并行；镜只等覆盖它的资产；设定图等身份脸"""
    c = D.connect(tmp_path / "q.db")
    c.execute("INSERT INTO runs(run_id) VALUES ('r')")
    D.enqueue(c, run_id="r", ep=5, shot="ep05", step="assets", want_host="g", state="done")
    face, _ = D.enqueue(c, run_id="r", ep=5, shot="ep05", step="asset", want_host="g", sig={"a": "face-x"},
                        payload={"asset": "face-x", "shots": ["ep05-S1"], "after": []})
    sheet, _ = D.enqueue(c, run_id="r", ep=5, shot="ep05", step="asset", want_host="g", sig={"a": "sheet-x-B"},
                         payload={"asset": "sheet-x-B", "shots": ["ep05-S1", "ep05-S2"], "after": ["face-x"]})
    b1, _ = D.enqueue(c, run_id="r", ep=5, shot="ep05-S1", step="board", want_host="g")
    b3, _ = D.enqueue(c, run_id="r", ep=5, shot="ep05-S3", step="board", want_host="g")
    assert D.claim(c, "g", "g:0", ["board"])["id"] == b3["id"]          # S3 不缺资产：马上开工
    got = D.claim(c, "g", "g:1", ["asset"])
    assert got["id"] == face["id"]                                       # 设定图在等身份脸
    assert D.claim(c, "g", "g:2", ["asset"]) is None
    D.finish(c, face["id"], "g", "done")
    assert D.claim(c, "g", "g:2", ["asset"])["id"] == sheet["id"]
    assert D.claim(c, "g", "g:3", ["board"]) is None                     # S1 还在等设定图
    D.finish(c, sheet["id"], "g", "blocked", error="两张都不过")
    assert D.claim(c, "g", "g:3", ["board"]) is None                     # 资产卡住：S1 照样等（不带着缺资产出图）


def test_p6_asset_without_shots_blocks_nobody(tmp_path):
    """11-09：没写镜号的资产行（单品裁图）卡住也不挡任何镜"""
    c = D.connect(tmp_path / "q.db")
    c.execute("INSERT INTO runs(run_id) VALUES ('r')")
    D.enqueue(c, run_id="r", ep=5, shot="ep05", step="assets", want_host="g", state="done")
    D.enqueue(c, run_id="r", ep=5, shot="ep05", step="asset", want_host="g", sig={"a": "item"}, state="blocked",
              payload={"asset": "item-x", "shots": [], "after": []})
    b, _ = D.enqueue(c, run_id="r", ep=5, shot="ep05-S1", step="board", want_host="g")
    assert D.claim(c, "g", "g:0", ["board"])["id"] == b["id"]



def test_b3_reap_marks_comfy_prompt_for_cancel(tmp_path):
    """审查 3 B3：提交后登记 prompt；回收 → 标要撤，本机整机心跳取走一次；撤完报 done；别的机器取不到"""
    c = mk(tmp_path)
    a, _ = D.enqueue(c, run_id="r", ep=1, shot="s", step="first")
    D.claim(c, "h", "h:0", ["first"])
    assert D.comfy_record(c, a["id"], "other", "other:0", "p0") is False     # 不是本机正在跑的活 → 不收
    assert D.comfy_record(c, a["id"], "h", "h:0", "p1") is True
    assert D.comfy_cancels(c, "h") == []                                     # 没回收不撤
    assert D.reap(c, lease_s=0, now=1e12) == [a["id"]]
    assert D.comfy_cancels(c, "other") == []
    assert D.comfy_cancels(c, "h") == [{"pid": "p1", "job_id": a["id"], "lane": "h:0"}]
    assert D.comfy_cancels(c, "h") == []                                     # 只发一次
    D.comfy_done(c, "p1", "h")
    assert c.execute("SELECT state FROM comfy_prompts WHERE pid='p1'").fetchone()[0] == "done"
    assert c.execute("SELECT ready_at FROM jobs WHERE id=?", (a["id"],)).fetchone()[0] >= 1e12 + D.REAP_BACKOFF_S


def test_b3_worker_cancels_leftover_and_reaped_prompts(tmp_path, monkeypatch):
    """worker 死了重启：pending/comfy 里剩的 prompt 全撤；心跳回包要撤的也撤，撤完删本机文件"""
    from ns2 import worker as W
    monkeypatch.setattr(W, "PENDING", tmp_path / "pending")
    monkeypatch.delenv("NS2_API", raising=False)
    W.comfy_mark({"id": 7}, "gpu36:1", "pidA", live=True)
    W.comfy_mark({"id": 8}, "gpu36:2", "pidB", live=True)
    W.comfy_mark({"id": 8}, "gpu36:2", "pidB", live=False)                   # 正常结束的不留
    got = []
    out = W.cancel_leftover_prompts(cancel=lambda lane, pid: got.append((lane, pid)) or "deleted,interrupted")
    assert got == [("gpu36:1", "pidA")] and out == [("pidA", "deleted,interrupted")]
    assert not list((tmp_path / "pending/comfy").glob("*.json"))
    got.clear()
    W.cancel_prompts([{"pid": "pidC", "lane": "gpu36:0", "job_id": 9}], cancel=lambda lane, pid: got.append(pid) or "deleted")
    assert got == ["pidC"]


def test_b3_wait_cancels_on_error_branches(monkeypatch):
    """wait() 的 comfy_down / comfy_error / no_output 分支都撤自己的 prompt（原来只有 timeout / http 撤）"""
    import json
    from ns2 import step_video as SV
    calls = []
    monkeypatch.setattr(SV, "cancel", lambda lane, pid: calls.append(pid) or "deleted")
    monkeypatch.setattr(SV.time, "sleep", lambda s: None)
    hist = {"err": {"p": {"status": {"status_str": "error", "messages": [["execution_error", {}]]}}},
            "noout": {"p": {"status": {"completed": True, "messages": []}, "outputs": {}}}}
    for k in ("err", "noout"):
        monkeypatch.setattr(SV, "api", lambda port, path, *a, _k=k, **kw: json.dumps(hist[_k]).encode())
        r = SV.wait("gpu33:0", "p", timeout_s=60, poll_s=0)
        assert r["infra"] and r["cancel"] == "deleted"
    def down(*a, **k):
        raise OSError("refused")
    monkeypatch.setattr(SV, "api", down)
    r = SV.wait("gpu33:0", "p", timeout_s=60, poll_s=0, grace_s=0)
    assert r["error"].startswith("comfy_down") and r["cancel"] == "deleted"
    assert calls == ["p", "p", "p"]


def test_export_reviews_roundtrip_and_keep(tmp_path):
    """审查 3 §9.5（项目方 11-09 定每日导出）：导出 reviews + triggers；只留最近 N 份；恢复只补缺、不覆盖"""
    import json
    from ns2 import export_reviews as X
    c = D.connect(tmp_path / "q.db")
    D.set_review(c, "r", "ep01-S1", "passed")
    D.set_review(c, "r", "ep01-S2", "rejected", "手")
    D.trigger(c, "r", "ep03-S4")
    for i in range(3):
        f = X.export(str(tmp_path / "q.db"), str(tmp_path / "bk"), keep=2, now=f"2026-11-1{i}T05:10:00")
    files = sorted((tmp_path / "bk").glob("reviews_*.json"))
    assert len(files) == 2 and files[-1] == f
    doc = json.loads(f.read_text(encoding="utf-8"))
    assert {r["shot"]: r["state"] for r in doc["reviews"]} == {"ep01-S1": "passed", "ep01-S2": "rejected"} and doc["triggers"][0]["shot"] == "ep03-S4"
    c2 = D.connect(tmp_path / "new.db")
    D.set_review(c2, "r", "ep01-S1", "unreviewed")
    assert X.restore(str(tmp_path / "new.db"), str(f)) == {"reviews": 1, "triggers": 1}
    assert D.reviews(D.connect(tmp_path / "new.db"), "r")["ep01-S1"]["state"] == "unreviewed"      # 不覆盖已有
