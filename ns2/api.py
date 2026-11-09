"""ns2 API（P5 修正 ①：跑在 gpu33，绑 10.0.0.33 + 127.0.0.1）。queue.db 唯一写者。

启动：NS2_DB=/root/work/fredw/ns2/data/queue.db uvicorn ns2.api:app --host 10.0.0.33 --port 8900（只绑内网口；全部接口要 X-NS2-Key，见 ns2/auth.py）
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
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from fastapi.responses import JSONResponse

from ns2 import auth as AU
from ns2 import db as D
from ns2 import overview as OV

DB_PATH = os.environ.get("NS2_DB", "data/queue.db")
ROOT = Path(os.environ.get("NS2_ROOT") or Path(__file__).resolve().parents[1])
SELF = os.environ.get("NS2_SELF", "gpu33")
FILE_PORTS = {"gpu36": "http://10.0.0.36:8901", "gpu33": "http://10.0.0.33:8901"}
app = FastAPI(title="ns2")


_adult_cache: dict = {}


def is_adult(run: str, shot: str) -> bool:
    """B2：成人镜以剧本导入稿每镜 adult 字段为准（旧导入器 classify_adult 写的，与旧系统同口径）；读不到剧本 = 当成人（fail-closed）"""
    import json as _json
    f = ROOT / "data/import/runs" / run / "script_v2.json"
    try:
        mt = f.stat().st_mtime
    except OSError:
        return True
    if _adult_cache.get(run, (None,))[0] != mt:
        try:
            d = _json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True
        _adult_cache[run] = (mt, {f"ep{int(e['number']):02d}-S{int(x['index'])}": bool(x.get("adult"))
                                  for e in d.get("episodes") or [] for x in e.get("shots") or [] if x.get("index")})
    return _adult_cache[run][1].get(shot, True)          # 剧本里没有的镜 → 当成人（fail-closed）


@app.middleware("http")
async def _auth(request, call_next):
    """B1（任务书：全部写接口要共享密钥头）：非 GET 一律要 X-NS2-Key；worker 接口全要；读不到密钥 = 写全拒（fail-closed）。
    只读 GET（总览 / 队列 / 媒体 / 前端页）不要密钥，供浏览器看；/files 只给媒体与 JSON（不给 queue.db 等，M12）"""
    path = request.url.path
    if (request.method not in ("GET", "HEAD") or path.startswith("/api/worker/")) and not AU.ok(request.headers.get(AU.HEADER)):
        return JSONResponse({"detail": "写操作需要 X-NS2-Key"}, status_code=401)
    return await call_next(request)
_lock = threading.Lock()          # 单进程单写者：所有写串行
_conn = None
_workers: dict[str, dict] = {}


def conn():
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
        _conn = D.connect(DB_PATH)
    return _conn


_reaper_state = {"ok_at": None, "err": None, "reaped": 0}


def reap_once() -> None:
    """M2：一轮回收；出错记下、不抛（线程不能静默死），/api/health 报上次成功时间与最近错误"""
    import traceback
    try:
        with _lock:
            n = D.reap(conn())
        _reaper_state.update(ok_at=time.time(), err=None, reaped=_reaper_state["reaped"] + len(n))
    except Exception as e:  # noqa: BLE001
        _reaper_state["err"] = f"{type(e).__name__}: {e}"[:300]
        print("REAPER_ERR", traceback.format_exc()[-800:], flush=True)


def _reaper():
    while True:
        time.sleep(60)
        reap_once()


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
    rs = _reaper_state
    return {"ok": rs["err"] is None, "db": DB_PATH, "jobs": {r["state"]: r["n"] for r in n},
            "reaper": {"ok_age_s": round(time.time() - rs["ok_at"], 1) if rs["ok_at"] else None, "err": rs["err"], "reaped": rs["reaped"]},
            "workers": {h: {**w, "age_s": round(time.time() - w["at"], 1)} for h, w in _workers.items()}}


@app.get("/api/jobs")
def list_jobs(run: str | None = None, shot: str | None = None, state: str | None = None, limit: int = 500):
    with _lock:
        return D.jobs(conn(), run, shot, state, limit)


LIVE_STATES = ("queued", "blocked")


def _enqueue_checked(e: Enq):
    if e.step not in D.STEPS or e.state not in D.STATES or e.state == "running":
        raise HTTPException(400, f"step/state 不认识：{e.step}/{e.state}")
    if e.key:
        raise HTTPException(400, "不接受调用方自带幂等键（key 由服务端按 run/shot/step/sig 算）")
    with _lock:
        j, created = D.enqueue(conn(), **{**e.model_dump(), "adult": e.step in D.ADULT_STEPS and is_adult(e.run_id, e.shot)})
    return {"job": j, "created": created}


@app.post("/api/jobs")
def enqueue(e: Enq):
    """live 入队：只收 queued / blocked，且不收调用方自带 result（审查 3 B1：持密钥可伪造 done + 任意 result）。
    迁移导入的终态行走 /api/import/jobs（默认关）"""
    if e.step not in D.STEPS or e.state not in D.STATES or e.state == "running":
        raise HTTPException(400, f"step/state 不认识：{e.step}/{e.state}")
    if e.state not in LIVE_STATES or e.result is not None:
        raise HTTPException(403, "live 入队只收 queued / blocked、不带 result；迁移导入终态走 /api/import/jobs（API 须以 NS2_ALLOW_IMPORT=1 启动）")
    return _enqueue_checked(e)


@app.post("/api/import/jobs")
def import_job(e: Enq):
    """迁移导入（一次性）：可写 done / na / best_effort / failed 与 result。只有 API 以 NS2_ALLOW_IMPORT=1 启动时开（平时 403）"""
    if os.environ.get("NS2_ALLOW_IMPORT") != "1":
        raise HTTPException(403, "导入口默认关闭：迁移时以 NS2_ALLOW_IMPORT=1 重启 API，导完再关")
    return _enqueue_checked(e)



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
        j = D.requeue(conn(), job_id, reason, force, adult_of=is_adult)
    if j is None:
        raise HTTPException(409, "没有重派：任务不存在，或不在 failed / blocked / best_effort（force 才收 running）")
    return j


@app.put("/api/runs")
def put_run(r: RunReq):
    if r.status not in ("producing", "paused", "archived"):
        raise HTTPException(400, "status 只能是 producing / paused / archived")
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
    if f.state not in ("done", "failed", "blocked", "best_effort", "na", "retry", "infra"):
        raise HTTPException(400, f"state 不认识：{f.state}")
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


class BulkReviewReq(BaseModel):
    run_id: str = "series10_v2"
    shots: list[str]
    state: str = "passed"
    by: str = "owner"


@app.post("/api/reviews/bulk")
def review_bulk(r: BulkReviewReq):
    """一键用这版：一次请求定一批镜（没片的跳过并说明原因）"""
    if r.state != "passed":
        raise HTTPException(400, "批量只支持 passed（用这版）；驳回要逐镜写原因")
    if len(r.shots) > 500:
        raise HTTPException(400, "一次最多 500 镜")
    with _lock:
        return D.review_bulk(conn(), r.run_id, r.shots, r.state, r.by)


@app.post("/api/auth/check")
def auth_check():
    """前端保存密钥时验一下对不对（中间件已验；能到这里就是对的）"""
    return {"ok": True}


@app.post("/api/shots/{shot_id}/cancel")
def cancel(shot_id: str, run: str = "series10_v2"):
    with _lock:
        return {"cancelled": D.cancel(conn(), run, shot_id)}


MEDIA_EXT = AU.MEDIA_EXT


def _safe_rel(rel: str) -> str:
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        raise HTTPException(400, "bad path")
    if p.suffix.lower() not in MEDIA_EXT:            # M12：不经 /files 外泄 queue.db / 密钥 / 日志等
        raise HTTPException(403, "只给图 / 片 / JSON")
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
        up = urllib.request.urlopen(urllib.request.Request(f"{base}/files/{urllib.request.quote(rel)}", headers={**hdr, **AU.headers()}), timeout=30)
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
        j, created = D.enqueue(c, run_id=r.run_id, ep=ep, shot=shot_id, step=r.stage, want_host=host,
                               payload={"edit": r.edit.strip()} if r.mode == "edit" else {},
                               sig={"manual": D.now_iso(), "by": r.by, "mode": r.mode}, adult=is_adult(r.run_id, shot_id),
                               max_tries=4 if r.stage == "video" else 3)
    return {"job": j, "created": created, "adult_hold": bool(j["owner_trigger"])}


class TriggerReq(BaseModel):
    run_id: str = "series10_v2"
    by: str = "owner"


@app.post("/api/shots/{shot_id}/trigger")
def trigger(shot_id: str, t: TriggerReq):
    """L-128：项目方触发成人镜（只由项目方在前端点；放行本镜排队中的出图 / 出片行，之后新起的行直接带触发）"""
    with _lock:
        return {"released": D.trigger(conn(), t.run_id, shot_id, t.by)}


# P2：前端与 API 同源（任务书：端口 8900，不用参考包的 8800）。总览 /ui/index.html（15 s 轮询）、审片 /ui/review.html?shot=
_FRONT = ROOT / "frontend"
if _FRONT.is_dir():
    app.mount("/ui", StaticFiles(directory=str(_FRONT), html=True), name="ui")


@app.get("/")
def _home():
    return RedirectResponse("/ui/index.html")
