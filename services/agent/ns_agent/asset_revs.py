"""
资产二次修改 P0（docs/ux_round5/B_revision.md）：依赖键 → 内容指纹 rev、镜头版本盖章 deps、读时比对“依赖已更新”。

依赖键（P0 覆盖现有过期检测没管到的那几类，造型 / 单品 / 场景底图 / 首帧沿用原有机制，不重复）：
    identity:<slug>          定妆母版 plates/master.png + face_reference.json 指向的图 + 资产页身份参考（role=identity，未作废）
    loraslot:<slug>:image    人物出图身份 LoRA 指针（config/auto_lora_triggers.json：文件 + 强度 + 登记 sha）
    preset:<id>              出图预设内容（render_quality.json image_presets / user_presets 的 tier + params）
    vbase:<id>               视频基底配方（render_quality.json video_bases.bases，去掉说明性字段）
    vlora:<name>             附加视频 LoRA（optional_video_loras：文件名 + sha256）
    voice:<slug>             音色（voices.json 本人条目 + tts 模型 id）；P0 只出指纹，不进镜头 deps
    registry:<slug>          人物表登记（剧本导入 data/import/script_v2.json 的 character_registry：EN外貌短语 / VOICE
                             声线短语 / 中文外貌句）；进视频 / 首帧 deps——改了登记，用到它的旧版本自动标“依赖已更新”
                             （ARCH 2026-10-31，CHAINSPEC 系统问题 ①）

rev = 规范化内容 sha256 前 12 位（内容不变 rev 不变 → 回滚后自动恢复“不过期”）。
版本账本 data/assets/revisions.jsonl（只追加）：observe() 发现某键当前 rev 不在账本里 → 记一行（第一次 = baseline，
之后 = observed）。seq 给人看的 vN。

只读本地文件，不做内容检测，不改任何配置；盖章失败 / 算不出一律返回空，不影响渲染。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

from .bible import project_root
from .state_timeline import timeline_path

try:
    from ns_schema.looks import CHARACTERS
except Exception:  # noqa: BLE001
    CHARACTERS = {"lin_wan": "林晚", "gu_chen": "顾沉", "chen_yu": "陈宇"}

LEDGER = Path("data") / "assets" / "revisions.jsonl"
DEP_PREFIX = "依赖已更新："
INPUTS_KEY = "inputs"                       # G1：本镜 inputs_sha 变了而提示词没变（母版 / 参考 / LoRA / 底图…）
# 视频基底里只是说明、不影响出片的字段
VBASE_DESC = {"label", "short", "note", "source_url", "license", "usage", "tag", "owner_decision", "src",
              "experimental", "default_last_frame_mode"}
TYPES = ("identity", "loraslot", "preset", "vbase", "vlora", "voice", "registry")
SCRIPT_JSON = Path("data") / "import" / "script_v2.json"

_lock = threading.RLock()
_JSON_CACHE: dict = {}
_SHA_CACHE: dict = {}
RACY_S = 2                                   # mtime 早于这么多秒的文件才只看 (mtime_ns, size)
_LEDGER_CACHE: dict = {}


# ------------------------------------------------------------------ 基础

def _h(obj) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:12]


def _read_cached(p: Path, cache: dict, parse):
    """带缓存的文件读取 → parse(bytes)；读不到 → None。
    Windows 上 mtime 按系统时钟刻度（约 15.6 ms）走，同一刻度内写两次同长度文件 (st_mtime_ns, size) 完全相同，
    只靠它会命中旧内容。所以只有文件 mtime 已早于 RACY_S 秒、且 (st_mtime_ns, size) 没变时才直接用缓存；
    “刚改过”的文件每次都读内容、按内容 sha256 判断（同 git 的 racily-clean 处理）"""
    try:
        st = p.stat()
    except OSError:
        return None
    hit = cache.get(str(p))
    stable = time.time_ns() - st.st_mtime_ns > RACY_S * 1_000_000_000
    if hit and stable and hit[0][:2] == (st.st_mtime_ns, st.st_size):
        return hit[1]
    try:
        raw = p.read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(raw).hexdigest()
    key = (st.st_mtime_ns, st.st_size, digest)
    if hit and hit[0][2] == digest:
        cache[str(p)] = (key, hit[1])
        return hit[1]
    try:
        val = parse(raw, digest)
    except (UnicodeDecodeError, ValueError):
        val = None
    cache[str(p)] = (key, val)
    return val


def _json(p: Path):
    """JSON 读取（缓存见 _read_cached）；读不到 / 不是 JSON → None"""
    return _read_cached(p, _JSON_CACHE, lambda raw, _d: json.loads(raw.decode("utf-8")))


def _sha(p: Path) -> str | None:
    """文件内容 sha256（缓存见 _read_cached）；不在 → None"""
    return _read_cached(p, _SHA_CACHE, lambda _raw, d: d)


def _quality(root: Path) -> dict:
    q = _json(root / "config" / "render_quality.json")
    return q if isinstance(q, dict) else {}


def char_name(slug: str) -> str:
    return CHARACTERS.get(slug, slug)


# ------------------------------------------------------------------ 各类指纹

def identity_rev(root: Path, slug: str) -> str | None:
    parts: dict = {"master": _sha(root / "assets" / "characters" / slug / "plates" / "master.png")}
    fr = _json(root / "config" / "face_reference.json")
    ref = (fr or {}).get("reference") if isinstance(fr, dict) else None
    if isinstance(ref, str) and f"/{slug}/" in "/" + ref.replace("\\", "/"):
        parts["face_reference"] = [ref, _sha(root / ref)]
    refs = _json(root / "data" / "assets" / "asset_refs.json")
    lst = (refs or {}).get(slug) if isinstance(refs, dict) else None
    ids = sorted([r.get("ref_id"), r.get("sha256") or _sha(root / str(r.get("file") or ""))]
                 for r in (lst or []) if isinstance(r, dict) and r.get("role") == "identity" and not r.get("void"))
    if ids:
        parts["identity_refs"] = ids
    if not any(v for v in parts.values()):
        return None
    return _h(parts)


def _lora_shas(root: Path) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    reg = _json(root / "data" / "renders_gpu3" / "LORA_REGISTRY.json")
    for e in (reg or {}).get("loras") or [] if isinstance(reg, dict) else []:
        if isinstance(e, dict) and e.get("file"):
            out[str(e["file"])] = e.get("sha256")
    lock = _json(root / "comfy" / "models.lock")
    for e in (lock or {}).get("loras") or [] if isinstance(lock, dict) else []:
        if isinstance(e, dict) and e.get("file") and not out.get(str(e["file"])):
            out[str(e["file"])] = e.get("sha256")
    return out


def loraslot_rev(root: Path, slug: str, kind: str = "image") -> str | None:
    cfg = _json(root / "config" / "auto_lora_triggers.json")
    es = ((cfg or {}).get("characters") or {}).get(char_name(slug)) if isinstance(cfg, dict) else None
    shas = _lora_shas(root)
    rows = sorted([str(e.get("file")), e.get("strength"), shas.get(str(e.get("file")))]
                  for e in (es or []) if isinstance(e, dict) and e.get("file") and (e.get("kind") or "image") == kind)
    return _h(rows) if rows else None


def presets(root: Path) -> dict[str, dict]:
    q = _quality(root)
    out = {}
    for p in (q.get("image_presets") or []) + (q.get("user_presets") or []):
        if isinstance(p, dict) and p.get("id"):
            out[str(p["id"])] = p
    return out


def preset_rev(root: Path, pid: str | None) -> str | None:
    p = presets(root).get(pid or "")
    return _h({"tier": p.get("tier"), "params": p.get("params")}) if p else None


def vbases(root: Path) -> dict[str, dict]:
    vb = _quality(root).get("video_bases") or {}
    return {str(b["id"]): b for b in (vb.get("bases") or []) if isinstance(b, dict) and b.get("id")}


def vbase_rev(root: Path, bid: str | None) -> str | None:
    b = vbases(root).get(bid or "")
    return _h({k: v for k, v in b.items() if k not in VBASE_DESC}) if b else None


def vloras(root: Path) -> dict[str, dict]:
    return {str(e["name"]): e for e in _quality(root).get("optional_video_loras") or []
            if isinstance(e, dict) and e.get("name")}


def vlora_rev(root: Path, name: str | None) -> str | None:
    e = vloras(root).get(name or "")
    return _h({"name": e.get("name"), "sha256": e.get("sha256")}) if e else None


def voice_rev(root: Path, slug: str) -> str | None:
    v = _json(root / "config" / "voices.json")
    if not isinstance(v, dict):
        return None
    c = (v.get("characters") or {}).get(slug)
    if not isinstance(c, dict):
        return None
    return _h({"model": (v.get("tts_model") or {}).get("id"), "sid": c.get("sid"), "voice": c.get("voice"),
               "speed": c.get("speed")})


def registry_rev(root: Path, slug: str) -> str | None:
    """人物表登记指纹：按 char_id（或中文名）找 character_registry 一行，只取影响生成的三格"""
    from . import run_paths as RP
    d = _json(RP.script_json(root))                           # I-14：按剧取导入稿
    if not isinstance(d, dict):
        return None
    name = char_name(slug)
    for c in (d.get("globals") or {}).get("character_registry") or []:
        if c.get("char_id") == slug or c.get("name") == name:
            return _h({"en": c.get("en"), "voice": c.get("voice"), "zh": c.get("zh")})
    return None


def _slugs(root: Path) -> list[str]:
    d = root / "assets" / "characters"
    found = {p.name for p in d.iterdir() if p.is_dir()} if d.is_dir() else set()
    return sorted(found | set(CHARACTERS))


def current_rev(key: str, root: str | Path | None = None) -> str | None:
    root = project_root(root)
    typ, _, rest = key.partition(":")
    if typ == "identity":
        return identity_rev(root, rest)
    if typ == "loraslot":
        slug, _, kind = rest.partition(":")
        if kind == "video":                      # P1：视频身份 LoRA 指针（registry 扩展里 accepted 的条目）
            from .lora_registry import loraslot_video_rev
            return loraslot_video_rev(root, slug)
        return loraslot_rev(root, slug, kind or "image")
    if typ == "preset":
        return preset_rev(root, rest)
    if typ == "vbase":
        return vbase_rev(root, rest)
    if typ == "vlora":
        return vlora_rev(root, rest)
    if typ == "voice":
        return voice_rev(root, rest)
    if typ == "registry":
        return registry_rev(root, rest)
    return None


def all_keys(root: str | Path | None = None) -> dict[str, str]:
    """全部已知依赖键 → 当前 rev（算不出的不列）"""
    root = project_root(root)
    keys: list[str] = []
    for s in _slugs(root):
        keys += [f"identity:{s}", f"loraslot:{s}:image", f"loraslot:{s}:video", f"voice:{s}", f"registry:{s}"]
    keys += [f"preset:{p}" for p in presets(root)]
    keys += [f"vbase:{b}" for b in vbases(root)]
    keys += [f"vlora:{n}" for n in vloras(root)]
    out = {}
    for k in keys:
        r = current_rev(k, root)
        if r:
            out[k] = r
    return out


def label(key: str, root: str | Path | None = None) -> str:
    typ, _, rest = key.partition(":")
    if typ == "identity":
        return f"{char_name(rest)} 定妆 / 身份参考"
    if typ == "loraslot":
        slug, _, kind = rest.partition(":")
        return f"{char_name(slug)} {'视频' if kind == 'video' else '出图'}身份 LoRA"
    if typ == "voice":
        return f"{char_name(rest)} 音色"
    if typ == "registry":
        return f"{char_name(rest)} 人物表登记（EN / VOICE）"
    root = project_root(root)
    if typ == "preset":
        return f"出图预设「{(presets(root).get(rest) or {}).get('label') or rest}」"
    if typ == "vbase":
        return f"视频基底「{(vbases(root).get(rest) or {}).get('label') or rest}」配方"
    if typ == "vlora":
        return f"附加视频 LoRA「{(vloras(root).get(rest) or {}).get('label') or rest}」"
    if key == INPUTS_KEY:
        return "本镜输入（母版 / 参考 / LoRA / 底图 / 造型）"
    return key


# ------------------------------------------------------------------ 账本

def ledger_path(root: str | Path | None = None) -> Path:
    return project_root(root) / LEDGER


def read_ledger(root: str | Path | None = None) -> dict[str, list[dict]]:
    p = ledger_path(root)
    out: dict[str, list[dict]] = {}
    try:
        st = p.stat()
        ck = (st.st_mtime_ns, st.st_size)
        hit = _LEDGER_CACHE.get(str(p))
        if hit and hit[0] == ck:
            return hit[1]
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("key") and r.get("rev"):
            out.setdefault(r["key"], []).append(r)
    _LEDGER_CACHE[str(p)] = (ck, out)
    return out


def observe(root: str | Path | None = None) -> list[dict]:
    """把当前 rev 不在账本里的键记一行（第一次见到该键 = baseline，之后 = observed：未经系统登记的改动）。返回新增行"""
    root = project_root(root)
    with _lock:
        led = read_ledger(root)
        cur = all_keys(root)
        new = []
        for k, r in sorted(cur.items()):
            hist = led.get(k) or []
            if hist and hist[-1]["rev"] == r:
                continue
            old = next((h for h in hist if h["rev"] == r), None)
            new.append({"key": k, "rev": r, "seq": old["seq"] if old else len({h["rev"] for h in hist}) + 1,
                        "prev": hist[-1]["rev"] if hist else None, "label": label(k, root),
                        "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "by": "observed" if hist else "baseline",
                        "rollback": bool(old)})
        if new:
            p = ledger_path(root)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8", newline="\n") as f:
                for r in new:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        # P1：每个当前版本都留一份内容快照（同一 rev 只存一次），之后才能“回到这版”
        try:
            from .asset_revs_p1 import has_snapshot, snapshot_quiet
            for k, r in cur.items():
                if not has_snapshot(k, r, root):
                    snapshot_quiet(k, root)
        except Exception:  # noqa: BLE001
            pass
        return new


def seq_of(key: str, rev: str | None, ledger: dict[str, list[dict]] | None) -> int | None:
    for h in (ledger or {}).get(key) or []:
        if h.get("rev") == rev:
            return h.get("seq")
    return None


def _ver(key: str, rev: str | None, ledger) -> str:
    if rev is None:
        return "已移除"
    n = seq_of(key, rev, ledger)
    return f"v{n}" if n else rev[:6]


# ------------------------------------------------------------------ 镜头盖章

def present_characters(shot_id: str, root: str | Path | None = None) -> list[str]:
    """本镜在画的人物 slug（读已写好的 data/state_timeline.json；没有 → []）"""
    tl = _json(timeline_path(project_root(root)))
    for r in (tl or {}).get("shots") or [] if isinstance(tl, dict) else []:
        if r.get("shot_id") == shot_id:
            return [c for c, v in (r.get("characters") or {}).items() if isinstance(v, dict) and v.get("present")]
    return []


def _extra_vloras(params: dict) -> list[str]:
    ex = ((params.get("model") or {}).get("extra_loras")) or []
    return [str(x[0]) for x in ex if isinstance(x, (list, tuple)) and x]


def shot_deps(shot_id: str, stage: str, params: dict | None = None, root: str | Path | None = None) -> dict[str, str]:
    """本镜本格出图 / 出视频此刻所用依赖 → rev（算不出的键不记）"""
    root = project_root(root)
    params = params or {}
    keys: list[str] = []
    if stage in ("image", "last"):
        for c in present_characters(shot_id, root):
            keys += [f"identity:{c}", f"loraslot:{c}:image"]
        if params.get("preset"):
            keys.append(f"preset:{params['preset']}")
    elif stage == "video":
        keys += [f"registry:{c}" for c in present_characters(shot_id, root)]
        if params.get("base"):
            keys.append(f"vbase:{params['base']}")
        keys += [f"vlora:{n}" for n in _extra_vloras(params)]
    out = {}
    for k in keys:
        r = current_rev(k, root)
        if r:
            out[k] = r
    return out


def stamp(shot_id: str, take, root: str | Path | None = None) -> None:
    """给 Take（pydantic 或 dict）补 deps；已有的键不覆盖（派生版本继承父版本）。失败静默"""
    try:
        is_dict = isinstance(take, dict)
        stage = take.get("stage") if is_dict else take.stage
        params = (take.get("params") if is_dict else take.params) or {}
        cur = dict((take.get("deps") if is_dict else take.deps) or {})
        for k, r in shot_deps(shot_id, stage, params, root).items():
            cur.setdefault(k, r)
        if is_dict:
            take["deps"] = cur
        else:
            take.deps = cur
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ 过期比对

def stale_deps(take: dict, root: str | Path | None = None, ledger: dict | None = None) -> list[dict]:
    """take.deps 中当前 rev 已不同的项 → [{key, label, from, to, from_v, to_v, text}]；没有 deps → []"""
    deps = (take or {}).get("deps") or {}
    if not deps:
        return []
    root = project_root(root)
    if ledger is None:
        ledger = read_ledger(root)
    out = []
    for k, r in sorted(deps.items()):
        now = current_rev(k, root)
        if now == r:
            continue
        lb = label(k, root)
        fv, tv = _ver(k, r, ledger), _ver(k, now, ledger)
        out.append({"key": k, "label": lb, "from": r, "to": now, "from_v": fv, "to_v": tv,
                    "text": f"{DEP_PREFIX}{lb} {fv}→{tv}"})
    return out


def inputs_dep(take: dict, current_inputs_sha: str | None) -> dict | None:
    """G1：take.inputs_sha 与本镜当前 inputs_sha 不同（与提示词是否变无关）→ 一条依赖项；算不出 → None"""
    sha = (take or {}).get("inputs_sha")
    if not sha or not current_inputs_sha or sha == current_inputs_sha:
        return None
    lb = label(INPUTS_KEY)
    return {"key": INPUTS_KEY, "label": lb, "from": sha, "to": current_inputs_sha, "from_v": sha[:6],
            "to_v": current_inputs_sha[:6], "text": f"{DEP_PREFIX}{lb}"}


VBASE_FIELDS = ("lora", "cfg", "guider", "sha256")   # base_params 里原样抄自配置的字段（旧视频没有 deps 时用）


def vbase_drift(take: dict, root: str | Path | None = None) -> dict | None:
    """旧视频版本（无 deps）：params.base_params 中原样抄自配置的字段与当前基底配方不同 → 一条依赖项"""
    p = (take or {}).get("params") or {}
    bp, bid = p.get("base_params"), p.get("base")
    if (take or {}).get("deps") or not isinstance(bp, dict) or not bid:
        return None
    spec = vbases(project_root(root)).get(bid)
    if not spec:
        return None
    diff = [f for f in VBASE_FIELDS if f in bp and bp.get(f) != spec.get(f, 1.0 if f == "cfg" else
                                                                          "BasicGuider" if f == "guider" else None)]
    if not diff:
        return None
    lb = label(f"vbase:{bid}", root)
    return {"key": f"vbase:{bid}", "label": lb, "from": None, "to": vbase_rev(project_root(root), bid),
            "from_v": "旧", "to_v": "当前", "fields": diff, "text": f"{DEP_PREFIX}{lb}（{'、'.join(diff)}）"}
