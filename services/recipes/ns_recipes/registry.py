"""配方注册表：读 services/recipes/recipes/*.yaml（+ overlays/），解析 extends 继承、叠加 overlay、
叠加自动迭代写回的已确认参数（config/recipes/learned.yaml，只取 status=确认 的条目）。

每个叶子参数都必须能在 provenance 表里按“点路径最长前缀”找到来源（validate() 检查，测试强制）。
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
RECIPE_DIR = Path(__file__).resolve().parents[1] / "recipes"
LEARNED_PATH = ROOT / "config" / "recipes" / "learned.yaml"

META_KEYS = {"id", "extends", "abstract", "provenance", "priority", "kind", "experiment", "selectable",
             "registry", "words", "match", "set", "when_exposure", "block_recipes", "keyframe_hints", "label",
             "status"}
STATUSES = ("确认", "建议", "待验", "待许可登记")
LEARNED_STATUS_APPLIED = "确认"


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def get_path(d: dict, path: str, default=None):
    cur = d
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def set_path(d: dict, path: str, value) -> dict:
    parts = path.split(".")
    cur = d
    for p in parts[:-1]:
        if not isinstance(cur.get(p), dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = copy.deepcopy(value)
    return d


def leaves(d: dict, prefix: str = ""):
    for k, v in d.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict) and v:
            yield from leaves(v, path)
        else:
            yield path, v


def canonical_hash(obj) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def provenance_for(prov: dict, path: str) -> dict | None:
    p = path
    while True:
        if p in prov:
            return prov[p]
        if "." not in p:
            return None
        p = p.rsplit(".", 1)[0]


@dataclass
class Registry:
    raw: dict[str, dict]
    overlays: dict[str, dict]
    learned: list[dict] = field(default_factory=list)
    source_hash: str = ""

    # ---- 解析 ----
    def _resolve_raw(self, rid: str, seen=()) -> dict:
        if rid in seen:
            raise ValueError(f"配方继承成环：{' → '.join((*seen, rid))}")
        if rid not in self.raw:
            raise KeyError(f"没有配方 {rid}")
        r = self.raw[rid]
        if r.get("extends"):
            parent = self._resolve_raw(r["extends"], (*seen, rid))
            child = {k: v for k, v in r.items() if k not in ("provenance", "abstract")}
            merged = deep_merge({k: v for k, v in parent.items() if k not in ("abstract", "selectable",
                                                                               "experiment", "priority", "match")},
                                child)
            merged["provenance"] = {**parent.get("provenance", {}), **r.get("provenance", {})}
            merged["abstract"] = bool(r.get("abstract", False))
            return merged
        return copy.deepcopy(r)

    def ids(self, include_abstract: bool = False) -> list[str]:
        return sorted(k for k, v in self.raw.items() if include_abstract or not v.get("abstract"))

    def resolve(self, rid: str, overlays: list[str] | None = None, exposure: str = "normal",
                apply_learned: bool = True) -> dict:
        r = self._resolve_raw(rid)
        r.setdefault("loras", {})
        applied = []
        for oid in overlays or []:
            ov = self.overlays[oid]
            for path, val in (ov.get("set") or {}).items():
                set_path(r, path, val)
            for path, val in ((ov.get("when_exposure") or {}).get(exposure) or {}).items():
                set_path(r, path, val)
            for k, v in (ov.get("provenance") or {}).items():
                r["provenance"][k] = {**v, "overlay": oid}
            applied.append(oid)
        learned_applied = []
        if apply_learned:
            for e in self.learned:
                if e.get("recipe") == rid and e.get("status") == LEARNED_STATUS_APPLIED:
                    set_path(r, e["path"], e["value"])
                    r["provenance"][e["path"]] = {"src": f"iterate_loop 写回（{len(e.get('evidence', []))} 条证据，"
                                                         f"{e.get('key')}）", "date": e.get("updated"),
                                                  "status": "确认", "learned": True}
                    learned_applied.append(e["key"])
        r["_overlays"] = applied
        r["_learned"] = learned_applied
        return r

    def params_of(self, resolved: dict) -> dict:
        """配方里参与编译 / 哈希的参数（去掉元数据和 provenance）"""
        return {k: v for k, v in resolved.items() if k not in META_KEYS and not k.startswith("_")}

    def recipe_hash(self, resolved: dict) -> str:
        return canonical_hash(self.params_of(resolved))

    def capabilities(self, rid: str) -> dict:
        """配方能力档案（COMP 建议 2：写作方拆镜前拿到；导入校验用）"""
        r = self.resolve(rid, apply_learned=True)
        fr = r["frames"]
        ref = r.get("ref2va") or {}
        return {"recipe": rid, "label": r.get("label"), "status": r.get("status"),
                "mode": r.get("mode"), "min_s": round((fr["min"] - 1) / fr["fps"], 2),
                "max_s": round(fr["max"] / fr["fps"], 2), "frame_grid": fr["grid"],
                "single_lane_max_s": round(fr["lane_max_default"] / fr["fps"], 2),
                "locks_first_frame": True, "locks_last_frame": (r.get("keyframes") or {}).get("last_frame_mode") == "use",
                "max_ref_images": int(ref.get("max_refs", 0)) if ref.get("enabled") else 0,
                "outputs_audio": True, "max_onscreen_speakers": (r.get("prompt") or {}).get("max_speakers"),
                "resolution": [r["resolution"]["width"], r["resolution"]["height"]]}

    # ---- 校验 ----
    def validate(self) -> list[str]:
        errs = []
        for rid in self.ids(include_abstract=True):
            try:
                r = self._resolve_raw(rid)
            except (KeyError, ValueError) as e:
                errs.append(f"{rid}: {e}")
                continue
            prov = r.get("provenance", {})
            if r.get("status") not in STATUSES:
                errs.append(f"{rid}: status {r.get('status')!r} 不在 {STATUSES}")
            for path, _v in leaves({k: v for k, v in r.items() if k not in ("provenance",)}):
                top = path.split(".")[0]
                if top in ("id", "extends", "abstract", "priority", "kind", "registry", "words", "provenance"):
                    continue
                pv = provenance_for(prov, path)
                if not pv or not pv.get("src") or pv.get("status") not in STATUSES:
                    errs.append(f"{rid}: 参数 {path} 没有合格的 provenance")
            for fb in r.get("fallback") or []:
                if fb not in self.raw:
                    errs.append(f"{rid}: fallback {fb} 不存在")
            if r.get("blocked_substitute") and r["blocked_substitute"] not in self.raw:
                errs.append(f"{rid}: blocked_substitute {r['blocked_substitute']} 不存在")
            if r.get("status") == "待许可登记" and r.get("selectable", True) and not r.get("abstract"):
                errs.append(f"{rid}: 待许可登记的配方必须 selectable: false")
        for oid, ov in self.overlays.items():
            prov = ov.get("provenance", {})
            for path in (ov.get("set") or {}):
                if not provenance_for(prov, path):
                    errs.append(f"overlay {oid}: {path} 没有 provenance")
        return errs


def _load_dir(d: Path) -> dict[str, dict]:
    out = {}
    for p in sorted(d.glob("*.yaml")):
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        data = json.loads(json.dumps(data, ensure_ascii=False, default=str))   # YAML 日期 → 字符串（计划要能 JSON 化）
        rid = data.get("id") or p.stem
        if rid in out:
            raise ValueError(f"配方 id 重复：{rid}（{p.name}）")
        data["_file"] = str(p.relative_to(ROOT)).replace("\\", "/") if p.is_relative_to(ROOT) else str(p)
        out[rid] = data
    return out


def load_learned(path: Path | None = None) -> list[dict]:
    path = path or LEARNED_PATH
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("entries") or [])


def load(recipe_dir: Path | None = None, learned_path: Path | None = None, use_learned: bool = True) -> Registry:
    recipe_dir = recipe_dir or RECIPE_DIR
    raw = _load_dir(recipe_dir)
    overlays = _load_dir(recipe_dir / "overlays") if (recipe_dir / "overlays").exists() else {}
    for v in (*raw.values(), *overlays.values()):
        v.pop("_file", None)
    learned = load_learned(learned_path) if use_learned else []
    src = canonical_hash({"raw": raw, "overlays": overlays,
                          "learned": [e for e in learned if e.get("status") == LEARNED_STATUS_APPLIED]})
    return Registry(raw=raw, overlays=overlays, learned=learned, source_hash=src)
