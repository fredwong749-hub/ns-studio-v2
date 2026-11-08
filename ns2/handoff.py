"""出图机 → 视频机的首 / 尾帧交接（P3 真机发现：两台 data 各一份，视频机的 takes.json 停在旧首帧，
smoke_v2 ep01-S2 视频用了 v1 旧首帧而不是出图机新出的 v14——旧系统靠笔记本同步，新系统没有这一步）。

视频步在编译 / 提交前调 sync_frames：以出图机为准，把它当前的首帧 / 尾帧 take（文件 + takes.json 条目）拉到本机，并把本机 active 指过去。
- 文件经出图机 worker 只读文件口拉（要密钥），先写临时名再改名
- takes.json 只经 mutate_takes 写（带锁、备份、op_id 幂等）；本机已有同 n 但文件不同 → 冲突，抛 HandoffConflict（视频步记 blocked，不覆盖）
- 出图机没有当前尾帧（不用尾帧的镜）→ 本机 active 里也去掉 last
"""
from __future__ import annotations

import json
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
            if not dst.is_file():
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
            if t is None:
                if stage == "last" and "last" in act:
                    act.pop("last"); out["changed"] = True
                continue
            mine = next((x for x in takes if x.get("stage") == stage and x.get("n") == t["n"]), None)
            if mine is None:
                takes.append({**t, "synced_from": src_host})
                out["changed"] = True
            elif mine.get("file") != t.get("file"):
                raise HandoffConflict(f"{sid} {stage} v{t['n']}：本机同号是 {mine.get('file')}，出图机是 {t.get('file')}（编号分叉，要人工对齐）")
            if act.get(stage) != t["n"]:
                act[stage] = t["n"]; out["changed"] = True
        return doc

    op = f"ns2-handoff-{src_host}-{out['image']}-{out['last']}"
    P.mutate_takes(sid, fn, op="ns2_handoff", by="ns2-worker", op_id=op, run=run)
    return out
