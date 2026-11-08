"""
LoRA 总表 + registry 扩展（docs/ux_round5/B_revision.md §7 P1「LoRA」、A_assets_ia.md B9；UX6 实现）。

现状：出图身份 LoRA 在 data/renders_gpu3/LORA_REGISTRY.json（gpu3 维护，系统不改它）；视频身份 LoRA（lin_wan_h3_v1）
只在 comfy/models.lock 里（status=training、sha256=null），不在 registry，换 step 无感（B G5）；训练数据
training/*/dataset_manifest.json 记了 source_sha256，但没人拿它和当前定妆比（B G6）。

这里做两件事：
1. registry 扩展文件 data/assets/lora_registry_ext.json（系统自己的文件）：每条 = 一个 LoRA 文件的一个版本
   {lora_id, file, kind: image|video, character, step, sha256, from_identity_rev, dataset_manifest, dataset_sha,
    status: candidate|accepted|void, by, at, owner_quote, location, note}
   · 新登记一律 candidate；改成 accepted / void 必须项目方本人 + 原话（GateError）。
   · from_identity_rev 不填 = 登记时该人物的定妆 rev（asset_revs.identity_rev）。
   · loraslot:<slug>:video 的 rev 取自这里 character=slug、kind=video、status=accepted 的条目（+ 视频身份总开关）。
2. table()：只读合并总表——gpu3 registry、models.lock、auto_lora_triggers（出图指针）、render_quality（附加视频 LoRA）、
   衣橱单品 lora_file、剧本 globals.lora_triggers、扩展文件；每行带来源、阶段、所属、状态、sha、
   “训练数据来自旧定妆”（数据集里母版的 source_sha256 ≠ 当前母版 sha）。

不改 gpu3 registry / models.lock / config；不做内容检测。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path

from . import asset_revs as AR
from .bible import project_root

EXT = Path("data") / "assets" / "lora_registry_ext.json"
STATUSES = ("candidate", "accepted", "void")
_lock = threading.RLock()


class GateError(Exception):
    pass


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def read_ext(root: str | Path | None = None) -> dict:
    d = AR._json(project_root(root) / EXT)
    if not isinstance(d, dict) or not isinstance(d.get("loras"), list):
        return {"schema": "nsfs.lora_registry_ext/v1", "loras": []}
    return d


def _write_ext(root: Path, d: dict) -> None:
    p = root / EXT
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def video_enabled(root: Path) -> bool:
    q = AR._quality(root)
    for sect in (q.get("prompt_guard") or {}, q):
        if isinstance(sect, dict) and "video_identity_enabled" in sect and not isinstance(sect["video_identity_enabled"], str):
            return bool(sect["video_identity_enabled"])
    return False


def video_slot_entries(root: str | Path | None, slug: str) -> list[dict]:
    root = project_root(root)
    return [{k: e.get(k) for k in ("file", "sha256", "step", "strength", "status")}
            for e in read_ext(root)["loras"]
            if e.get("character") == slug and e.get("kind") == "video" and e.get("status") == "accepted"]


def loraslot_video_rev(root: str | Path | None, slug: str) -> str | None:
    rows = video_slot_entries(root, slug)
    if not rows:
        return None
    return AR._h({"enabled": video_enabled(project_root(root)), "loras": sorted(rows, key=lambda r: str(r.get("file")))})


def _file_sha(p: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def register(entry: dict, root: str | Path | None = None) -> dict:
    """新登记一个 LoRA 版本（candidate）。必填 file、kind；sha256 不给且 local_path 指向本机文件 → 现算"""
    root = project_root(root)
    file = str(entry.get("file") or "").strip()
    kind = entry.get("kind") or "image"
    if not file or not re.fullmatch(r"[\w.\-]+\.safetensors", file):
        raise ValueError("file 要是 LoRA 文件名（*.safetensors）")
    if kind not in ("image", "video"):
        raise ValueError("kind 只能是 image | video")
    if entry.get("status") not in (None, "candidate"):
        raise GateError("新登记只能是 candidate；改 accepted / void 用 status 接口（项目方本人 + 原话）")
    slug = entry.get("character")
    sha = entry.get("sha256")
    lp = entry.get("local_path")
    if not sha and lp:
        p = (root / lp).resolve()
        if p.is_file() and p.is_relative_to(root.resolve()):
            sha = _file_sha(p)
    ds = entry.get("dataset_manifest")
    ds_sha = None
    if ds:
        dp = (root / ds).resolve()
        if dp.is_file() and dp.is_relative_to(root.resolve()):
            ds_sha = AR._sha(dp)
    row = {"lora_id": entry.get("lora_id") or Path(file).stem + (f"_step{entry['step']}" if entry.get("step") else ""),
           "file": file, "kind": kind, "character": slug, "step": entry.get("step"), "sha256": sha,
           "from_identity_rev": entry.get("from_identity_rev") or (AR.identity_rev(root, slug) if slug else None),
           "dataset_manifest": ds, "dataset_sha": ds_sha, "trigger": entry.get("trigger"),
           "strength": entry.get("strength"), "location": entry.get("location"), "status": "candidate",
           "by": entry.get("by") or "system", "at": _now(), "note": entry.get("note")}
    with _lock:
        d = read_ext(root)
        if any(e.get("lora_id") == row["lora_id"] for e in d["loras"]):
            raise ValueError(f"已经登记过：{row['lora_id']}")
        d["loras"].append(row)
        _write_ext(root, d)
    return row


def set_status(lora_id: str, status: str, by: str | None, owner_quote: str | None,
               root: str | Path | None = None) -> dict:
    if status not in STATUSES:
        raise ValueError(f"status 只能是 {STATUSES}")
    if status in ("accepted", "void") and (by != "owner" or not (owner_quote or "").strip()):
        raise GateError("LoRA 设为采用 / 作废属于项目方闸门：需要 by=owner 和原话 owner_quote")
    root = project_root(root)
    with _lock:
        d = read_ext(root)
        e = next((x for x in d["loras"] if x.get("lora_id") == lora_id), None)
        if not e:
            raise KeyError(lora_id)
        e.setdefault("history", []).append({"from": e.get("status"), "to": status, "by": by,
                                            "owner_quote": owner_quote, "at": _now()})
        e["status"] = status
        _write_ext(root, d)
    return e


# ------------------------------------------------------------------ 只读总表

def _datasets(root: Path) -> list[dict]:
    out = []
    for m in sorted((root / "training").glob("*/dataset_manifest.json")):
        d = AR._json(m)
        if not isinstance(d, dict):
            continue
        imgs = [i for i in d.get("images") or [] if isinstance(i, dict)]
        masters = {i.get("source_sha256") for i in imgs if str(i.get("source") or "").endswith("plates/master.png")}
        out.append({"dir": m.parent.name, "manifest": m.relative_to(root).as_posix(), "trigger": d.get("trigger"),
                    "count": d.get("count") or len(imgs), "dataset_sha": AR._sha(m),
                    "master_shas": sorted(x for x in masters if x)})
    return out


def table(root: str | Path | None = None) -> dict:
    root = project_root(root)
    rows: dict[str, dict] = {}

    def row(file: str) -> dict:
        return rows.setdefault(file, {"file": file, "kind": None, "owner": None, "owner_type": None, "status": None,
                                      "sha256": None, "triggers": [], "sources": [], "strength": None,
                                      "versions": [], "license": None, "location": None, "notes": []})

    reg = AR._json(root / "data" / "renders_gpu3" / "LORA_REGISTRY.json")
    for e in (reg or {}).get("loras") or [] if isinstance(reg, dict) else []:
        if not isinstance(e, dict) or not e.get("file"):
            continue
        r = row(str(e["file"]))
        r["sources"].append("gpu3 LORA_REGISTRY")
        r["kind"] = r["kind"] or e.get("kind")
        r["status"] = e.get("status") or r["status"]
        r["sha256"] = e.get("sha256") or r["sha256"]
        r["triggers"] += [t for t in e.get("triggers") or [] if t not in r["triggers"]]
        r["strength"] = e.get("strength", r["strength"])
        if e.get("accepted_ref"):
            r["notes"].append(f"采用依据：{e['accepted_ref']}")
    lock = AR._json(root / "comfy" / "models.lock")
    for e in (lock or {}).get("loras") or [] if isinstance(lock, dict) else []:
        if not isinstance(e, dict) or not e.get("file"):
            continue
        r = row(str(e["file"]))
        r["sources"].append("comfy/models.lock")
        k = str(e.get("kind") or "")
        r["kind"] = r["kind"] or ("video" if k.startswith("video") else "image")
        r["status"] = r["status"] or e.get("status") or "listed"
        r["sha256"] = r["sha256"] or e.get("sha256")
        r["location"] = r["location"] or e.get("location")
        for t in e.get("triggers") or []:
            tt = t.get("trigger") if isinstance(t, dict) else t
            if tt and tt not in r["triggers"]:
                r["triggers"].append(tt)
        if e.get("status_note"):
            r["notes"].append(str(e["status_note"]))
    cfg = AR._json(root / "config" / "auto_lora_triggers.json")
    name2slug = {v: k for k, v in AR.CHARACTERS.items()}
    for name, es in ((cfg or {}).get("characters") or {}).items() if isinstance(cfg, dict) else []:
        for e in es or []:
            if isinstance(e, dict) and e.get("file"):
                r = row(str(e["file"]))
                r["sources"].append("config/auto_lora_triggers.json（出图指针）")
                r["owner"], r["owner_type"] = name, "character"
                r["slot"] = f"loraslot:{name2slug.get(name, name)}:{e.get('kind') or 'image'}"
                r["kind"] = r["kind"] or e.get("kind") or "image"
                r["strength"] = e.get("strength", r["strength"])
                if e.get("trigger") and e["trigger"] not in r["triggers"]:
                    r["triggers"].append(e["trigger"])
    for e in AR._quality(root).get("optional_video_loras") or []:
        if isinstance(e, dict) and e.get("name"):
            r = row(str(e["name"]))
            r["sources"].append("render_quality 附加视频 LoRA")
            r["kind"], r["owner_type"] = "video", r["owner_type"] or "style"
            r["owner"] = r["owner"] or e.get("label")
            r["sha256"] = r["sha256"] or e.get("sha256")
            r["license"] = e.get("license") or r["license"]
    from . import run_paths as RP
    wd = AR._json(RP.looks_dir(root) / "wardrobe.json")       # I-14
    for it in (wd or {}).get("items") or [] if isinstance(wd, dict) else []:
        if isinstance(it, dict) and it.get("lora_file"):
            r = row(str(it["lora_file"]))
            if "衣橱单品" not in r["sources"]:
                r["sources"].append("衣橱单品")
            r["owner_type"] = r["owner_type"] or "prop"
            r.setdefault("items", []).append({"id": it.get("id"), "name": it.get("name"), "character": it.get("character")})
            if it.get("trigger") and it["trigger"] not in r["triggers"]:
                r["triggers"].append(it["trigger"])
    from . import run_paths as RP
    sc = AR._json(RP.script_json(root))                       # I-14
    for e in ((sc or {}).get("globals") or {}).get("lora_triggers") or [] if isinstance(sc, dict) else []:
        if isinstance(e, dict) and e.get("file"):
            r = row(str(e["file"]))
            if "剧本 globals.lora_triggers" not in r["sources"]:
                r["sources"].append("剧本 globals.lora_triggers")
            r["kind"] = r["kind"] or e.get("kind")
            if e.get("trigger") and e["trigger"] not in r["triggers"]:
                r["triggers"].append(e["trigger"])
    ds = _datasets(root)
    cur_master = {s: AR._sha(root / "assets" / "characters" / s / "plates" / "master.png") for s in AR._slugs(root)}
    for e in read_ext(root)["loras"]:
        r = row(str(e.get("file")))
        if "登记扩展" not in r["sources"]:
            r["sources"].append("登记扩展")
        r["kind"] = r["kind"] or e.get("kind")
        if e.get("character"):
            r["owner"], r["owner_type"] = AR.char_name(e["character"]), "character"
        cur_id = AR.identity_rev(root, e["character"]) if e.get("character") else None
        r["versions"].append({**e, "identity_outdated": bool(e.get("from_identity_rev") and cur_id
                                                             and e["from_identity_rev"] != cur_id)})
        if e.get("status") == "accepted":
            r["status"], r["sha256"] = "accepted", e.get("sha256") or r["sha256"]
    out = []
    for r in rows.values():
        r["kind"] = "video" if str(r["kind"] or "").startswith("video") else "image"
        r["stage"] = "视频" if r["kind"] == "video" else "出图"
        r["sha_missing"] = not r["sha256"]
        out.append(r)
    out.sort(key=lambda r: (r["owner_type"] != "character", r["kind"], r["file"]))
    for d in ds:
        d["identity_outdated"] = {s: bool(d["master_shas"] and sha and sha not in d["master_shas"])
                                  for s, sha in cur_master.items() if sha and d["master_shas"]}
    return {"loras": out, "datasets": ds, "video_identity_enabled": video_enabled(root),
            "note": "只读合并视图；gpu3 registry / models.lock / config 不由系统改，登记扩展写 data/assets/lora_registry_ext.json"}
