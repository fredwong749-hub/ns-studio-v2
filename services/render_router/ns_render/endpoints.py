"""出图 / 视频的 ComfyUI 地址按卡解析（fredw-92/W2）。

文件 config/render_endpoints.json 与 W1 的 ns_api.llm_config（GET/PUT /api/render/endpoints）同一份：
  {"image": {"url", "gpu": 0}, "video": {"url", "gpu": 3}, "gpus": {"0": url, "3": url},
   "lanes": {"gpu36:0": url | {url, max_frames?, label?}}}     ← G36：其他服务器的道（id "server:n"）
url_for_gpu(n) 优先级：环境变量 NS_COMFY_URL_GPU<n> > NS_COMFY_URL（仅 GPU0）> image/video 中 gpu==n 的 url
> gpus[n] > 内置默认（18188 / 18191）。每次调用都重读文件（PUT 改了立即生效）。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "config" / "render_endpoints.json"
DEFAULT_GPUS = {0: "http://127.0.0.1:18188", 3: "http://127.0.0.1:18191"}
DEFAULT_GPU = {"image": 0, "video": 3}


def _load(path: Path | None = None) -> dict:
    try:
        d = json.loads((path or CONFIG).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


# ------------------------------------------------------------------ 多服务器道（fredw-92/G36）
# 道 id：gpu33 的卡仍是 int（0 / 1 / 3，旧任务、旧配置、旧接口全兼容）；其他服务器的卡是 "<server>:<n>"（如 "gpu36:1"）。
# "gpu33:0" / "0" 归一成 int 0。配置 render_endpoints.json 的 "lanes" 表：{"gpu36:0": url 或 {url, max_frames?, label?}}。
HOME_SERVER = "gpu33"


def norm_lane(lane):
    """道 id 归一：int / "3" / "gpu33:3" → 3；"gpu36:1" → "gpu36:1"；不合法 → ValueError"""
    if isinstance(lane, bool) or lane is None:
        raise ValueError(f"道 id 不对：{lane!r}")
    if isinstance(lane, int):
        return lane
    s = str(lane).strip()
    if s.lstrip("-").isdigit():
        return int(s)
    srv, sep, n = s.partition(":")
    if not sep or not srv or not n.isdigit():
        raise ValueError(f"道 id 不对：{lane!r}（gpu33 的卡写数字，其他服务器写 server:n，如 gpu36:0）")
    return int(n) if srv == HOME_SERVER else f"{srv}:{int(n)}"


def server_of(lane) -> str:
    lane = norm_lane(lane)
    return HOME_SERVER if isinstance(lane, int) else lane.split(":", 1)[0]


def lane_name(lane) -> str:
    """显示名：gpu33:0 / gpu36:1"""
    lane = norm_lane(lane)
    return f"{HOME_SERVER}:{lane}" if isinstance(lane, int) else lane


def _lane_key(lane):
    return (0, lane, "") if isinstance(lane, int) else (1, 0, lane)


def _extra_lanes(path: Path | None = None) -> dict:
    """配置里 lanes 表（非 gpu33 的道）→ {lane_id: {url, max_frames?, label?}}；写成 gpu33:n 的并入 int 道"""
    raw = _load(path).get("lanes")
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        try:
            lid = norm_lane(k)
        except ValueError:
            continue
        rec = {"url": v} if isinstance(v, str) else dict(v) if isinstance(v, dict) else None
        if rec and rec.get("url") and not rec.get("disabled"):
            out[lid] = rec
    return out


def gpu_ids(path: Path | None = None) -> tuple:
    """全部道 id：gpu33 的 int（内置 0 / 3 ∪ gpus 表）在前，其他服务器的 "server:n" 在后"""
    g = _load(path).get("gpus")
    extra = {int(k) for k in g} if isinstance(g, dict) else set()
    return tuple(sorted(set(DEFAULT_GPUS) | extra | set(_extra_lanes(path)), key=_lane_key))


# fredw-92/LS：每条视频道实测能一次出的 H3 最大帧数（768×1344）。config 的 "max_frames": {"<gpu>": n}；
# 没写的道按 DEFAULT_MAX_FRAMES 192（保守值）。实测（ep02-S8，docs/e2e_iteration_log.md 第 4 轮）：3090 + 默认
# 动态显存 + --reserve-vram 1：243 过、277/294 在 MLP fc1 OOM（--reserve-vram 4/7、--disable-pinned-memory、
# --lowvram 都不行）；--novram（权重全放内存、逐层上卡）294/362 都能过 → gpu36:2 长镜头道。
# 超过本道上限的镜头不截短（项目方：“长镜头长度模型决定，不限定时长”）→ video_jobs.route_long 改派长镜头道。
DEFAULT_MAX_FRAMES = 192


def image_only(lane, path: Path | None = None) -> bool:
    """LOCALGPU（11-01）：lanes 记录 "image_only": true 的道（laptop:0 笔记本 12 GB）只出图，任何视频都不派过去"""
    try:
        return bool((_extra_lanes(path).get(norm_lane(lane)) or {}).get("image_only"))
    except ValueError:
        return False


def held(lane, path: Path | None = None) -> bool:
    """LOCALGPU：lanes 记录 "hold": true 的道 = 道就绪但不自动派单（等总调度开派；手动指定这条道仍可用）"""
    try:
        return bool((_extra_lanes(path).get(norm_lane(lane)) or {}).get("hold"))
    except ValueError:
        return False


def image_lanes(path: Path | None = None) -> dict:
    """image_only 道 {显示名: url}（producer 出图道表并入用）"""
    return {lane_name(k): str(v["url"]).rstrip("/") for k, v in _extra_lanes(path).items() if v.get("image_only")}


def max_frames(gpu, path: Path | None = None) -> int:
    mf = _load(path).get("max_frames")
    if image_only(gpu, path):          # LOCALGPU：只出图的道一帧视频都不接（long_lanes / episode_produce / pick_lane 都按它筛）
        return 0
    try:
        lane = norm_lane(gpu)
        key = str(lane)
        if isinstance(mf, dict) and key in mf:
            return int(mf[key])
        if isinstance(mf, dict) and lane_name(lane) in mf:
            return int(mf[lane_name(lane)])
        rec = _extra_lanes(path).get(lane) or {}
        return int(rec["max_frames"]) if rec.get("max_frames") else DEFAULT_MAX_FRAMES
    except (TypeError, ValueError):
        return DEFAULT_MAX_FRAMES


def long_only(lane, path: Path | None = None) -> bool:
    """lanes 记录 "long_only": true 的道（gpu36:2 --novram 长镜头道：每步慢，只接超过其他道上限的视频，不接出图）"""
    try:
        return bool((_extra_lanes(path).get(norm_lane(lane)) or {}).get("long_only"))
    except ValueError:
        return False


def long_lanes(frames: int, path: Path | None = None) -> list:
    """能一次出 frames 帧的道，上限小的在前（够用就不占最长的那条）"""
    ok = [g for g in gpu_ids(path) if max_frames(g, path) >= int(frames)]
    return sorted(ok, key=lambda g: (max_frames(g, path), _lane_key(g)))


def _env_name(lane) -> str:
    return "NS_COMFY_URL_" + "".join(c if c.isalnum() else "_" for c in str(lane)).upper()


def url_for_gpu(gpu, path: Path | None = None) -> str:
    try:
        gpu = norm_lane(gpu)
    except ValueError:
        raise ValueError(f"gpu 只能是 {gpu_ids(path)}")
    if gpu not in gpu_ids(path):
        raise ValueError(f"gpu 只能是 {gpu_ids(path)}")
    if not isinstance(gpu, int):    # G36：其他服务器的道（环境变量 NS_COMFY_URL_GPU36_1 > lanes 表）
        return (os.environ.get(_env_name(gpu)) or str(_extra_lanes(path)[gpu]["url"])).rstrip("/")
    env = os.environ.get(f"NS_COMFY_URL_GPU{gpu}") or (os.environ.get("NS_COMFY_URL") if gpu == 0 else None)
    if env:
        return env.rstrip("/")
    d = _load(path)
    for eid in ("image", "video"):
        e = d.get(eid)
        if isinstance(e, dict) and e.get("url") and int(e.get("gpu", DEFAULT_GPU[eid])) == gpu:
            return str(e["url"]).rstrip("/")
    g = d.get("gpus") if isinstance(d.get("gpus"), dict) else {}
    return str(g.get(str(gpu)) or (_extra_lanes(path).get(gpu) or {}).get("url") or DEFAULT_GPUS.get(gpu)).rstrip("/")


def default_gpu(eid: str, path: Path | None = None):
    e = _load(path).get(eid)
    try:
        return norm_lane(e.get("gpu", DEFAULT_GPU[eid])) if isinstance(e, dict) else DEFAULT_GPU[eid]
    except ValueError:
        return DEFAULT_GPU[eid]


def lane_label(lane, path: Path | None = None) -> str:
    lane = norm_lane(lane)
    rec = _extra_lanes(path).get(lane) or {}
    return str(rec.get("label") or lane_name(lane))


def image_url(path: Path | None = None) -> str:
    """画布出图的默认地址（旧 /render 队列也用它）"""
    return url_for_gpu(default_gpu("image", path), path)


# ------------------------------------------------------------------ 可达性探测（fredw-92/F4）
# /api/render-backend（画布顶栏）与 /api/render/endpoints（模型与 API 页）共用这一份：同一地址同一结果，
# 不会一个说通一个说不通。经 SSH 隧道到 gpu3 时 /system_stats 偶尔要几秒 → 超时放宽到 8 s，结果缓存 15 s
# （画布轮询不会每次都打隧道；同一地址同时只有一个探测在飞，其余等它的结果）。
PROBE_TIMEOUT_S = 8.0
PROBE_TTL_S = 15.0
_probe_cache: dict = {}            # url -> (monotonic 时间, 结果 dict)
_probe_locks: dict = {}
_probe_guard = threading.Lock()


def forget(url: str | None = None) -> None:
    """清缓存（PUT 改地址后立即重新探测）；url=None 全清"""
    with _probe_guard:
        if url is None:
            _probe_cache.clear()
        else:
            _probe_cache.pop(str(url).rstrip("/"), None)


def _probe_now(url: str, timeout: float) -> dict:
    import time
    import urllib.error
    import urllib.request
    out: dict = {"url": url, "reachable": False}
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(url + "/system_stats", timeout=timeout) as r:
            stats = json.loads(r.read().decode() or "{}")
            out.update(reachable=r.status == 200, detail=f"HTTP {r.status}")
    except urllib.error.HTTPError as e:
        out.update(detail=f"HTTP {e.code}", error=f"HTTP {e.code}")
        return out
    except (urllib.error.URLError, OSError, ValueError) as e:
        reason = getattr(e, "reason", e)
        out.update(detail=f"连不上：{reason}", error=str(reason)[:200])
        return out
    finally:
        out["probe_ms"] = round((time.monotonic() - t0) * 1000)
    dev = ((stats or {}).get("devices") or [{}])[0] if isinstance(stats, dict) else {}
    if dev.get("vram_total"):
        out.update(gpu=dev.get("name"), vram_total_mb=dev["vram_total"] // 2**20,
                   vram_used_mb=(dev["vram_total"] - dev.get("vram_free", 0)) // 2**20)
    try:   # 队列长度是附加信息：拿不到不算不可达
        with urllib.request.urlopen(url + "/queue", timeout=timeout) as r:
            q = json.loads(r.read().decode() or "{}")
        out.update(running=len(q.get("queue_running", [])), pending=len(q.get("queue_pending", [])))
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return out


def peek(url: str) -> tuple[float, dict] | None:
    """上次探测结果 (monotonic 时间, 结果)；没探过 → None。只读，不发请求（UIDEBUG：道表展示先用旧值，后台刷新）"""
    with _probe_guard:
        return _probe_cache.get(str(url).rstrip("/"))


def probe(url: str, *, timeout: float | None = None, ttl: float | None = None) -> dict:
    """ComfyUI 地址可达性 {url, reachable, detail, error?, running?, pending?, gpu?, vram_total_mb?, vram_used_mb?,
    probe_ms, cached}；ttl 秒内同一地址复用上次结果"""
    import time
    url = str(url).rstrip("/")
    ttl = PROBE_TTL_S if ttl is None else ttl
    with _probe_guard:
        lk = _probe_locks.setdefault(url, threading.Lock())
    with lk:
        with _probe_guard:
            hit = _probe_cache.get(url)
        if hit and time.monotonic() - hit[0] < ttl:
            return {**hit[1], "cached": True}
        res = _probe_now(url, PROBE_TIMEOUT_S if timeout is None else timeout)
        with _probe_guard:
            _probe_cache[url] = (time.monotonic(), res)
        return {**res, "cached": False}
