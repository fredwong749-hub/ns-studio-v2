"""ns2 API（P5 修正 ①：跑在 gpu33，绑 10.0.0.33 + 127.0.0.1）。queue.db 唯一写者。

启动：NS2_DB=/root/work/fredw/ns2/data/queue.db uvicorn ns2.api:app --host 0.0.0.0 --port 8900
M1a 接口：health / jobs / enqueue / requeue / runs / worker claim·heartbeat·finish。overview 与前端在 M2。
"""
from __future__ import annotations

import os
import threading
import time

import urllib.error
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ns2 import db as D
from ns2 import overview as OV

DB_PATH = os.environ.get("NS2_DB", "data/queue.db")
ROOT = Path(os.environ.get("NS2_ROOT") or Path(__file__).resolve().parents[1])
SELF = os.environ.get("NS2_SELF", "gpu33")
FILE_PORTS = {"gpu36": "http://10.0.0.36:8901", "gpu33": "http://10.0.0.33:8901"}
app = FastAPI(title="ns2")
_lock = threading.Lock()          # 单进程单写者：所有写串行
_conn = None
_workers: dict[str, dict] = {}


def conn():
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
        _conn = D.connect(DB_PATH)
    return _conn


def _reaper():
    while True:
        time.sleep(60)
        with _lock:
            D.reap(conn())


@app.on_event("startup")
def _start():
    conn()
    threading.Thread(target=_reaper, daemon=True, name="reaper").start()


class Enq(BaseModel):
    run_id: str
    ep: int
    shot: str
    step: str
    payload: dict = {}
    want_host: str | None = None
    sig: dict | None = None
    state: str = "queued"
    result: dict | None = None
    owner_trigger: bool = False
    max_tries: int = 3
    key: str | None = None
    error: str | None = None


class Claim(BaseModel):
    host: str
    lane: str
    steps: list[str]


class Beat(BaseModel):
    host: str
    job_id: int | None = None
    lanes: list[str] = []


class Fin(BaseModel):
    host: str
    job_id: int
    state: str
    result: dict | None = None
    error: str | None = None


class RunReq(BaseModel):
    run_id: str
    title: str = ""
    status: str = "producing"


@app.get("/api/health")
def health():
    with _lock:
        n = conn().execute("SELECT state, COUNT(*) n FROM jobs GROUP BY state").fetchall()
    return {"ok": True, "db": DB_PATH, "jobs": {r["state"]: r["n"] for r in n},
            "workers": {h: {**w, "age_s": round(time.time() - w["at"], 1)} for h, w in _workers.items()}}


@app.get("/api/jobs")
def list_jobs(run: str | None = None, shot: str | None = None, state: str | None = None, limit: int = 500):
    with _lock:
        return D.jobs(conn(), run, shot, state, limit)


@app.post("/api/jobs")
def enqueue(e: Enq):
    if e.step not in D.STEPS or e.state not in D.STATES:
        raise HTTPException(400, f"step/state 不认识：{e.step}/{e.state}")
    with _lock:
        j, created = D.enqueue(conn(), **e.model_dump())
    return {"job": j, "created": created}


class FilmReq(BaseModel):
    run_id: str
    want_host: str | None = None
    force: bool = False


@app.post("/api/episodes/{ep}/assemble")
def assemble(ep: int, f: FilmReq):
    """PLAN §2.4：给该集插一行 film 任务。签名 = 本集每镜最新视频行，同签名幂等；force=true 出新一版。
    （视频步每定一镜 db.finish 也会自动入队，平时不用手点）"""
    with _lock:
        j, created = D.enqueue_film(conn(), f.run_id, ep, f.want_host, f.force)
    return {"job": j, "created": created}


@app.post("/api/jobs/{job_id}/requeue")
def requeue(job_id: int, reason: str = "manual", force: bool = False):
    with _lock:
        j = D.requeue(conn(), job_id, reason, force)
    if j is None:
        raise HTTPException(404)
    return j


@app.put("/api/runs")
def put_run(r: RunReq):
    with _lock:
        conn().execute("INSERT INTO runs(run_id, title, status) VALUES (?,?,?) ON CONFLICT(run_id) DO UPDATE SET title=excluded.title, status=excluded.status",
                       (r.run_id, r.title, r.status))
    return r


@app.post("/api/worker/claim")
def claim(c: Claim):
    _workers.setdefault(c.host, {"at": time.time()})["at"] = time.time()
    with _lock:
        return {"job": D.claim(conn(), c.host, c.lane, c.steps)}


@app.post("/api/worker/heartbeat")
def heartbeat(b: Beat):
    _workers[b.host] = {"at": time.time(), "lanes": b.lanes}
    ok = True
    if b.job_id is not None:
        with _lock:
            ok = D.heartbeat(conn(), b.job_id, b.host)
    return {"ok": ok}


@app.post("/api/worker/finish")
def finish(f: Fin):
    with _lock:
        j = D.finish(conn(), f.job_id, f.host, f.state, f.result, f.error)
    if j is None:
        raise HTTPException(409, "不是本机在跑的任务（可能已被心跳回收）")
    return j


@app.get("/api/overview")
def overview(run: str = "series10_v2"):
    """PLAN §2.4 唯一总览只读接口（口径见 ns2/overview.py）"""
    with _lock:
        rows = D.jobs(conn(), run, limit=1_000_000)
        rv = D.reviews(conn(), run)
        ev = D.finish_events(conn(), run)
    return OV.build(run, rows, rv, OV.script_eps(ROOT, run), D.now_iso(), ev)


class ReviewReq(BaseModel):
    run_id: str = "series10_v2"
    state: str
    reason: str | None = None


@app.put("/api/shots/{shot_id}/review")
def review(shot_id: str, r: ReviewReq):
    if r.state not in ("passed", "rejected", "unreviewed"):
        raise HTTPException(400, "state 只能是 passed / rejected / unreviewed")
    with _lock:
        return D.set_review(conn(), r.run_id, shot_id, r.state, r.reason)


@app.post("/api/shots/{shot_id}/cancel")
def cancel(shot_id: str, run: str = "series10_v2"):
    with _lock:
        return {"cancelled": D.cancel(conn(), run, shot_id)}


def _safe_rel(rel: str) -> str:
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        raise HTTPException(400, "bad path")
    return p.as_posix()


@app.get("/files/{host}/{rel:path}")
def files(host: str, rel: str, request: Request):
    """出图 / 视频在产出那台机器上：一律经该机 worker 的只读文件口取（本机也走，统一 Range 语义），Range 透传"""
    rel = _safe_rel(rel)
    base = FILE_PORTS.get(host)
    if not base:
        raise HTTPException(404, f"不认识的机器 {host}")
    hdr = {"Range": request.headers["range"]} if request.headers.get("range") else {}
    try:
        up = urllib.request.urlopen(urllib.request.Request(f"{base}/files/{urllib.request.quote(rel)}", headers=hdr), timeout=30)
    except urllib.error.HTTPError as e:
        raise HTTPException(e.code)
    except OSError as e:
        raise HTTPException(502, f"{host} 文件口不通：{e}")
    keep = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges")
    return StreamingResponse(iter(lambda: up.read(1 << 16), b""), status_code=up.status,
                             headers={k: up.headers[k] for k in keep if up.headers.get(k)})


class DispatchReq(BaseModel):
    run_id: str = "series10_v2"
    stage: str                    # first | last | video
    mode: str = "regen"           # regen（重出）| edit（按修改意见重出，首 / 尾帧）
    edit: str | None = None
    by: str = "manual"


@app.post("/api/shots/{shot_id}/jobs")
def dispatch(shot_id: str, r: DispatchReq):
    """PLAN §2.4 派单：给本镜某一步新起一行（人工重出 / 按意见修改）。机器按出图 / 视频分区；前置（首帧过 → 尾帧 → 视频）照队列规则等。
    成人镜（本镜 board 判成人或已有行带 owner_trigger）新行同样等项目方触发。"""
    from ns2.migrate import host_for
    if r.stage not in ("first", "last", "video") or r.mode not in ("regen", "edit"):
        raise HTTPException(400, "stage = first / last / video；mode = regen / edit")
    if r.mode == "edit" and (r.stage == "video" or not (r.edit or "").strip()):
        raise HTTPException(400, "修改只对首 / 尾帧，且要写修改意见")
    try:
        ep = int(shot_id.split("-")[0][2:])
    except ValueError:
        raise HTTPException(400, "镜号格式 epNN-SN")
    host = host_for("video" if r.stage == "video" else "images", ep)
    with _lock:
        c = conn()
        adult = c.execute("SELECT 1 FROM jobs WHERE run_id=? AND shot=? AND (owner_trigger=1 OR (step='board' AND result LIKE '%\"adult\": true%'))",
                          (r.run_id, shot_id)).fetchone() is not None
        j, created = D.enqueue(c, run_id=r.run_id, ep=ep, shot=shot_id, step=r.stage, want_host=host,
                               payload={"edit": r.edit.strip()} if r.mode == "edit" else {},
                               sig={"manual": D.now_iso(), "by": r.by, "mode": r.mode}, owner_trigger=adult,
                               max_tries=4 if r.stage == "video" else 3)
    return {"job": j, "created": created, "adult_hold": adult}
