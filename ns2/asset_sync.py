"""出图两台分担（R4，NS2_SPLIT_IMAGES）：资产包（assets_v2/manifest.json + 清单里的文件）从另一台补齐。

两台各自生成资产（身份脸 / 设定图 / 单品 / 场景底图），清单已经分叉：11-11 实测 gpu33 多 2 个场景（sk_night_cab、sk_show_backstage）
和两个角色的造型 B 单品，gpu36 多一项造型 B sheet；两边**同键不同值 0 处**。所以合并 = 并集：
- 只有对方有的键 → 加进本机清单，引用的文件从对方 worker 文件口（/assets/，要密钥、只读、同一扩展名白名单）拉下来，验 sha256；
- 两边都有且值相同 → 不动；两边都有但值不同 → **不覆盖本机**，记 conflicts（报出来，人工 / 下一轮处理）；
- 本机清单先备份成 manifest.json.bak_ns2sync_<时间> 再原子写。
参考板 / 裁片（data/pipeline/runs/<run>/images/refboards…）由资产文件按内容哈希派生，两台各自算即可，不同步。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

from ns2 import auth as AU

FILE_PORTS = {"gpu33": "http://10.0.0.33:8901", "gpu36": "http://10.0.0.36:8901"}
PREFIX = "assets_v2/"


def merge(local, remote, path: str = "", conflicts: list | None = None):
    """并集合并（见模块说明）→ (合并结果, 新增键路径列表, 冲突列表)"""
    conflicts = [] if conflicts is None else conflicts
    added: list[str] = []
    if isinstance(local, dict) and isinstance(remote, dict):
        out = dict(local)
        for k, v in remote.items():
            p = f"{path}/{k}"
            if k not in local:
                out[k] = v
                added.append(p)
            else:
                out[k], a, _ = merge(local[k], v, p, conflicts)
                added += a
        return out, added, conflicts
    if json.dumps(local, sort_keys=True, ensure_ascii=False) != json.dumps(remote, sort_keys=True, ensure_ascii=False):
        if path.split("/")[-1] not in ("updated_at", "created_at"):
            conflicts.append(path)
    return local, added, conflicts


def file_refs(m) -> list[dict]:
    """清单里所有 {file: assets_v2/..., sha256?} 引用"""
    out = []
    if isinstance(m, dict):
        f = m.get("file")
        if isinstance(f, str) and f.startswith(PREFIX):
            out.append({"file": f, "sha256": m.get("sha256")})
        for v in m.values():
            out += file_refs(v)
    elif isinstance(m, list):
        for v in m:
            out += file_refs(v)
    return out


def _get(base: str, rel: str, timeout: float = 120) -> bytes:
    req = urllib.request.Request(f"{base}/assets/{urllib.parse.quote(rel)}", headers=AU.headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _write_atomic(p: Path, data: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, p)


def pull(root: Path, other: str, base: str | None = None, fetch=None, dry: bool = False) -> dict:
    """从另一台补齐本机资产包。root = 旧部署 repo 目录。→ {added, pulled, conflicts, bad_sha, changed}"""
    fetch = fetch or (lambda rel: _get(base or FILE_PORTS[other], rel))
    mf = root / PREFIX / "manifest.json"
    local = json.loads(mf.read_text(encoding="utf-8"))
    remote = json.loads(fetch("manifest.json"))
    merged, added, conflicts = merge(local, remote)
    pulled, bad = [], []
    for ref in file_refs(merged):
        f = root / ref["file"]
        if f.is_file():
            continue
        try:
            data = fetch(ref["file"][len(PREFIX):])
        except OSError as e:                 # 对方也没有这个文件（清单引用了但文件缺）→ 记下，不挡
            bad.append(f"{ref['file']}（拉不到：{e!r}）"[:200])
            continue
        if ref.get("sha256") and hashlib.sha256(data).hexdigest() != ref["sha256"]:
            bad.append(ref["file"])
            continue
        if not dry:
            _write_atomic(f, data)
        pulled.append(ref["file"])
    changed = bool(added)
    if changed and not dry:
        bak = mf.with_name(f"manifest.json.bak_ns2sync_{time.strftime('%Y%m%d-%H%M%S')}")
        bak.write_bytes(mf.read_bytes())
        if json.loads(mf.read_text(encoding="utf-8")) != local:          # 读和写之间本机清单被别的进程改了 → 不写，下次再同步
            return {"added": added, "pulled": pulled, "conflicts": conflicts, "bad_sha": bad, "changed": False, "retry": "本机清单刚被改过"}
        _write_atomic(mf, json.dumps(merged, ensure_ascii=False, indent=1).encode("utf-8"))
    return {"added": added, "pulled": pulled, "conflicts": conflicts, "bad_sha": bad, "changed": changed}


HUB = "looks/hub_episode_looks.json"      # data/ 下：资产步“排期”（角色 × 集 → 造型），出图机的资产步写；另一台出图前要有


def _get_file(base: str, rel: str, timeout: float = 60) -> bytes:
    req = urllib.request.Request(f"{base}/files/{urllib.parse.quote(rel)}", headers=AU.headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def pull_hub(root: Path, other: str, base: str | None = None, fetch=None, dry: bool = False) -> dict:
    """排期并集（ep08 实测：gpu36 资产步写了 lead_f / ch_li_an 第 8 集，gpu33 的同名文件没有 → 分到 gpu33 的镜 I3 全挡）。
    同键不同值不覆盖本机（_by / _at 不算冲突）"""
    fetch = fetch or (lambda rel: _get_file(base or FILE_PORTS[other], rel))
    f = root / "data" / HUB
    local = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
    remote = json.loads(fetch(HUB))
    merged, added, conflicts = merge(local, remote)
    added = [a for a in added if not a.endswith(("/_by", "/_at"))]
    conflicts = [c for c in conflicts if not c.endswith(("/_by", "/_at"))]
    if added and not dry:
        if f.is_file():
            f.with_name(f"{f.name}.bak_ns2sync_{time.strftime('%Y%m%d-%H%M%S')}").write_bytes(f.read_bytes())
        _write_atomic(f, json.dumps(merged, ensure_ascii=False, indent=1).encode("utf-8"))
    return {"added": added, "conflicts": conflicts, "changed": bool(added)}
