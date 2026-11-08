"""
资产二次修改 P1 写路径（docs/ux_round5/B_revision.md §2.2 / §2.6 / §7 P1；UX6 实现）。

    snapshot(key)            把某依赖键“当前内容”存到 data/assets/snapshots/<type>/<id>/<rev>/（manifest.json + 原地覆盖型文件副本）；
                             同一 rev 只存一次。observe() 每见到新 rev 就顺手存（被动观察也留得住旧内容）。
    set_current(key, rev)    移当前指针（含回滚）：
                               · 闸门类（定妆 / 身份参考、身份 LoRA 指针、音色）必须 by=owner 且带项目方原话 owner_quote，否则 GateError(403)；
                               · 原地覆盖前先给“当前内容”自动快照（快照写不成就不覆盖）；
                               · 文件型部分（assets/ 下的母版 plates/master.png、face_reference 指向的脸图）直接从快照恢复；
                               · 配置型部分（config/*.json：LoRA 指针、预设、视频基底、附加视频 LoRA、音色、face_reference 指针，
                                 以及 data/assets/asset_refs.json 的身份参考启用状态）**不直接写**，写一份提议到 data/assets/proposals/，
                                 由项目方 / 总调度按配置写入规则落地；
                               · 结果记进账本 revisions.jsonl（by / owner_quote / source / snapshot / rollback）和动作日志 revision_actions.jsonl。
    keep(...)                “保留旧版”豁免（kept）：项目方原话必填，记 data/assets/dep_waivers.jsonl；只对豁免时那一个 rev 生效，
                             依赖再变一次会重新提示。
    write_proposal(...)      统一的提议文件（音色修改等也走这里）。

不删任何文件、不改 config/、不做内容检测。
"""
from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from pathlib import Path

from . import asset_revs as AR
from .bible import project_root

SNAP_DIR = Path("data") / "assets" / "snapshots"
PROPOSALS = Path("data") / "assets" / "proposals"
WAIVERS = Path("data") / "assets" / "dep_waivers.jsonl"
ACTIONS = Path("data") / "assets" / "revision_actions.jsonl"
# 闸门类：移指针 / 回滚必须项目方本人 + 原话（B §3 场景汇总表：定妆确认、换身份 LoRA 指针、换音色）
GATED = {"identity": "定妆 / 身份参考（定妆确认）", "loraslot": "身份 LoRA 指针", "voice": "音色"}
CONFIG_TYPES = {"loraslot", "preset", "vbase", "vlora", "voice"}


class GateError(Exception):
    """闸门类改动缺项目方本人 / 原话 → API 回 403"""


class RevNotFound(Exception):
    """没有这个键 / 这一版没有快照 → API 回 404 / 409"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _safe(s: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", s).strip("_") or "_"


def _append(p: Path, row: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _jsonl(p: Path) -> list[dict]:
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def is_gated(key: str) -> bool:
    return key.partition(":")[0] in GATED


def check_gate(key: str, by: str | None, owner_quote: str | None) -> None:
    typ = key.partition(":")[0]
    if typ in GATED and (by != "owner" or not (owner_quote or "").strip()):
        raise GateError(f"{GATED[typ]}属于项目方闸门：需要项目方本人（by=owner）并附原话 owner_quote")


# ------------------------------------------------------------------ 每类键的“内容”（快照用）

def _fragment(key: str, root: Path) -> dict | None:
    """键 → {config, path, value}（配置型）或 {files:{name: 相对路径}, parts}（文件型）；读不到 → None"""
    typ, _, rest = key.partition(":")
    if typ == "identity":
        slug = rest
        files: dict[str, str] = {}
        master = Path("assets") / "characters" / slug / "plates" / "master.png"
        if (root / master).is_file():
            files["master"] = master.as_posix()
        fr = AR._json(root / "config" / "face_reference.json")
        ref = (fr or {}).get("reference") if isinstance(fr, dict) else None
        face_ptr = None
        if isinstance(ref, str) and f"/{slug}/" in "/" + ref.replace("\\", "/"):
            face_ptr = ref
            if (root / ref).is_file() and not ref.replace("\\", "/").startswith("config/"):
                files["face_reference"] = ref.replace("\\", "/")
        refs = AR._json(root / "data" / "assets" / "asset_refs.json")
        lst = (refs or {}).get(slug) if isinstance(refs, dict) else None
        ids = sorted(str(r.get("ref_id")) for r in (lst or [])
                     if isinstance(r, dict) and r.get("role") == "identity" and not r.get("void"))
        if not files and not face_ptr and not ids:
            return None
        return {"kind": "files", "files": files, "face_reference_pointer": face_ptr, "identity_ref_ids": ids}
    if typ == "loraslot":
        slug, _, kind = rest.partition(":")
        kind = kind or "image"
        if kind == "video":
            from . import lora_registry as LR
            return {"kind": "config", "config": LR.EXT.as_posix(), "path": ["loras", f"character={slug}", "kind=video"],
                    "value": LR.video_slot_entries(root, slug)}
        cfg = AR._json(root / "config" / "auto_lora_triggers.json")
        name = AR.char_name(slug)
        es = ((cfg or {}).get("characters") or {}).get(name) if isinstance(cfg, dict) else None
        return {"kind": "config", "config": "config/auto_lora_triggers.json", "path": ["characters", name],
                "value": [e for e in (es or []) if isinstance(e, dict) and (e.get("kind") or "image") == kind]}
    if typ == "preset":
        q = AR._quality(root)
        for lst in ("image_presets", "user_presets"):
            for p in q.get(lst) or []:
                if isinstance(p, dict) and str(p.get("id")) == rest:
                    return {"kind": "config", "config": "config/render_quality.json", "path": [lst, f"id={rest}"],
                            "value": p}
        return None
    if typ == "vbase":
        b = AR.vbases(root).get(rest)
        return {"kind": "config", "config": "config/render_quality.json",
                "path": ["video_bases", "bases", f"id={rest}"], "value": b} if b else None
    if typ == "vlora":
        e = AR.vloras(root).get(rest)
        return {"kind": "config", "config": "config/render_quality.json",
                "path": ["optional_video_loras", f"name={rest}"], "value": e} if e else None
    if typ == "voice":
        v = AR._json(root / "config" / "voices.json")
        c = ((v or {}).get("characters") or {}).get(rest) if isinstance(v, dict) else None
        if not isinstance(c, dict):
            return None
        return {"kind": "config", "config": "config/voices.json", "path": ["characters", rest], "value": c,
                "tts_model": ((v or {}).get("tts_model") or {}).get("id")}
    return None


def snap_dir(key: str, rev: str, root: str | Path | None = None) -> Path:
    typ, _, rest = key.partition(":")
    return project_root(root) / SNAP_DIR / _safe(typ) / _safe(rest) / _safe(rev)


def has_snapshot(key: str, rev: str | None, root: str | Path | None = None) -> bool:
    return bool(rev) and (snap_dir(key, rev, root) / "manifest.json").is_file()


def read_snapshot(key: str, rev: str, root: str | Path | None = None) -> dict | None:
    try:
        return json.loads((snap_dir(key, rev, root) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def snapshot(key: str, root: str | Path | None = None, reason: str = "observe") -> dict | None:
    """当前内容 → 快照目录（同一 rev 已有就直接返回）。复制后再算一次 rev，对不上（复制途中被改）就不留 manifest"""
    root = project_root(root)
    rev = AR.current_rev(key, root)
    if not rev:
        return None
    d = snap_dir(key, rev, root)
    if (d / "manifest.json").is_file():
        return read_snapshot(key, rev, root)
    frag = _fragment(key, root)
    if frag is None:
        return None
    d.mkdir(parents=True, exist_ok=True)
    copied = {}
    for name, rel in (frag.get("files") or {}).items():
        src = root / rel
        dst = d / f"{name}{Path(rel).suffix}"
        shutil.copy2(src, dst)
        copied[name] = {"from": rel, "file": dst.name, "sha256": AR._sha(dst)}
    if AR.current_rev(key, root) != rev:          # 复制途中被改了 → 这份不可信，不写 manifest（文件留着，不删）
        return None
    man = {"key": key, "rev": rev, "at": _now(), "reason": reason, **frag, "copied": copied}
    (d / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    return man


def snapshot_quiet(key: str, root: str | Path | None = None, reason: str = "observe") -> None:
    try:
        snapshot(key, root, reason)
    except Exception:  # noqa: BLE001 — 快照失败不影响读
        pass


def snapshot_before_overwrite(path: str | Path, root: str | Path | None = None) -> list[str]:
    """原地覆盖某个文件之前调用：凡是依赖这个文件的键都先快照（目前是 assets/characters/<slug>/… 的定妆母版 / 脸图）"""
    root = project_root(root)
    rel = Path(path).resolve().relative_to(root.resolve()).as_posix() if Path(path).is_absolute() else Path(path).as_posix()
    m = re.match(r"assets/characters/([^/]+)/", rel)
    done = []
    if m:
        key = f"identity:{m.group(1)}"
        if snapshot(key, root, reason=f"before_overwrite:{rel}"):
            done.append(key)
    return done


# ------------------------------------------------------------------ 提议（不直接写 config）

def write_proposal(kind: str, body: dict, root: str | Path | None = None) -> dict:
    root = project_root(root)
    pid = f"{time.strftime('%Y%m%d-%H%M%S')}_{_safe(kind)}_{uuid.uuid4().hex[:6]}"
    rec = {"proposal_id": pid, "kind": kind, "status": "pending", "created_at": _now(),
           "note": "系统只记录提议，不直接改配置；由项目方 / 总调度按配置写入规则落地", **body}
    p = root / PROPOSALS / f"{pid}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return {**rec, "file": (PROPOSALS / f"{pid}.json").as_posix()}


def list_proposals(root: str | Path | None = None, kind: str | None = None) -> list[dict]:
    d = project_root(root) / PROPOSALS
    out = []
    for p in sorted(d.glob("*.json"), reverse=True) if d.is_dir() else []:
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(r, dict) and (not kind or r.get("kind") == kind):
            out.append({**r, "file": (PROPOSALS / p.name).as_posix()})
    return out


# ------------------------------------------------------------------ 移当前指针（含回滚）

def set_current(key: str, rev: str, by: str | None, owner_quote: str | None = None, note: str | None = None,
                root: str | Path | None = None) -> dict:
    root = project_root(root)
    check_gate(key, by, owner_quote)
    cur = AR.current_rev(key, root)
    if rev == cur:
        return {"key": key, "rev": rev, "applied": True, "changed": False, "proposal": None,
                "message": "已经是当前版本"}
    man = read_snapshot(key, rev, root)
    if not man:
        raise RevNotFound(f"{key} 的 {rev} 没有快照（P1 之前登记的版本只记了指纹、没留内容），无法自动回到这版")
    # 原地覆盖前：当前内容先快照；快照不成功（且当前确有内容）就不动
    pre = snapshot(key, root, reason=f"before_set_current:{rev}") if cur else None
    if cur and not pre:
        raise RevNotFound(f"{key} 当前内容快照失败（可能正在被改），这次不覆盖，稍后再试")
    restored, pending = [], {}
    if man.get("kind") == "files":
        d = snap_dir(key, rev, root)
        for name, info in (man.get("copied") or {}).items():
            dst = root / info["from"]
            if AR._sha(dst) == info.get("sha256"):
                continue
            src = d / info["file"]
            if not src.is_file():
                pending[name] = f"快照文件缺失：{info['file']}"
                continue
            if info["from"].startswith("config/"):
                pending[name] = {"restore_file": info["from"], "from_snapshot": src.relative_to(root).as_posix()}
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            restored.append(info["from"])
        now = _fragment(key, root) or {}
        if now.get("face_reference_pointer") != man.get("face_reference_pointer"):
            pending["face_reference_pointer"] = {"config": "config/face_reference.json", "path": ["reference"],
                                                 "from": now.get("face_reference_pointer"),
                                                 "to": man.get("face_reference_pointer")}
        if now.get("identity_ref_ids") != man.get("identity_ref_ids"):
            pending["identity_ref_ids"] = {"file": "data/assets/asset_refs.json", "role": "identity",
                                           "from": now.get("identity_ref_ids"), "to": man.get("identity_ref_ids")}
    else:
        now = _fragment(key, root) or {}
        pending["config"] = {"config": man.get("config"), "path": man.get("path"), "from": now.get("value"),
                             "to": man.get("value")}
    proposal = None
    if pending:
        proposal = write_proposal("set_current", {"key": key, "label": AR.label(key, root), "from_rev": cur,
                                                   "to_rev": rev, "by": by, "owner_quote": owner_quote,
                                                   "changes": pending}, root)
    after = AR.current_rev(key, root)
    applied = after == rev
    led = AR.read_ledger(root)
    hist = led.get(key) or []
    if applied and (not hist or hist[-1].get("rev") != rev):
        old = next((h for h in hist if h.get("rev") == rev), None)
        AR._LEDGER_CACHE.clear()
        _append(AR.ledger_path(root), {
            "key": key, "rev": rev, "seq": old["seq"] if old else len({h["rev"] for h in hist}) + 1,
            "prev": cur, "label": AR.label(key, root), "at": _now(), "by": by or "canvas",
            "owner_quote": owner_quote, "note": note, "rollback": bool(old),
            "source": {"api": f"POST /api/revisions/{key}/set-current"},
            "snapshot": snap_dir(key, rev, root).relative_to(root).as_posix()})
    action = {"action": "set_current", "key": key, "from_rev": cur, "to_rev": rev, "after_rev": after,
              "applied": applied, "restored": restored, "proposal": (proposal or {}).get("file"), "by": by,
              "owner_quote": owner_quote, "note": note, "at": _now()}
    _append(root / ACTIONS, action)
    msg = ("已回到这版" if applied else
           "已写提议（配置文件不由系统直接改，等项目方 / 总调度落地）" if proposal else "没有变化")
    return {"key": key, "rev": rev, "applied": applied, "changed": bool(restored), "restored": restored,
            "proposal": proposal, "current_after": after, "message": msg}


def actions(root: str | Path | None = None, key: str | None = None) -> list[dict]:
    rows = _jsonl(project_root(root) / ACTIONS)
    return [r for r in rows if not key or r.get("key") == key]


# ------------------------------------------------------------------ 保留旧版（kept 豁免）

def keep(shot_id: str, stage: str, key: str, by: str | None, owner_quote: str | None, n: int | None = None,
         root: str | Path | None = None, rev_at_waiver: str | None = None) -> dict:
    """豁免某镜某格对某依赖的“依赖已更新”：只对此刻的当前 rev 生效（rev_at_waiver）"""
    if by != "owner" or not (owner_quote or "").strip():
        raise GateError("“保留旧版”要项目方本人（by=owner）并附原话 owner_quote")
    root = project_root(root)
    cur = rev_at_waiver if key == AR.INPUTS_KEY else AR.current_rev(key, root)
    row = {"waiver_id": f"kw-{uuid.uuid4().hex[:8]}", "shot_id": shot_id, "stage": stage, "n": n, "key": key,
           "rev_at_waiver": cur, "by": "owner", "owner_quote": owner_quote.strip(), "at": _now()}
    _append(root / WAIVERS, row)
    return row


def waivers(root: str | Path | None = None, shot_id: str | None = None) -> list[dict]:
    rows = _jsonl(project_root(root) / WAIVERS)
    return [r for r in rows if not shot_id or r.get("shot_id") == shot_id]


def waived(item: dict, shot_id: str, stage: str, n: int | None, rows: list[dict]) -> dict | None:
    """stale_deps 的一项是否被豁免：同镜同格（n 不填 = 该格任意版本）同键，且豁免时的 rev == 现在的 rev"""
    for w in reversed(rows):
        if w.get("shot_id") != shot_id or w.get("stage") != stage or w.get("key") != item.get("key"):
            continue
        if w.get("n") is not None and n is not None and w.get("n") != n:
            continue
        if w.get("rev_at_waiver") == item.get("to"):
            return w
    return None
