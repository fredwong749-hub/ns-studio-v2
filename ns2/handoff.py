"""出图机 → 视频机的首 / 尾帧交接（P3 真机发现：两台 data 各一份，视频机的 takes.json 停在旧首帧，
smoke_v2 ep01-S2 视频用了 v1 旧首帧而不是出图机新出的 v14——旧系统靠笔记本同步，新系统没有这一步）。

视频步在编译 / 提交前调 sync_frames：以出图机为准，把它当前的首帧 / 尾帧 take（文件 + takes.json 条目）拉到本机，并把本机 active 指过去。
- 文件经出图机 worker 只读文件口拉（要密钥），先写临时名再改名
- takes.json 只经 mutate_takes 写（带锁、备份、op_id 幂等）；本机已有同 n 但文件不同 → 冲突，抛 HandoffConflict（视频步记 blocked，不覆盖）
- 出图机没有当前尾帧（不用尾帧的镜）→ 本机 active 里也去掉 last
"""
from __future__ import annotations

import json
import uuid
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from ns2 import auth as AU

FILE_PORTS = {"gpu33": "http://10.0.0.33:8901", "gpu36": "http://10.0.0.36:8901"}


class HandoffConflict(RuntimeError):
    pass


def _get(base: str, rel: str, timeout: float = 120) -> bytes:
    req = urllib.request.Request(f"{base}/files/{urllib.parse.quote(rel)}", headers=AU.headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def remote_size(base: str, rel: str) -> int | None:
    """出图机上这个文件多大（Range 0-0 取 Content-Range 总长）；问不到 → None（不挡）"""
    req = urllib.request.Request(f"{base}/files/{urllib.parse.quote(rel)}", headers={**AU.headers(), "Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            cr = r.headers.get("Content-Range") or ""
            return int(cr.rsplit("/", 1)[1]) if "/" in cr else int(r.headers.get("Content-Length") or 0) or None
    except (OSError, ValueError):
        return None


def remote_active(run: str, sid: str, src_host: str, base: str | None = None) -> dict:
    """审查 3 §4 stale-frame：提交视频前再问一次出图机当前首 / 尾帧版本号 → {"image": n, "last": n}"""
    ep, s = sid.split("-")
    remote = json.loads(_get(base or FILE_PORTS[src_host], f"renders/{run}/{ep}/{s}/takes.json"))
    act = remote.get("active") or {}
    return {"image": act.get("image"), "last": act.get("last")}


def sync_frames(run: str, sid: str, src_host: str, root: Path, base: str | None = None) -> dict:
    """→ {"image": n | None, "last": n | None, "pulled": [文件], "changed": bool}"""
    for p in ("services/render_router", "services/agent", "packages/schema"):
        if str(root / p) not in sys.path:
            sys.path.insert(0, str(root / p))
    from ns_render import pipeline as P
    base = base or FILE_PORTS[src_host]
    ep, s = sid.split("-")
    remote = json.loads(_get(base, f"renders/{run}/{ep}/{s}/takes.json"))
    ract = remote.get("active") or {}
    want = {}
    for stage in ("image", "last"):
        n = ract.get(stage)
        t = next((x for x in remote.get("takes") or [] if x.get("stage") == stage and x.get("n") == n), None) if n is not None else None
        want[stage] = t
    pulled = []
    for t in want.values():
        if t and t.get("file"):
            dst = root / "data" / t["file"]
            if dst.is_file():                        # 审查 3 §4 N3：本机已有同名文件 → 核大小，对不上 = 冲突（不拿错图出片）
                rs = remote_size(base, t["file"])
                if rs is not None and rs != dst.stat().st_size:
                    raise HandoffConflict(f"{sid} {t.get('stage')} v{t.get('n')}：本机 {t['file']} {dst.stat().st_size} 字节，出图机 {rs} 字节（同名不同图，要人工对齐）")
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst.with_suffix(dst.suffix + ".part")
                tmp.write_bytes(_get(base, t["file"]))
                tmp.replace(dst)
                pulled.append(t["file"])
    out = {"image": (want["image"] or {}).get("n"), "last": (want["last"] or {}).get("n"), "pulled": pulled, "changed": False}

    def fn(doc):
        takes = doc.setdefault("takes", [])
        act = doc.setdefault("active", {})
        for stage, t in want.items():
            if t is None:                            # 审查 3 §4 N2：出图机没有当前版（首帧也算）→ 本机也去掉，不留旧帧
                if stage in act:
                    act.pop(stage); out["changed"] = True
                continue
            mine = next((x for x in takes if x.get("stage") == stage and x.get("n") == t["n"]), None)
            if mine is None:
                takes.append({**t, "synced_from": src_host})
                out["changed"] = True
            elif mine.get("file") != t.get("file"):
                raise HandoffConflict(f"{sid} {stage} v{t['n']}：本机同号是 {mine.get('file')}，出图机是 {t.get('file')}（编号分叉，要人工对齐）")
            if act.get(stage) != t["n"]:
                act[stage] = t["n"]; out["changed"] = True
        # P6 11-09（两台分着出同一集视频）：尽力版标记 / 尾帧 I2VA 兜底也要跟着过来，否则视频机按“没过契约”判、编 0 个变体
        rbe, mbe = remote.get("best_effort") or {}, doc.get("best_effort") or {}
        for stage, t in want.items():
            mark = rbe.get(stage)
            if t is not None and isinstance(mark, dict) and mark.get("n") == t["n"]:
                if mbe.get(stage) != mark:
                    doc.setdefault("best_effort", {})[stage] = mark; out["changed"] = True
            elif stage in mbe:
                doc["best_effort"].pop(stage); out["changed"] = True
        fb = remote.get("last_frame_fallback")
        if fb != doc.get("last_frame_fallback"):
            if fb is None:
                doc.pop("last_frame_fallback", None)
            else:
                doc["last_frame_fallback"] = fb
            out["changed"] = True
        return doc

    op = f"ns2-handoff-{src_host}-{out['image']}-{out['last']}-{uuid.uuid4().hex[:8]}"   # 审查 3 §4 N4：14→13→14 回摆不被重放保护吞掉
    P.mutate_takes(sid, fn, op="ns2_handoff", by="ns2-worker", op_id=op, run=run)
    return out
