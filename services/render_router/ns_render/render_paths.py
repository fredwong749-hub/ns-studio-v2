"""W33（C-14 / L-126）：版本文件相对路径的唯一出处。

旧写法 `f"renders/{shot_dir.relative_to(RENDERS)}"` 在 RENDERS = data/renders/<run>/ 时丢了剧名前缀，
写盘 DATA / rel 实际落进 V1 的 data/renders/epNN/SNN/takes/（与 V1 同目录、同名覆盖风险）。

- 写入：shot_rel_dir(d) / take_rel_dir(d) / take_rel(run, ep, shot, name) 一律相对 data/ 取路径：
  V2 = renders/<run>/epNN/SNN/takes/<file>；V1（runs.json legacy_unprefixed_run）= renders/epNN/SNN/takes/<file>（旧路径不变）。
- 读取：resolve_rel(rel) 新旧两种 rel 都能解析（旧数据不迁移也能读；迁移后旧 rel 也能找到新位置）。
"""
from __future__ import annotations

import re
from pathlib import Path

_EP_RE = re.compile(r"^renders/(ep\d+|_h3_keyframes)/")


def _P():
    from . import pipeline as P
    return P


def shot_rel_dir(d: Path, data: Path | None = None, renders: Path | None = None) -> str:
    """镜目录 → 相对 data/ 的路径（带剧名前缀，legacy 剧自然不带）。
    d 不在 data/ 下（测试把 NS_RENDERS_DIR 指到别处）→ 旧口径 renders/<相对 RENDERS>（行为不变）"""
    P = None
    if data is None or renders is None:
        P = _P()
    data = Path(data if data is not None else P.DATA)
    try:
        return Path(d).resolve().relative_to(data.resolve()).as_posix()
    except ValueError:
        renders = Path(renders if renders is not None else (P or _P()).RENDERS)
        return f"renders/{Path(d).resolve().relative_to(renders.resolve()).as_posix()}"


def take_rel_dir(d: Path, data: Path | None = None, renders: Path | None = None) -> str:
    """镜目录 → 版本目录相对路径 renders/<run>/epNN/SNN/takes"""
    return f"{shot_rel_dir(d, data, renders)}/takes"


def shot_take_rel_dir(shot_id: str) -> str:
    """本进程当前剧（pipeline.RENDERS）的镜号 → 版本目录相对路径"""
    P = _P()
    return take_rel_dir(P.shot_dir(shot_id), P.DATA, P.RENDERS)


def run_prefix(run: str | None, root: Path | None = None) -> str:
    """剧 → 相对 data/ 的渲染前缀：legacy（V1）= "renders"；其它 = "renders/<run>"；run 不给 = 本进程 RENDERS"""
    P = _P()
    if not run:
        return shot_rel_dir(P.RENDERS, P.DATA, P.RENDERS).rstrip("/")
    root = Path(root) if root else P.ROOT
    from ns_agent import run_paths as RPA
    m = RPA.runs_module(root)
    if m is not None and (root / "config" / "pipeline" / "runs.json").is_file() and m.is_legacy(run, root):
        return "renders"
    return f"renders/{run}"


def take_rel(run: str | None, ep: str, shot: str, name: str, root: Path | None = None) -> str:
    """renders/<run>/epNN/SNN/takes/<name>（legacy 剧 = renders/epNN/SNN/takes/<name>）"""
    return f"{run_prefix(run, root)}/{ep}/{shot}/takes/{name}"


def is_unprefixed(rel: str) -> bool:
    """rel 是不是旧的不带剧名口径 renders/epNN/…"""
    r = str(rel or "").replace("\\", "/").lstrip("./")
    r = r[5:] if r.startswith("data/") else r
    return bool(_EP_RE.match(r))


def resolve_rel(rel: str, data: Path | None = None, run: str | None = None, root: Path | None = None) -> Path:
    """按 rel 找文件（rel 相对 data/，也容忍带 data/ 前缀）：
    1) data/rel 存在 → 它（新路径，或还没迁移的旧数据——W33 前 V2 版本的 rel 不带剧名，文件也确实在那里）；
    2) 旧口径 renders/epNN/… 不在 且 本剧不是 legacy → 试 renders/<run>/epNN/…（--apply 迁移后的位置）。
    反方向（新 rel 找不到就退回 V1 旧目录）故意不做：那会把 V1 同名文件当成 V2 的（C-14）。
    都没有 → 返回 data/rel（调用方照旧判 is_file）"""
    if data is None:
        data = _P().DATA
    data = Path(data)
    r = str(rel or "").replace("\\", "/").lstrip("/")
    r = r[5:] if r.startswith("data/") else r
    p = data / r
    if p.exists() or not is_unprefixed(r):
        return p
    pre = run_prefix(run, root)
    if pre != "renders":
        q = data / pre / r[len("renders/"):]
        if q.exists():
            return q
    return p
