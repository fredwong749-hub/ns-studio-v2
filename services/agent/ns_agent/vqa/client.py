"""调本地看图服务（gpu36 tools/vqa/server.py）。只走自家机器：本机 127.0.0.1:8870（在 gpu36 上跑时）或 `ssh gpu36` 管道。
绝不把图片发到任何外部地址（K-REV-V01）：URL 只允许 127.0.0.1 / localhost，远端只允许 config model.host。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import ROOT, cfg, rubric

CACHE = ROOT / "data" / "vqa" / "verdicts"
LOCAL_HOSTS = ("127.0.0.1", "localhost")


def _url() -> str:
    m = cfg().get("model") or {}
    u = os.environ.get("VQA_URL") or f"http://127.0.0.1:{m.get('port', 8870)}"
    host = u.split("//", 1)[-1].split(":", 1)[0].split("/", 1)[0]
    if host not in LOCAL_HOSTS:
        raise ValueError(f"K-REV-V01：看图服务只能是本机地址，拒绝 {u}")
    return u


def _lan_url() -> str | None:
    """NETFIX 11-01（K-OPS-N03）：gpu36 看图服务的内网入口（tools/netfix/lan_proxy.py，只听 10.0.0.36、只认 gpu33 来源）。
    只在服务器上用（OFFLINE_SELF 有值 = 常驻 producer；或 VQA_LAN=1）；地址只认 config model.lan_url，且必须是 10. 内网地址——
    图片仍只在自家机器之间走（K-REV-V01）。笔记本上不试（10.0.0.36 不可达，白等超时）。"""
    if not (os.environ.get("OFFLINE_SELF") or os.environ.get("VQA_LAN") == "1"):
        return None
    m = cfg().get("model") or {}
    u = m.get("lan_url")
    if os.environ.get("VQA_LAN_URL") is not None:
        # L-206 工位分卡：审图位由 config/pipeline/lane_roles.json review 定（tools/series10_video/lane_roles.review_env 写环境）：
        # 显式给的内网入口优先（空串 = 不走内网）；仍只认 10. 内网地址（K-REV-V01），“同一台”判断不适用（审图位可能就是别台）
        u = os.environ["VQA_LAN_URL"] or None
    elif not u or os.environ.get("OFFLINE_SELF") == m.get("host"):
        return None
    if not u or not u.split("//", 1)[-1].split(":", 1)[0].startswith("10."):
        return None
    return u if _local_up(u) else None


def _key(req: dict) -> str:
    h = hashlib.sha256()
    h.update((cfg().get("version", "") + req.get("system", "") + req["text"]).encode("utf-8"))
    for im in req["images"]:
        p = Path(im["path"])
        h.update(p.read_bytes() if p.exists() else im["path"].encode())
        h.update(str(im.get("max_pixels")).encode())
    return h.hexdigest()[:24]


_HEALTH: dict[str, dict] = {}       # url → 最近一次 /health 的 JSON（_local_up 顺手存；路由读 loaded / queue）


def _rcfg() -> dict:
    return cfg().get("route") or {}


def _local_up(url: str) -> bool:
    """W23：/health 通 = True，并把回包（loaded / queue）存进 _HEALTH 供路由判断。tools/vqa/*_ab.py、测试会把它换成 lambda
    强制走隧道——那时 _HEALTH 没有记录，路由按"已加载、不排队"处理"""
    _HEALTH.pop(url, None)
    try:
        with urllib.request.urlopen(url + "/health", timeout=float(_rcfg().get("health_timeout_s", 3))) as r:
            try:
                h = json.loads(r.read() or b"{}")
            except ValueError:
                h = {}
            _HEALTH[url] = h if isinstance(h, dict) else {}
            return True
    except (OSError, urllib.error.URLError) as e:
        print(f"[vqa.client] health 探测失败 {url}: {e!r}", file=sys.stderr, flush=True)
        return False


def _ready(url: str) -> tuple[bool, int]:
    """→ (在且已加载, 排队数)。不在 / loaded=false → (False, 0)"""
    if not _local_up(url):
        return False, 0
    h = _HEALTH.get(url) or {}
    # W38（11-04 05:3x gpu36 实测）：看图服务空闲会卸模型（loaded=false），来请求再加载（load_s≈3 s）；原来 loaded=false 一律判“不在”
    # → 服务器上谁都不发请求 → 永远不重新加载 → 看图全线“未测”（视频 Q9、首尾帧 Q9/QB 反复 score_infra）。
    # 服务器上（OFFLINE_SELF）本机 8870 就是正主：通了即用，loaded=false 照发（服务端排队加载）；笔记本保持原口径（未加载 → 走隧道）
    if h.get("loaded") is False and not os.environ.get("OFFLINE_SELF"):
        return False, 0
    try:
        return True, int(h.get("queue") or 0)
    except (TypeError, ValueError):
        return True, 0


ROUTE_LOG = ROOT / "data" / "vqa" / "route.jsonl"


def _log_route(route: str, ms: float, ok: bool, kind: str, reason: str = "") -> None:
    """W23：每次调用（缓存命中除外）追加一行 {at, route: local|tunnel|lan|none, ms, ok, kind}，总调度看路由占比"""
    f = ROUTE_LOG
    if f == ROOT / "data" / "vqa" / "route.jsonl":
        if os.environ.get("PYTEST_CURRENT_TEST"):          # 测试不写真日志（测试里把 ROUTE_LOG 换成 tmp 路径）
            return
        f = ROOT / (_rcfg().get("log") or "data/vqa/route.jsonl")
    row = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": route, "ms": int(ms), "ok": bool(ok), "kind": kind}
    if reason:
        row["reason"] = reason
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[vqa.client] 路由日志写失败 {f}: {e!r}", file=sys.stderr, flush=True)


def _untested(reason: str, detail: str = "") -> dict:
    """看图没跑成 ≠ 图有问题：带 error（contract._item → Q9 untested、evidence 无 model → accept.q9_timeout 认作 retry）"""
    return {"error": f"看图未测：{reason}" + (f"（{detail}）" if detail else ""), "status": "untested", "reason": reason}


def _to_bytes(body: dict) -> None:
    """隧道 / 内网那头是 gpu36，读不到本机路径：先缩图转字节（20:xx gpu36 FileNotFoundError）"""
    for im in body["images"]:
        if "path" in im and Path(im["path"]).exists():
            im["b64"] = base64.b64encode(_shrink(Path(im["path"]), im.get("max_pixels"))).decode()
            im.pop("path")


def _post_local(url: str, body: dict, timeout: float) -> dict:
    r = urllib.request.Request(url + "/generate", data=json.dumps(body).encode("utf-8"),
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}") or {"error": f"HTTP {e.code}"}


def _shrink(p: Path, max_pixels) -> bytes:
    """VQAGATE 11-01：笔记本上行慢（K-OPS-302），原图 PNG 2–4 MB × 5 张走 ssh 一张要几分钟。服务端本来就按 max_pixels 缩，
    这里先在本机缩到 max_pixels 再转 JPEG q92 发（缓存键仍按原图字节算，判定口径不变）。读不了就发原字节"""
    try:
        import io
        from PIL import Image
        with Image.open(p) as im:
            im = im.convert("RGB")
            mp = int(max_pixels or 1300000)
            if im.width * im.height > mp:
                k = (mp / (im.width * im.height)) ** 0.5
                im = im.resize((max(28, int(im.width * k)), max(28, int(im.height * k))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=92)
            return buf.getvalue()
    except Exception:  # noqa: BLE001
        return p.read_bytes()


def _post_ssh(body: dict, timeout: float) -> dict:
    m = cfg().get("model") or {}
    host, port = m.get("host", "gpu36"), m.get("port", 8870)
    # 图片以字节随请求走 ssh 管道到自家 gpu36，服务只听 127.0.0.1
    for im in body["images"]:
        if "path" in im and Path(im["path"]).exists():
            im["b64"] = base64.b64encode(_shrink(Path(im["path"]), im.get("max_pixels"))).decode()
            im.pop("path")
    cmd = ["ssh", "-o", "BatchMode=yes", host,
           f"curl -s -m {int(timeout)} -X POST -H 'Content-Type: application/json' --data-binary @- http://127.0.0.1:{port}/generate"]
    p = subprocess.run(cmd, input=json.dumps(body).encode("utf-8"), capture_output=True, timeout=timeout + 30)
    try:
        return json.loads(p.stdout or b"{}") or {"error": (p.stderr or b"").decode("utf-8", "replace")[:300] or "空回复"}
    except ValueError:
        return {"error": (p.stdout or b"")[:300].decode("utf-8", "replace")}


def _balance_cands(url: str) -> list[tuple[str, str]]:
    """B1（11-05）服务器侧多实例分发：[(url, route)]，本机实例（主端口 + route.extra_local_ports，只许 127.0.0.1）在前，gpu36 内网入口殿后。
    只在 route.balance 打开且在服务器上（OFFLINE_SELF）才有；返回 [] = 走原 W23 路由。K-REV-V01：仍然只有本机 / 10. 内网地址"""
    rc = _rcfg()
    if not (rc.get("balance") and os.environ.get("OFFLINE_SELF")):
        return []
    out = [(url, "local")] + [(f"http://127.0.0.1:{int(p)}", "local") for p in rc.get("extra_local_ports") or []]
    if _review_local_only(out):                      # L-206：本机是审图位且本机实例活着 → 全部看图留在审图卡，不分流到 gpu36（那张卡是出图位）
        return out
    lan = _lan_url()                                  # 内网入口健康通才返回（顺手填 _HEALTH）
    if lan and rc.get("balance_lan", True):
        out.append((lan, "lan"))
    return out


def _lane_roles_cfg() -> dict:
    """config/pipeline/lane_roles.json（L-206 工位分卡单一来源）；读不到 = 关"""
    try:
        d = json.loads((ROOT / "config" / "pipeline" / "lane_roles.json").read_text(encoding="utf-8-sig"))
        return d if isinstance(d, dict) else {"enabled": False}
    except (OSError, ValueError):   # arch-ok: 配置缺 = 旧路由
        return {"enabled": False}


def _review_local_only(local_cands: list) -> bool:
    """L-206：lane_roles 开着、本机（OFFLINE_SELF）就是审图位主机（review.lane 的主机，gpu33）、且至少一个本机实例 /health 通
    → 只用本机候选（8870 分发器 → gpu33:3 上的 N 个实例）。本机全不在 → False（退回原路由：还能走 gpu36 内网入口，不让看图断掉）"""
    lr = _lane_roles_cfg()
    if not lr.get("enabled") or os.environ.get("VQA_REVIEW_FALLBACK") == "1":   # MINOR 3：producer 判审图位连续没结论 → 旧路由
        return False
    host = str((lr.get("review") or {}).get("lane") or "").split(":", 1)[0]
    if not host or os.environ.get("OFFLINE_SELF") != host:
        return False
    return any(_ready(u)[0] for u, _ in local_cands)


def _pick_balanced(cands: list[tuple[str, str]], wait_s: float) -> tuple[str | None, str, int, str]:
    """在候选实例里挑排队最少且 < queue_max 的（并列取靠前者 = 本机优先）；全忙就按 poll_s 轮询，最多等 min(busy_wait_s, wait_s)；
    都不在 / 等满 = (None, none, 排队数, 原因)，调用方记 untested，绝不判 fail"""
    rc = _rcfg()
    qmax = int(rc.get("queue_max", 6))
    t_end = time.monotonic() + min(float(rc.get("busy_wait_s", 600)), wait_s)
    while True:
        best, any_up, qlast = None, False, 0
        for u, route in cands:
            up, q = _ready(u)
            if not up:
                continue
            any_up, qlast = True, q
            if q < qmax and (best is None or q < best[2]):
                best = (u, route, q)
        if best:
            return best[0], best[1], best[2], ""
        if not any_up:
            return None, "none", 0, "服务不可用"
        if time.monotonic() >= t_end:
            return None, "none", qlast, "本机忙"
        time.sleep(max(0.0, min(float(rc.get("poll_s", 10)), t_end - time.monotonic())))


def _pick(url: str, wait_s: float) -> tuple[str | None, str, int, str]:
    """W23 路由 → (url, route, 排队数, 未测原因)。本机 8870 在且已加载 → 只用本机（忙就等，不切隧道）；
    本机不在 / 未加载 → 8871 隧道（tools/vqa/tunnel.ps1 → gpu36:8870）；服务器上（OFFLINE_SELF / VQA_LAN=1）→ model.lan_url；都不在 → 未测"""
    rc = _rcfg()
    if os.environ.get("VQA_REVIEW_ONLY") == "1" and os.environ.get("VQA_REVIEW_FALLBACK") != "1":
        # L-206：看图只去审图位（lane_roles.review 的内网入口）——本机 8870（视频卡上的看图）/ 8871 隧道都不用；不通 = 未测，不判 fail
        # MINOR 3：VQA_REVIEW_FALLBACK=1（审图位连续没结论）→ 跳过只认审图位，走下面的旧路由
        lan = _lan_url()
        if not lan:
            return None, "none", 0, "审图位不可达"
        return _pick_balanced([(lan, "lan")], wait_s)
    cands = _balance_cands(url)
    if cands:
        return _pick_balanced(cands, wait_s)
    if rc.get("laptop_prefer_tunnel") and not os.environ.get("OFFLINE_SELF"):
        # PS-MASTER 11-06：笔记本自带看图（笔记本显卡，OOM、排队 6）把 ep02 素材验收堵了 6 小时；笔记本上优先走 8871 隧道（gpu33 3090），
        # 隧道不通或排满才回本机
        alt0 = url.rsplit(":", 1)[0] + ":" + str(int(url.rsplit(":", 1)[1].split("/")[0]) + 1)
        aup, aq = _ready(alt0)
        if aup and aq < int(rc.get("queue_max", 6)):
            return alt0, "tunnel", aq, ""
    up, q = _ready(url)
    if up:
        qmax = int(rc.get("queue_max", 6))
        if q < qmax:
            return url, "local", q, ""
        t_end = time.monotonic() + min(float(rc.get("busy_wait_s", 600)), wait_s)
        while time.monotonic() < t_end:              # 本机忙：排队等，不去挤隧道
            time.sleep(max(0.0, min(float(rc.get("poll_s", 10)), t_end - time.monotonic())))
            up, q = _ready(url)
            if not up:
                break
            if q < qmax:
                return url, "local", q, ""
        if up:
            return None, "none", q, "本机忙"
    alt = url.rsplit(":", 1)[0] + ":" + str(int(url.rsplit(":", 1)[1].split("/")[0]) + 1)
    up, q = _ready(alt)
    if up:
        return alt, "tunnel", q, ""
    lan = _lan_url()                                  # NETFIX 11-01（K-OPS-N03）：gpu33 常驻 producer 走 gpu36 内网入口
    if lan:
        return lan, "lan", 0, ""
    return None, "none", 0, "服务不可用"


def generate(req: dict, *, wait_s: float = 1800, refresh: bool = False, label: str = "", kind: str = "ask") -> dict:
    """req = rubric.build_*_request(...) → {text, seconds, gpu, cached, key} 或 {error}。
    W23：服务都不在 / 超时 / 本机忙等满 → {error, status: untested, reason}（调用方按 untested 处理，绝不判 fail）；kind = image | video | ask 定超时"""
    key = _key(req)
    CACHE.mkdir(parents=True, exist_ok=True)
    cf = CACHE / f"{key}.json"
    if cf.exists() and not refresh:
        return {**json.loads(cf.read_text(encoding="utf-8")), "cached": True, "key": key}
    body = {"system": req.get("system"), "text": req["text"], "key": key, "wait_s": wait_s, "label": label,
            "max_new_tokens": int((cfg().get("model") or {}).get("max_new_tokens", 900)),
            "images": [{k: v for k, v in im.items()} for im in req["images"]], "refresh": refresh}
    t0 = time.monotonic()
    if _rcfg().get("max_wait_s"):                      # B1：首帧检测的最长等待可配（默认不限，由调用方 wait_s 定）
        wait_s = min(wait_s, float(_rcfg()["max_wait_s"]))
    url, route, q, why = _pick(_url(), wait_s)
    if not url:
        _log_route("none", (time.monotonic() - t0) * 1000, False, kind, why)
        return _untested(why, f"排队 {q}" if why == "本机忙" else "8870 / 8871 都不通")
    if route != "local":
        _to_bytes(body)
    tmo = max(60.0, float((_rcfg().get("timeouts") or {}).get(kind, 240)))
    timeout = min(wait_s + 120, tmo * (q + 1))
    try:
        res = _post_local(url, body, timeout)
    except (TimeoutError, socket.timeout) as e:
        _log_route(route, (time.monotonic() - t0) * 1000, False, kind, "超时")
        return _untested("超时", f"{route} {int(timeout)} s：{e!r}"[:160])
    except (OSError, urllib.error.URLError, ValueError) as e:
        _log_route(route, (time.monotonic() - t0) * 1000, False, kind, "服务不可用")
        return _untested("服务不可用", f"{route}：{e!r}"[:160])
    ok = "error" not in res
    _log_route(route, (time.monotonic() - t0) * 1000, ok, kind, "" if ok else str(res.get("error"))[:80])
    if ok:
        res["key"] = key
        res["route"] = route
        cf.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    return res


def _finish(req: dict, res: dict, kind: str, subject: str) -> dict:
    if "error" in res:
        out = {"kind": kind, "subject": subject, "error": res["error"], "checks": {}, "defects": [], "unsure": []}
        if res.get("status") == "untested":
            out.update(status="untested", reason=res.get("reason"))
        return out
    v = rubric.parse(res.get("text", ""))
    v.update(kind=kind, subject=subject, model=res.get("model"), seconds=res.get("seconds"), key=res.get("key"),
             cached=res.get("cached"), version=cfg().get("version"), at=time.strftime("%Y-%m-%dT%H:%M:%S"),
             spec=req.get("spec"))
    if not v.get("ok_parse"):
        v["error"] = "看图模型输出不是 JSON"
    return v


def inspect_image(subject: str, prompt: str, references: list[str] | None = None, **kw) -> dict:
    """出图复核：→ 规范化判定（rubric.parse 的字段 + kind / subject / model / seconds / key）"""
    req = rubric.build_image_request(subject, prompt, references, extra_refs=kw.pop("extra_refs", None),
                                     spec=kw.pop("spec", None))
    return _finish(req, generate(req, **{"kind": "image", **kw}), "image", str(subject))


def sample_frames(video: str | Path, n: int | None = None, out_dir: Path | None = None) -> list[str]:
    """均匀抽 n 帧（含第 1 帧，覆盖开头定格 R11-8）。用 cv2，本机算"""
    import cv2
    n = n or int((cfg().get("model") or {}).get("video_frames", 6))
    video = Path(video)
    out_dir = out_dir or (ROOT / "data" / "vqa" / "frames" / video.stem)
    out_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(video))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    fps = cap.get(cv2.CAP_PROP_FPS) or 24
    idx = sorted({0, min(total - 1, int(fps))} | {int(i * (total - 1) / max(1, n - 1)) for i in range(n)})[:n + 1]
    out = []
    for i in idx:
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ok, fr = cap.read()
        if ok:
            p = out_dir / f"f{i:05d}.jpg"
            cv2.imwrite(str(p), fr, [cv2.IMWRITE_JPEG_QUALITY, 92])
            out.append(str(p))
    cap.release()
    return out


def inspect_video(video: str | Path, prompt: str, references: list[str] | None = None, **kw) -> dict:
    frames = sample_frames(video, kw.pop("n", None))
    req = rubric.build_video_request(frames, prompt, references)
    v = _finish(req, generate(req, **{"kind": "video", **kw}), "video", str(video))
    v["frames"] = frames
    return v
