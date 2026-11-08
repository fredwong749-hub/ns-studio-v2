"""M9（审查）：models.lock 校验——只读、只报告，不拦渲染（拦不拦由项目方定）。

    python -m ns2.models_check --lock models.lock --loras <本机 ComfyUI models/loras 目录> --host gpu33 > out.json
    python -m ns2.models_check --merge gpu33=a.json,gpu36=b.json --out reports/models_lock_check.md

每台：锁表里每个 LoRA → 本机有没有、大小、sha256（现算）、与锁表 sha256 是否一致；锁表 sha256 为 null 的列为“未校验”；
同名不同 sha256 的锁表条目列为“重复登记”；本机目录里有、锁表里没有的列为“未登记”。不改锁表、不改模型文件。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while b := f.read(1 << 22):
            h.update(b)
    return h.hexdigest()


def check(lock: dict, lora_dir: Path, host: str) -> dict:
    entries = list(lock.get("loras") or []) + [{**e, "kind": "download"} for e in lock.get("downloads") or [] if isinstance(e, dict)]   # 下载清单也核（purelens 重复登记在这里）
    by_file = defaultdict(set)
    for e in entries:
        by_file[e.get("file")].add(e.get("sha256"))
    rows = []
    for e in entries:
        f = lora_dir / str(e.get("file") or "")
        r = {"file": e.get("file"), "kind": e.get("kind"), "lock_sha": e.get("sha256"), "present": f.is_file()}
        if r["present"]:
            r["size"] = f.stat().st_size
            r["sha"] = sha256(f)
            r["match"] = None if not e.get("sha256") else r["sha"] == e["sha256"]
        rows.append(r)
    listed = {e.get("file") for e in entries}
    extra = sorted(p.name for p in lora_dir.glob("*.safetensors") if p.name not in listed) if lora_dir.is_dir() else []
    return {"host": host, "lora_dir": str(lora_dir), "rows": rows, "unlisted": extra,
            "dup_names": {k: sorted(map(str, v)) for k, v in by_file.items() if len(v) > 1}}


def merge(parts: dict) -> str:
    L = ["# models.lock 校验（M9，只读）", "",
         "| LoRA | 锁表 sha256 | " + " | ".join(parts) + " |", "|---|---|" + "---|" * len(parts)]
    keys = []                                  # 按（文件, 锁表 sha）分行：同名重复登记的每条都单独核
    for p in parts.values():
        for r in p["rows"]:
            if (r["file"], r["lock_sha"]) not in keys:
                keys.append((r["file"], r["lock_sha"]))
    issues = {"未校验（锁表 sha256 = null）": set(), "不一致": [], "缺文件": []}
    for f, lock_sha in keys:
        cells = []
        for h, p in parts.items():
            r = next((x for x in p["rows"] if x["file"] == f and x["lock_sha"] == lock_sha), None)
            if not r or not r["present"]:
                cells.append("缺"); issues["缺文件"].append(f"{h}:{f}")
            else:
                ok = {True: "✓ 一致", False: "✗ 不一致", None: "未校验"}[r["match"]]
                cells.append(f"{ok} `{r['sha'][:12]}`")
                if r["match"] is False:
                    issues["不一致"].append(f"{h}:{f}")
            if r and not r["lock_sha"]:
                issues["未校验（锁表 sha256 = null）"].add(f)
        L.append(f"| {f} | {('`' + lock_sha[:12] + '`') if lock_sha else 'null'} | " + " | ".join(cells) + " |")
    L += ["", "## 结论", ""]
    for k, v in issues.items():
        L.append(f"- {k}：{len(v)}" + (f"（{', '.join(sorted(v))}）" if v else ""))
    for h, p in parts.items():
        if p["dup_names"]:
            L.append(f"- 重复登记（同名不同 sha256，{h} 读到的锁表）：" + "；".join(f"{k} → {v}" for k, v in p["dup_names"].items()))
        if p["unlisted"]:
            L.append(f"- {h} 目录里有、锁表里没有：" + ", ".join(p["unlisted"]))
    L += ["", "只报告、不拦渲染；是否在渲染前强制校验由项目方定。"]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lock"); ap.add_argument("--loras"); ap.add_argument("--host", default="")
    ap.add_argument("--merge"); ap.add_argument("--out")
    a = ap.parse_args(argv)
    if a.merge:
        parts = {h: json.loads(Path(p).read_text(encoding="utf-8")) for h, p in (x.split("=", 1) for x in a.merge.split(","))}
        md = merge(parts)
        Path(a.out).write_text(md, encoding="utf-8") if a.out else print(md)
        return 0
    print(json.dumps(check(json.loads(Path(a.lock).read_text(encoding="utf-8")), Path(a.loras), a.host), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
