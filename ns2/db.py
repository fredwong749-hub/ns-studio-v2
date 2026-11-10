"""队列表（PLAN §2.3 + P5 修正）：派单的唯一事实来源。只有 API 进程写它（单写者），worker 经 HTTP 认领 / 心跳 / 上报。

语义：
- FIFO（created_at, id），无 priority 字段；唯一让路：同档内准备步（script / assets / asset）先领（R4 11-11，见 PREP_STEPS）。
- 同镜顺序：first 未 done/na 不领 last；first、last 未 done/na 不领 video（P5 修正 ④：QC 没过的帧不进视频）。
- 幂等：idem_key UNIQUE，同输入重复入队返回已有行。
- 认领原子：BEGIN IMMEDIATE + 条件 UPDATE。
- 失败分级：retry（QC 没过 / infra）→ tries<max 回 queued，否则 best_effort（有产物）或 failed（无产物）；blocked 不计 tries。
- 心跳超时（LEASE_S）回收为 queued、tries+1。
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

STEPS = ("script", "assets", "asset", "board", "first", "last", "video", "film")
STATES = ("queued", "running", "done", "failed", "blocked", "best_effort", "na")
PREREQ = {"last": ("first",), "video": ("first", "last")}
# 有就必须过、没有不挡（迁移来的镜没有 script / board 行）：first ← 本镜 board；board ← 本集本机 script（shot = epNN）
SOFT_PREREQ = {"first": ("board",), "board": ("assets", "script"), "assets": ("script",)}   # P6 R1a：剧本 → 资产 → 分镜（有行才挡）
EP_LEVEL = ("script", "assets")      # 一集一行（shot = epNN）
PASSED = ("done", "na")
FRAME_PASSED = ("done", "na", "best_effort")    # 项目方 11-09：尽力版一律自动用 → 首 / 尾帧尽力版照样放行尾帧 / 视频
LEASE_S = 45 * 60
INFRA_MAX = 5
INFRA_BACKOFF_S, INFRA_BACKOFF_MAX_S = 30, 600

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL, ep INTEGER NOT NULL, shot TEXT NOT NULL,
  step TEXT NOT NULL, stage TEXT,
  state TEXT NOT NULL DEFAULT 'queued',
  payload TEXT NOT NULL DEFAULT '{}', result TEXT,
  want_host TEXT, lane TEXT, host TEXT,
  tries INTEGER NOT NULL DEFAULT 0, max_tries INTEGER NOT NULL DEFAULT 3,
  idem_key TEXT UNIQUE, owner_trigger INTEGER NOT NULL DEFAULT 0, want_lane TEXT, ready_at REAL,
  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, beat_at REAL,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(state, created_at, id);
CREATE INDEX IF NOT EXISTS idx_jobs_shot ON jobs(run_id, shot, step);
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'producing');
CREATE TABLE IF NOT EXISTS triggers (run_id TEXT NOT NULL, shot TEXT NOT NULL, at TEXT NOT NULL, by TEXT, PRIMARY KEY (run_id, shot));
CREATE TABLE IF NOT EXISTS reviews (run_id TEXT NOT NULL, shot TEXT NOT NULL, state TEXT NOT NULL, reason TEXT, at TEXT NOT NULL,
  PRIMARY KEY (run_id, shot));
CREATE TABLE IF NOT EXISTS comfy_prompts (pid TEXT PRIMARY KEY, job_id INTEGER NOT NULL, host TEXT NOT NULL, lane TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'live', at TEXT NOT NULL, done_at TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, job_id INTEGER, kind TEXT NOT NULL, detail TEXT);
"""
STAGE_OF = {"first": "image", "last": "last", "video": "video"}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    c.executescript(SCHEMA)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
    for col, typ in (("want_lane", "TEXT"), ("ready_at", "REAL")):      # 旧库补列（CREATE IF NOT EXISTS 不加列）
        if col not in cols:
            c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")
    return c


PREREQ_BLOCK = "前置不过："


@contextmanager
def tx(c):
    """审查 3 M3：多语句写一个事务（连接是 autocommit）。已在事务里 → 直接并入外层"""
    if c.in_transaction:
        yield c
        return
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
    except BaseException:
        c.execute("ROLLBACK")
        raise
    c.execute("COMMIT")


def idem_key(run_id: str, shot: str, step: str, sig: dict | None) -> str:
    s = json.dumps([run_id, shot, step, sig or {}], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in ("payload", "result"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


def _event(c, job_id, kind, detail=None):
    c.execute("INSERT INTO events(at, job_id, kind, detail) VALUES (?,?,?,?)",
              (now_iso(), job_id, kind, json.dumps(detail, ensure_ascii=False) if detail is not None else None))


ADULT_STEPS = ("first", "last", "video")


LONG_LANE, LONG_FRAMES = "gpu36:2", 260          # 与 step_video 同值（那边 import 这里会循环，测试里断言两边一致）


def long_only(payload: dict) -> bool:
    vs = payload.get("variants") or {}
    ready = [v for k, v in vs.items() if (v or {}).get("status") == "ready" and k in (payload.get("ladder") or list(vs))]
    return bool(ready) and all(int((v or {}).get("frames") or 0) > LONG_FRAMES for v in ready)


def triggered(c, run_id: str, shot: str) -> bool:
    return c.execute("SELECT 1 FROM triggers WHERE run_id=? AND shot=?", (run_id, shot)).fetchone() is not None


def enqueue(c, *, run_id: str, ep: int, shot: str, step: str, payload: dict | None = None, want_host: str | None = None,
            sig: dict | None = None, state: str = "queued", result: dict | None = None, owner_trigger: bool = False,
            max_tries: int = 3, key: str | None = None, error: str | None = None, adult: bool = False) -> tuple[dict, bool]:
    """→ (job, created)。同 idem_key 已有 → 原行、created=False。
    B2：adult = 服务端按剧本判的成人镜（调用方的 owner_trigger 只能加严不能放宽）；成人镜的出图 / 出片行在项目方触发前一律 owner_trigger=1，
    触发过的镜 payload 带 owner_triggered（worker 侧第二道检查用）"""
    if step not in STEPS or state not in STATES:
        raise ValueError(f"step/state 不认识：{step}/{state}")
    if adult and step in ADULT_STEPS:
        if triggered(c, run_id, shot):
            payload = {**(payload or {}), "owner_triggered": True}
        else:
            owner_trigger = True
    k = key or idem_key(run_id, shot, step, sig)
    cur = c.execute(                      # M3：INSERT OR IGNORE + 重查，并发同键不 500
        "INSERT OR IGNORE INTO jobs(run_id, ep, shot, step, stage, state, payload, result, want_host, max_tries, idem_key, owner_trigger, created_at, finished_at, error)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, ep, shot, step, STAGE_OF.get(step), state, json.dumps(payload or {}, ensure_ascii=False),
         json.dumps(result, ensure_ascii=False) if result is not None else None, want_host, max_tries, k,
         1 if owner_trigger else 0, now_iso(), now_iso() if state in ("done", "na", "best_effort") else None, error))
    if cur.rowcount == 0:
        return row(c.execute("SELECT * FROM jobs WHERE idem_key=?", (k,)).fetchone()), False
    _event(c, cur.lastrowid, "enqueue", {"state": state})
    if step == "video" and long_only(payload or {}):        # 审查 3 M6：可派变体全是超长 → 入队就锁长道（不再被别的道白领一次）
        c.execute("UPDATE jobs SET want_lane=? WHERE id=?", (LONG_LANE, cur.lastrowid))
    if step == "film" and state == "queued":     # 新一版整集输入 → 本集更早的排队 film 行作废（不删，记 na + 原因）
        for r in c.execute("SELECT id FROM jobs WHERE run_id=? AND shot=? AND step='film' AND state='queued' AND id<?",
                           (run_id, shot, cur.lastrowid)).fetchall():
            if c.execute("UPDATE jobs SET state='na', error='被新一版整集输入取代', finished_at=? WHERE id=? AND state='queued'",
                         (now_iso(), r["id"])).rowcount == 1:          # 审查 3 M3：带状态谓词（检查与写之间被领走的不动）
                _event(c, r["id"], "superseded", {"by": cur.lastrowid})
    return row(c.execute("SELECT * FROM jobs WHERE id=?", (cur.lastrowid,)).fetchone()), True


FILM_SQL = (" AND j.id = (SELECT MAX(f.id) FROM jobs f WHERE f.run_id=j.run_id AND f.shot=j.shot AND f.step='film')"
            " AND EXISTS (SELECT 1 FROM jobs v WHERE v.run_id=j.run_id AND v.ep=j.ep AND v.step='video')"
            " AND NOT EXISTS (SELECT 1 FROM jobs v WHERE v.run_id=j.run_id AND v.ep=j.ep AND v.step='video'"
            "   AND v.id = (SELECT MAX(x.id) FROM jobs x WHERE x.run_id=v.run_id AND x.shot=v.shot AND x.step='video')"
            "   AND (v.state='running' OR (v.state='queued' AND v.owner_trigger=0"
            # 排队的视频只有在前置还“有望”时才算在途；首 / 尾帧最新行已是 failed / blocked / best_effort（等人）→ 不挡整集合成（占位）
            "     AND NOT EXISTS (SELECT 1 FROM jobs f WHERE f.run_id=v.run_id AND f.shot=v.shot AND f.step IN ('first','last')"
            "       AND f.id = (SELECT MAX(y.id) FROM jobs y WHERE y.run_id=f.run_id AND y.shot=f.shot AND y.step=f.step)"
            "       AND f.state IN ('failed','blocked','best_effort')))))")


def _claim_sql(step: str, pinned: bool = False) -> str:
    """前置步 = 该镜该步“最新一行”（id 最大，重派会新起一行）必须 done/na；没入队 = 不领。
    film（shot = epNN，整集一行）：本集所有镜的最新视频行都不在 queued/running（尽力 / 阻断的镜用占位，L-204 D8 不挡整集），
    且只领本集最新的 film 行（视频每定一镜就新起一行 film，旧行被取代）"""
    if step == "film":
        return ("SELECT j.* FROM jobs j JOIN runs r ON r.run_id = j.run_id"
                " WHERE j.state='queued' AND j.owner_trigger=0 AND r.status='producing'"
                " AND (j.want_host IS NULL OR j.want_host=?) AND (j.want_lane IS NULL OR j.want_lane=?) AND j.step=?"
                " AND (j.ready_at IS NULL OR j.ready_at <= CAST(strftime('%s','now') AS REAL))" + FILM_SQL +
                " ORDER BY j.created_at, j.id LIMIT 1").replace("(j.want_host IS NULL OR j.want_host=?)", "j.want_host=?" if pinned else "(j.want_host IS NULL OR j.want_host=?)")
    pre = "".join(
        f" AND (SELECT p.state FROM jobs p WHERE p.run_id=j.run_id AND p.shot=j.shot AND p.step='{p}'"
        f" ORDER BY p.id DESC LIMIT 1) IN ('done','na','best_effort')" for p in PREREQ.get(step, ()))   # FRAME_PASSED
    for soft in SOFT_PREREQ.get(step, ()):
        pre += _soft_sql(soft)
    if step == "board":          # P6 R1a：本镜只等覆盖它的资产行（payload.shots 含本镜或 "*"）做完；不缺资产的镜不等整集
        pre += (" AND NOT EXISTS (SELECT 1 FROM jobs a, json_each(json_extract(a.payload, '$.shots')) s"
                " WHERE a.run_id=j.run_id AND a.ep=j.ep AND a.step='asset' AND a.state NOT IN ('done','na') AND s.value IN (j.shot, '*'))")
    elif step == "asset":        # 设定图等身份脸：payload.after 里的资产行做完才领
        pre += (" AND NOT EXISTS (SELECT 1 FROM jobs a, json_each(json_extract(j.payload, '$.after')) x"
                " WHERE a.run_id=j.run_id AND a.step='asset' AND json_extract(a.payload, '$.asset')=x.value AND a.state NOT IN ('done','na'))")
    return ("SELECT j.* FROM jobs j JOIN runs r ON r.run_id = j.run_id"
            " WHERE j.state='queued' AND j.owner_trigger=0 AND r.status='producing'"
            " AND (j.want_host IS NULL OR j.want_host=?) AND (j.want_lane IS NULL OR j.want_lane=?) AND j.step=?"
            " AND (j.ready_at IS NULL OR j.ready_at <= CAST(strftime('%s','now') AS REAL))" + pre +
            " ORDER BY j.created_at, j.id LIMIT 1").replace("(j.want_host IS NULL OR j.want_host=?)", "j.want_host=?" if pinned else "(j.want_host IS NULL OR j.want_host=?)")


def _soft_sql(soft: str) -> str:
    pre = ""
    if soft == "board":
        pre += (" AND COALESCE((SELECT p.state FROM jobs p WHERE p.run_id=j.run_id AND p.shot=j.shot AND p.step='board'"
                " ORDER BY p.id DESC LIMIT 1), 'done') IN ('done','na')")
    elif soft in EP_LEVEL:       # 本集剧本 / 资产行（shot = epNN）在本机（want_host 相同）做过
        pre += (f" AND COALESCE((SELECT p.state FROM jobs p WHERE p.run_id=j.run_id AND p.ep=j.ep AND p.step='{soft}'"
                " AND COALESCE(p.want_host,'')=COALESCE(j.want_host,'') ORDER BY p.id DESC LIMIT 1), 'done') IN ('done','na')")
    return pre


# R4（11-11 ep07 验收）：准备步插队——资产行是剧本导入后才建的，按 FIFO 排在本集所有首 / 尾帧后面，
# 用到新场景底图的镜（ep07 S11–S16）要等前面的镜全出完才轮到资产，整集尾巴被拖长。同一台、同一“专属 / 通用”档内，准备步先领。
PREP_STEPS = frozenset({"script", "assets", "asset"})


def claim(c, host: str, lane: str, steps: list[str]) -> dict | None:
    """原子认领一条：本机、前置步已过；先领“只能本机做”的（want_host = 本机），再领“哪台都行”的（want_host 空），同类内 FIFO。
    P6 11-09：视频行改成两台都能领后，出图机的道被视频占着、只能它出的尾帧排在后面（ep05 S10 / S11 尾帧等 gpu33 出完两条视频）"""
    steps = [s for s in steps if s in STEPS]
    if not steps:
        return None
    c.execute("BEGIN IMMEDIATE")
    try:
        if c.execute("SELECT 1 FROM jobs WHERE state='running' AND lane=?", (lane,)).fetchone():
            c.execute("COMMIT")          # B3d / M1：每道并发 1 由库强制——这道上还有在跑的（含没回收的旧活）就不领
            return None
        cands = [r for s in steps if (r := c.execute(_claim_sql(s), (host, lane, s)).fetchone()) is not None]
        if any(r["want_host"] == host for r in cands):       # 每步 SQL 只回最早一条：有本机专属的步，再按“专属优先”重取一遍
            cands += [r for s in steps if (r := c.execute(_claim_sql(s, pinned=True), (host, lane, s)).fetchone()) is not None]
        for r in sorted(cands, key=lambda r: (r["want_host"] != host, r["step"] not in PREP_STEPS, r["created_at"], r["id"]))[:1]:
            cur = c.execute("UPDATE jobs SET state='running', host=?, lane=?, started_at=?, beat_at=?, error=NULL WHERE id=? AND state='queued'",
                            (host, lane, now_iso(), time.time(), r["id"]))
            if cur.rowcount != 1:
                c.execute("COMMIT")
                return None
            _event(c, r["id"], "claim", {"host": host, "lane": lane})
            c.execute("COMMIT")
            return row(c.execute("SELECT * FROM jobs WHERE id=?", (r["id"],)).fetchone())
        c.execute("COMMIT")
        return None
    except Exception:
        c.execute("ROLLBACK")
        raise


def heartbeat(c, job_id: int, host: str) -> bool:
    cur = c.execute("UPDATE jobs SET beat_at=? WHERE id=? AND state='running' AND host=?", (time.time(), job_id, host))
    return cur.rowcount == 1


def finish(c, job_id: int, host: str, state: str, result: dict | None = None, error: str | None = None) -> dict | None:
    """审查 3 M3：主 UPDATE 与事件 / 成人挡 / 资产后重派 / 成片入队同一事务——中途崩全回滚，worker 重报（finish_durable）"""
    with tx(c):
        return _finish(c, job_id, host, state, result, error)


def _finish(c, job_id: int, host: str, state: str, result: dict | None = None, error: str | None = None) -> dict | None:
    """state ∈ done|failed|blocked|best_effort|na|retry|infra。retry：tries+1，没用完回 queued；用完 → 有产物 best_effort / 无产物 failed。
    infra（打分 / 连接 / 执行器出错，不是内容不过）：回 queued 不计次；连续 INFRA_MAX 次 → blocked（人工看）"""
    j = row(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    if j is None or j["state"] != "running" or j["host"] != host:
        return None
    tries = j["tries"]
    if state == "infra":
        n = 1
        for ev in c.execute("SELECT kind, detail FROM events WHERE job_id=? AND kind IN ('finish','requeue') ORDER BY id DESC", (job_id,)):
            if ev["kind"] == "requeue" or json.loads(ev["detail"] or "{}").get("asked") != "infra":
                break
            n += 1
        new = "blocked" if n >= INFRA_MAX else "queued"
        backoff = min(INFRA_BACKOFF_MAX_S, INFRA_BACKOFF_S * 2 ** (n - 1))      # M13：infra 回队退避，不连续霸占队首
        if new == "blocked":
            error = f"连续 {n} 次 infra 出错：{error}"
    elif state == "retry":
        backoff = 0
        tries += 1
        if tries < j["max_tries"]:
            new = "queued"
        else:
            new = "best_effort" if (result or {}).get("has_output") else "failed"
    else:
        assert state in STATES and state not in ("queued", "running"), state
        new = state
        backoff = 0
    prev = json.dumps(j["result"], ensure_ascii=False) if j.get("result") is not None else None
    cur = c.execute("UPDATE jobs SET state=?, tries=?, result=?, error=?, finished_at=?, host=CASE WHEN ?='queued' THEN NULL ELSE host END,"
                    " lane=CASE WHEN ?='queued' THEN NULL ELSE lane END, want_lane=COALESCE(?, want_lane), ready_at=? WHERE id=? AND state='running' AND host=?",
                    (new, tries, json.dumps(result, ensure_ascii=False) if result is not None else prev,
                     error, None if new == "queued" else now_iso(), new, new,
                     (result or {}).get("want_lane") if new == "queued" else None,                  # M6：步函数要求改派到指定道
                     time.time() + backoff if new == "queued" and backoff else None, job_id, host))
    if cur.rowcount != 1:                 # M3：检查与写之间被回收 / 重派 → 本次上报作废（API 回 409）
        return None
    r_ = result or {}
    _event(c, job_id, "finish", {"asked": state, "state": new, "tries": tries, "error": (error or "")[:300],
                                 "secs": r_.get("secs"), "gpu_s": r_.get("gpu_s"), "lane": j.get("lane"), "started_at": j.get("started_at")})
    if j["step"] == "board" and new == "done" and (result or {}).get("adult") and not triggered(c, j["run_id"], j["shot"]):    # L-128
        c.execute("UPDATE jobs SET owner_trigger=1 WHERE run_id=? AND shot=? AND step IN ('first','last','video') AND state='queued'",
                  (j["run_id"], j["shot"]))
        _event(c, job_id, "adult_hold", {"shot": j["shot"]})
    if j["step"] in ("first", "last") and new in ("failed", "blocked"):     # 审查 3 M6：前置帧失败 / 挡住 → 本镜排队的视频行改 blocked 写原因
        for v in c.execute("SELECT id FROM jobs WHERE run_id=? AND shot=? AND step='video' AND state='queued'", (j["run_id"], j["shot"])).fetchall():
            if c.execute("UPDATE jobs SET state='blocked', error=?, finished_at=? WHERE id=? AND state='queued'",
                         (f"{PREREQ_BLOCK}{j['step']} {new}：{(error or '')[:300]}", now_iso(), v["id"])).rowcount == 1:
                _event(c, v["id"], "prereq_blocked", {"by": job_id})
    if j["step"] in ("first", "last") and new in ("done", "na", "best_effort"):   # 前置恢复 → 因它挡住的视频行放回队列
        for v in c.execute("SELECT id FROM jobs WHERE run_id=? AND shot=? AND step='video' AND state='blocked' AND error LIKE ?",
                           (j["run_id"], j["shot"], f"{PREREQ_BLOCK}{j['step']} %")).fetchall():
            c.execute("UPDATE jobs SET state='queued', error=NULL, finished_at=NULL WHERE id=? AND state='blocked'", (v["id"],))
            _event(c, v["id"], "requeue", {"by": "prereq_recovered", "prereq_job": job_id})
    if j["step"] == "assets" and new == "done":         # P6 R1a：资产补齐 → 本集因预检卡住的分镜行放回队列（立即可领）
        for b in c.execute("SELECT id FROM jobs WHERE run_id=? AND ep=? AND step='board' AND state='blocked'", (j["run_id"], j["ep"])).fetchall():
            c.execute("UPDATE jobs SET state='queued', tries=0, host=NULL, lane=NULL, error=NULL, finished_at=NULL, result=NULL, beat_at=NULL,"
                      " ready_at=NULL WHERE id=? AND state='blocked'", (b["id"],))
            _event(c, b["id"], "requeue", {"by": "assets_done", "assets_job": job_id})
    if j["step"] in ("first", "last") and new in ("failed", "blocked", "best_effort") and not (j.get("payload") or {}).get("echo")             and c.execute("SELECT 1 FROM jobs WHERE run_id=? AND ep=? AND step='video' AND state IN ('done','best_effort')", (j["run_id"], j["ep"])).fetchone():
        enqueue_film(c, j["run_id"], j["ep"], want_host=None)
    if j["step"] == "video" and new != "queued" and not ((j.get("payload") or {}).get("trial") or (j.get("payload") or {}).get("echo")):
        enqueue_film(c, j["run_id"], j["ep"], want_host=j["host"])
    return row(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def film_sig(c, run_id: str, ep: int) -> dict:
    """整集输入签名 = 本集每镜最新视频行（id, 状态）。同签名重复入队幂等"""
    rows = c.execute("SELECT shot, MAX(id) id FROM jobs WHERE run_id=? AND ep=? AND step='video' GROUP BY shot", (run_id, ep)).fetchall()
    out = {}
    for r in rows:
        st = c.execute("SELECT state FROM jobs WHERE id=?", (r["id"],)).fetchone()["state"]
        out[r["shot"]] = [r["id"], st]
    return {"videos": dict(sorted(out.items()))}


def enqueue_film(c, run_id: str, ep: int, want_host: str | None = None, force: bool = False) -> tuple[dict, bool]:
    """force = 人工“重新合成”（输入没变也出新一版）"""
    sig = {**film_sig(c, run_id, ep), **({"manual": now_iso()} if force else {})}
    return enqueue(c, run_id=run_id, ep=ep, shot=f"ep{ep:02d}", step="film", want_host=want_host, sig=sig, max_tries=2)


REAP_BACKOFF_S = 120
FORCE_STALE_S = 5 * 60         # force 重派 running 的门槛：心跳停了这么久才认为 worker 已停


def comfy_record(c, job_id: int, host: str, lane: str, pid: str) -> bool:
    """审查 3 B3：worker 提交 ComfyUI 后、wait 之前登记 prompt_id（只认本机正在跑的活）"""
    j = c.execute("SELECT 1 FROM jobs WHERE id=? AND state='running' AND host=?", (job_id, host)).fetchone()
    if j is None:
        return False
    c.execute("INSERT OR IGNORE INTO comfy_prompts(pid, job_id, host, lane, state, at) VALUES (?,?,?,?, 'live', ?)", (pid, job_id, host, lane, now_iso()))
    return True


def comfy_done(c, pid: str, host: str) -> None:
    c.execute("UPDATE comfy_prompts SET state='done', done_at=? WHERE pid=? AND host=? AND state IN ('live','cancel_req','cancel_sent')", (now_iso(), pid, host))


def comfy_cancels(c, host: str) -> list[dict]:
    """要本机 worker 撤掉的 prompt（回收时标的）。取走即标 sent；worker 撤完回报 done"""
    rows = [dict(r) for r in c.execute("SELECT pid, job_id, lane FROM comfy_prompts WHERE host=? AND state='cancel_req'", (host,))]
    for r in rows:
        c.execute("UPDATE comfy_prompts SET state='cancel_sent' WHERE pid=? AND state='cancel_req'", (r["pid"],))
    return rows


def reap(c, lease_s: float = LEASE_S, now: float | None = None) -> list[int]:
    """心跳超时的 running → queued（tries+1；用完 → failed）。审查 3 B3：该活登记过的 ComfyUI prompt 标“要撤”（本机 worker 下次心跳取走
    → /queue 删 + /interrupt），回队后 REAP_BACKOFF_S 内不许再领（防旧 prompt 还在跑就双跑）"""
    t = (now or time.time()) - lease_s
    ids = [r["id"] for r in c.execute("SELECT id FROM jobs WHERE state='running' AND (beat_at IS NULL OR beat_at < ?)", (t,))]
    done = []
    for i in ids:                         # M3：带谓词——中间心跳 / 上报过的不动
        cur = c.execute("UPDATE jobs SET tries=tries+1, host=NULL, lane=NULL, error='心跳超时回收', ready_at=?,"
                        " state=CASE WHEN tries+1 >= max_tries THEN 'failed' ELSE 'queued' END"
                        " WHERE id=? AND state='running' AND (beat_at IS NULL OR beat_at < ?)", ((now or time.time()) + REAP_BACKOFF_S, i, t))
        if cur.rowcount == 1:
            c.execute("UPDATE comfy_prompts SET state='cancel_req' WHERE job_id=? AND state='live'", (i,))
            _event(c, i, "reap")
            done.append(i)
    return done


def requeue(c, job_id: int, reason: str, force: bool = False, adult_of=None) -> dict | None:
    """人工重派（failed / blocked / best_effort → queued，tries 清零、旧 result / 心跳清掉、infra 连续计数从这里重算）。
    force 也收 running（只在确认其 worker 已停时用；活 worker 之后的 finish 会被 409 拒）。没改到任何行 → None（API 回 409）。
    adult_of(run, shot)：成人镜且未触发 → 回队时重新挡住（owner_trigger=1）"""
    states = "('failed','blocked','best_effort','running')" if force else "('failed','blocked','best_effort')"
    j = row(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    if j is None:
        return None
    hold = 1 if (adult_of and j["step"] in ADULT_STEPS and adult_of(j["run_id"], j["shot"]) and not triggered(c, j["run_id"], j["shot"])) else j["owner_trigger"]
    if force and j["state"] == "running" and (j.get("beat_at") or 0) > time.time() - FORCE_STALE_S:
        return None               # 审查 3 M13：force 只收“心跳已停”的 running（最近 FORCE_STALE_S 内还在心跳 = worker 活着，拒）
    cur = c.execute(f"UPDATE jobs SET state='queued', tries=0, host=NULL, lane=NULL, error=NULL, finished_at=NULL, result=NULL, beat_at=NULL, ready_at=NULL,"
                    f" owner_trigger=? WHERE id=? AND state IN {states}", (hold, job_id))
    if cur.rowcount != 1:
        return None
    _event(c, job_id, "requeue", {"reason": reason, "force": force})
    return row(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def retarget(c, run_id: str, ep: int, step: str, want_host: str | None, by: str = "manual") -> list[int]:
    """排队中（queued / blocked）的行改目标机器（P6 11-09 两台都出视频）。只动没在跑的行；每行记事件，可再改回"""
    with tx(c):
        ids = [r["id"] for r in c.execute("SELECT id FROM jobs WHERE run_id=? AND ep=? AND step=? AND state IN ('queued','blocked')"
                                          " AND COALESCE(want_host,'')<>COALESCE(?,'')", (run_id, ep, step, want_host))]
        for i in ids:
            old = c.execute("SELECT want_host FROM jobs WHERE id=?", (i,)).fetchone()["want_host"]
            c.execute("UPDATE jobs SET want_host=? WHERE id=? AND state IN ('queued','blocked')", (want_host, i))
            _event(c, i, "retarget", {"from": old, "to": want_host, "by": by})
    return ids


def set_review(c, run_id: str, shot: str, state: str, reason: str | None = None) -> dict:
    """项目方定稿 / 驳回（PLAN §2.4 PUT /api/shots/{id}/review）。passed 才算 final。
    成人镜触发是另一件事（POST /api/shots/{id}/trigger → db.trigger），定稿不再顺带放行"""
    assert state in ("passed", "rejected", "unreviewed"), state
    c.execute("INSERT INTO reviews(run_id, shot, state, reason, at) VALUES (?,?,?,?,?) ON CONFLICT(run_id, shot) DO UPDATE SET"
              " state=excluded.state, reason=excluded.reason, at=excluded.at", (run_id, shot, state, reason, now_iso()))
    _event(c, None, "review", {"run": run_id, "shot": shot, "state": state, "reason": reason})
    return {"run_id": run_id, "shot": shot, "state": state, "reason": reason}


def review_bulk(c, run_id: str, shots: list[str], state: str = "passed", by: str = "owner", adult_of=None,
                confirm_rejected: list[str] | tuple = ()) -> dict:
    """项目方一键“用这版”（11-09 项目方：逐镜点太累）。只收最新视频行有片（done / best_effort）的镜，其他跳过并写原因；
    已是同状态的不重写。和逐镜 set_review 同一张 reviews 表，口径不变（final = 有片 + passed）。
    审查 3 N7：已驳回的镜不静默覆盖——跳过并列出（带原驳回原因），除非调用方把它逐镜列进 confirm_rejected。
    审查 3：显式成人检查——adult_of(run, shot) 为真且未触发的镜不批量定稿（不只靠视频行状态间接挡）"""
    assert state in ("passed",), state
    done, skipped, rejected = [], {}, {}
    trig = triggers(c, run_id)
    for sh in dict.fromkeys(shots):
        if adult_of is not None and adult_of(run_id, sh) and sh not in trig:
            skipped[sh] = "成人镜未经项目方触发，不批量定稿（L-128）"
            continue
        v = c.execute("SELECT state FROM jobs WHERE run_id=? AND shot=? AND step='video' ORDER BY id DESC LIMIT 1", (run_id, sh)).fetchone()
        if not v or v["state"] not in ("done", "best_effort"):
            skipped[sh] = "还没有片子" if not v else f"视频状态 {v['state']}，没有可用的片"
            continue
        cur = c.execute("SELECT state, reason FROM reviews WHERE run_id=? AND shot=?", (run_id, sh)).fetchone()
        if cur and cur["state"] == state:
            skipped[sh] = "已经是这个状态"
            continue
        if cur and cur["state"] == "rejected" and sh not in confirm_rejected:
            rejected[sh] = cur["reason"] or ""
            skipped[sh] = "已驳回（原因：" + (cur["reason"] or "未写") + "），批量不覆盖；要用这版请逐镜确认"
            continue
        set_review(c, run_id, sh, state, f"一键用这版（{by}）" + ("；覆盖了之前的驳回（逐镜确认）" if cur and cur["state"] == "rejected" else ""))
        done.append(sh)
    _event(c, None, "review_bulk", {"run": run_id, "state": state, "by": by, "done": done, "skipped": skipped,
                                    "confirmed_rejected": [s for s in confirm_rejected if s in done]})
    return {"run_id": run_id, "state": state, "done": done, "skipped": skipped, "rejected": rejected}


def trigger(c, run_id: str, shot: str, by: str = "owner") -> list[int]:
    """项目方触发成人镜（L-128）：记 triggers 表；本镜排队中的出图 / 出片行放行（owner_trigger=0 + payload.owner_triggered）"""
    c.execute("INSERT OR IGNORE INTO triggers(run_id, shot, at, by) VALUES (?,?,?,?)", (run_id, shot, now_iso(), by))
    ids = []
    for r in c.execute("SELECT id, payload FROM jobs WHERE run_id=? AND shot=? AND state='queued' AND step IN ('first','last','video')",
                       (run_id, shot)).fetchall():
        pl = {**json.loads(r["payload"] or "{}"), "owner_triggered": True}
        cur = c.execute("UPDATE jobs SET owner_trigger=0, payload=? WHERE id=? AND state='queued'", (json.dumps(pl, ensure_ascii=False), r["id"]))   # M3 谓词
        if cur.rowcount == 1:
            ids.append(r["id"])
    _event(c, None, "trigger", {"run": run_id, "shot": shot, "by": by, "released": ids})
    return ids


def triggers(c, run_id: str) -> set:
    return {r["shot"] for r in c.execute("SELECT shot FROM triggers WHERE run_id=?", (run_id,))}


def untrigger(c, run_id: str, shot: str, by: str = "owner") -> dict:
    """审查 3 堵点 3：撤销触发。删 triggers 行；本镜排队中的出图 / 出片行重新挡（owner_trigger=1、去掉 payload.owner_triggered）。
    在跑的不打断（返回 running 列表，前端提示）"""
    had = c.execute("DELETE FROM triggers WHERE run_id=? AND shot=?", (run_id, shot)).rowcount
    held = []
    for r in c.execute("SELECT id, payload FROM jobs WHERE run_id=? AND shot=? AND state='queued' AND step IN ('first','last','video')",
                       (run_id, shot)).fetchall():
        pl = {k: v for k, v in json.loads(r["payload"] or "{}").items() if k != "owner_triggered"}
        cur = c.execute("UPDATE jobs SET owner_trigger=1, payload=? WHERE id=? AND state='queued'", (json.dumps(pl, ensure_ascii=False), r["id"]))
        if cur.rowcount == 1:
            held.append(r["id"])
    running = [r["id"] for r in c.execute("SELECT id FROM jobs WHERE run_id=? AND shot=? AND state='running' AND step IN ('first','last','video')",
                                          (run_id, shot))]
    _event(c, None, "untrigger", {"run": run_id, "shot": shot, "by": by, "had": bool(had), "held": held, "running": running})
    return {"had_trigger": bool(had), "held": held, "running": running}


def reviews(c, run_id: str) -> dict:
    return {r["shot"]: dict(r) for r in c.execute("SELECT * FROM reviews WHERE run_id=?", (run_id,))}


def cancel(c, run_id: str, shot: str) -> list[int]:
    """只取消排队中的（在途的不取消，等跑完或心跳回收）；记 na + 原因，不删行"""
    ids = [r["id"] for r in c.execute("SELECT id FROM jobs WHERE run_id=? AND shot=? AND state='queued'", (run_id, shot))]
    done = []
    for i in ids:                         # 审查 3 M3：带状态谓词——检查与写之间被领走的不取消
        if c.execute("UPDATE jobs SET state='na', error='人工取消', finished_at=? WHERE id=? AND state='queued'", (now_iso(), i)).rowcount == 1:
            _event(c, i, "cancel")
            done.append(i)
    return done


def finish_events(c, run_id: str) -> list[dict]:
    """每次尝试一条（耗时归因用）：{job_id, ep, step, at, asked, state, secs, gpu_s, lane, started_at}"""
    q = ("SELECT e.job_id, e.at, e.detail, j.ep, j.step, j.created_at FROM events e JOIN jobs j ON j.id = e.job_id"
         " WHERE e.kind='finish' AND j.run_id=? ORDER BY e.id")
    out = []
    for r in c.execute(q, (run_id,)):
        d = json.loads(r["detail"] or "{}")
        out.append({"job_id": r["job_id"], "ep": r["ep"], "step": r["step"], "at": r["at"], "created_at": r["created_at"], **d})
    return out


def jobs(c, run_id: str | None = None, shot: str | None = None, state: str | None = None, limit: int = 500) -> list[dict]:
    q, a = "SELECT * FROM jobs WHERE 1=1", []
    for col, v in (("run_id", run_id), ("shot", shot), ("state", state)):
        if v:
            q += f" AND {col}=?"
            a.append(v)
    return [row(r) for r in c.execute(q + " ORDER BY id LIMIT ?", (*a, limit))]
