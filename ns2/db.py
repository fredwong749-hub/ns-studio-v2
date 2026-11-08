"""队列表（PLAN §2.3 + P5 修正）：派单的唯一事实来源。只有 API 进程写它（单写者），worker 经 HTTP 认领 / 心跳 / 上报。

语义：
- FIFO（created_at, id），无 priority 字段，无让路。
- 同镜顺序：first 未 done/na 不领 last；first、last 未 done/na 不领 video（P5 修正 ④：QC 没过的帧不进视频）。
- 幂等：idem_key UNIQUE，同输入重复入队返回已有行。
- 认领原子：BEGIN IMMEDIATE + 条件 UPDATE。
- 失败分级：retry（QC 没过 / infra）→ tries<max 回 queued，否则 best_effort（有产物）或 failed（无产物）；blocked 不计 tries。
- 心跳超时（LEASE_S）回收为 queued、tries+1。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

STEPS = ("script", "board", "first", "last", "video", "film")
STATES = ("queued", "running", "done", "failed", "blocked", "best_effort", "na")
PREREQ = {"last": ("first",), "video": ("first", "last")}
PASSED = ("done", "na")
LEASE_S = 45 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL, ep INTEGER NOT NULL, shot TEXT NOT NULL,
  step TEXT NOT NULL, stage TEXT,
  state TEXT NOT NULL DEFAULT 'queued',
  payload TEXT NOT NULL DEFAULT '{}', result TEXT,
  want_host TEXT, lane TEXT, host TEXT,
  tries INTEGER NOT NULL DEFAULT 0, max_tries INTEGER NOT NULL DEFAULT 3,
  idem_key TEXT UNIQUE, owner_trigger INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, beat_at REAL,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(state, created_at, id);
CREATE INDEX IF NOT EXISTS idx_jobs_shot ON jobs(run_id, shot, step);
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'producing');
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
    return c


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


def enqueue(c, *, run_id: str, ep: int, shot: str, step: str, payload: dict | None = None, want_host: str | None = None,
            sig: dict | None = None, state: str = "queued", result: dict | None = None, owner_trigger: bool = False,
            max_tries: int = 3, key: str | None = None) -> tuple[dict, bool]:
    """→ (job, created)。同 idem_key 已有 → 原行、created=False"""
    assert step in STEPS and state in STATES, (step, state)
    k = key or idem_key(run_id, shot, step, sig)
    old = c.execute("SELECT * FROM jobs WHERE idem_key=?", (k,)).fetchone()
    if old is not None:
        return row(old), False
    cur = c.execute(
        "INSERT INTO jobs(run_id, ep, shot, step, stage, state, payload, result, want_host, max_tries, idem_key, owner_trigger, created_at, finished_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, ep, shot, step, STAGE_OF.get(step), state, json.dumps(payload or {}, ensure_ascii=False),
         json.dumps(result, ensure_ascii=False) if result is not None else None, want_host, max_tries, k,
         1 if owner_trigger else 0, now_iso(), now_iso() if state in ("done", "na", "best_effort") else None))
    _event(c, cur.lastrowid, "enqueue", {"state": state})
    return row(c.execute("SELECT * FROM jobs WHERE id=?", (cur.lastrowid,)).fetchone()), True


def _claim_sql(step: str) -> str:
    """前置步 = 该镜该步“最新一行”（id 最大，重派会新起一行）必须 done/na；没入队 = 不领"""
    pre = "".join(
        f" AND (SELECT p.state FROM jobs p WHERE p.run_id=j.run_id AND p.shot=j.shot AND p.step='{p}'"
        f" ORDER BY p.id DESC LIMIT 1) IN ('done','na')" for p in PREREQ.get(step, ()))
    return ("SELECT j.* FROM jobs j JOIN runs r ON r.run_id = j.run_id"
            " WHERE j.state='queued' AND j.owner_trigger=0 AND r.status='producing'"
            " AND (j.want_host IS NULL OR j.want_host=?) AND j.step=?" + pre +
            " ORDER BY j.created_at, j.id LIMIT 1")


def claim(c, host: str, lane: str, steps: list[str]) -> dict | None:
    """原子认领一条：FIFO（跨所给 steps 取最早的一条）、本机、前置步已过"""
    steps = [s for s in steps if s in STEPS]
    if not steps:
        return None
    c.execute("BEGIN IMMEDIATE")
    try:
        cands = [r for s in steps if (r := c.execute(_claim_sql(s), (host, s)).fetchone()) is not None]
        for r in sorted(cands, key=lambda r: (r["created_at"], r["id"]))[:1]:
            c.execute("UPDATE jobs SET state='running', host=?, lane=?, started_at=?, beat_at=?, error=NULL WHERE id=? AND state='queued'",
                      (host, lane, now_iso(), time.time(), r["id"]))
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
    """state ∈ done|failed|blocked|best_effort|na|retry。retry：tries+1，没用完回 queued；用完 → 有产物 best_effort / 无产物 failed"""
    j = row(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    if j is None or j["state"] != "running" or j["host"] != host:
        return None
    tries = j["tries"]
    if state == "retry":
        tries += 1
        if tries < j["max_tries"]:
            new = "queued"
        else:
            new = "best_effort" if (result or {}).get("has_output") else "failed"
    else:
        assert state in STATES and state not in ("queued", "running"), state
        new = state
    c.execute("UPDATE jobs SET state=?, tries=?, result=?, error=?, finished_at=?, host=CASE WHEN ?='queued' THEN NULL ELSE host END,"
              " lane=CASE WHEN ?='queued' THEN NULL ELSE lane END WHERE id=?",
              (new, tries, json.dumps(result, ensure_ascii=False) if result is not None else j.get("result") and json.dumps(j["result"], ensure_ascii=False),
               error, None if new == "queued" else now_iso(), new, new, job_id))
    _event(c, job_id, "finish", {"asked": state, "state": new, "tries": tries, "error": (error or "")[:300]})
    return row(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def reap(c, lease_s: float = LEASE_S, now: float | None = None) -> list[int]:
    """心跳超时的 running → queued（tries+1；用完 → failed）"""
    t = (now or time.time()) - lease_s
    ids = [r["id"] for r in c.execute("SELECT id FROM jobs WHERE state='running' AND (beat_at IS NULL OR beat_at < ?)", (t,))]
    for i in ids:
        c.execute("UPDATE jobs SET tries=tries+1, host=NULL, lane=NULL, error='心跳超时回收',"
                  " state=CASE WHEN tries+1 >= max_tries THEN 'failed' ELSE 'queued' END WHERE id=?", (i,))
        _event(c, i, "reap")
    return ids


def requeue(c, job_id: int, reason: str, force: bool = False) -> dict | None:
    """人工重派（failed / blocked / best_effort → queued，tries 清零）；force 也收 running（其 worker 已停，不等 45 分钟回收）"""
    states = "('failed','blocked','best_effort','running')" if force else "('failed','blocked','best_effort')"
    c.execute(f"UPDATE jobs SET state='queued', tries=0, host=NULL, lane=NULL, error=NULL, finished_at=NULL WHERE id=? AND state IN {states}",
              (job_id,))
    _event(c, job_id, "requeue", {"reason": reason})
    return row(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def jobs(c, run_id: str | None = None, shot: str | None = None, state: str | None = None, limit: int = 500) -> list[dict]:
    q, a = "SELECT * FROM jobs WHERE 1=1", []
    for col, v in (("run_id", run_id), ("shot", shot), ("state", state)):
        if v:
            q += f" AND {col}=?"
            a.append(v)
    return [row(r) for r in c.execute(q + " ORDER BY id LIMIT ?", (*a, limit))]
