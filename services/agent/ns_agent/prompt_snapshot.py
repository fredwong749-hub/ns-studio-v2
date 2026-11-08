"""
按规则版本存全剧编译结果快照（T06）：data/bible_versions/<bible_version>.json.gz，供 tools/bible_diff.py 比较。

快照 = 每镜 compile_shot（全部 LoRA 视为可用，保留触发词；不含画布人工修改）的首帧/尾帧/视频提示词与负面词
+ Frame Spec 拼装（qwen / qwen_last）+ 本镜 inputs_sha。剧本导入、造型修改（时间线重建）、bible_diff 运行时自动补存。
"""
import gzip
import json
import time
from pathlib import Path

from .bible import bible_version, load_bible, project_root

FIELDS = ("image_prompt", "image_negative", "last_prompt", "video_prompt", "video_negative", "adult")


def snapshot_dir(root: Path) -> Path:
    return root / "data" / "bible_versions"


def compile_all(root: str | Path | None = None, inject_looks: bool | None = None, confirmed_only: bool = True) -> dict:
    """→ {shot_id: {字段: 值}}。inject_looks/confirmed_only 只用于预览造型注入（不改 toml）"""
    from ns_schema.script import ScriptImport

    from . import compiler as C
    from .frame_spec import build_spec
    root = project_root(root)
    b = load_bible(root)
    from . import run_paths as RP
    script = ScriptImport.model_validate_json(RP.script_json(root).read_text(encoding="utf-8"))   # I-14
    g = script.globals
    files = {t.file for t in g.lora_triggers if t.file}
    trig = {t.trigger for t in g.lora_triggers if t.file}
    out = {}
    for ep in script.episodes:
        for s in ep.shots:
            c = C.compile_shot(ep, s, g, files, bible=b, inject_looks=inject_looks, inject_confirmed_only=confirmed_only)
            row = {k: getattr(c, k) for k in FIELDS}
            sp = build_spec(ep, s, g, available_triggers=trig, bible=b)
            row["spec_qwen"], row["spec_qwen_last"] = sp.model_prompt.qwen, sp.model_prompt.qwen_last
            out[s.id] = row
    return out


def save_snapshot(root: str | Path | None = None, force: bool = False) -> Path:
    import os
    import tempfile
    root = project_root(root)
    v = bible_version(root)
    p = snapshot_dir(root) / f"{v}.json.gz"
    if p.exists() and not force:
        return p
    for _ in range(5):          # 编译期间输入变了 → 版本号对不上编译结果：按新版本重编（不存错版本）
        shots = compile_all(root)
        v2 = bible_version(root)
        if v2 == v:
            break
        v = v2
        p = snapshot_dir(root) / f"{v}.json.gz"
        if p.exists() and not force:
            return p
    else:
        raise RuntimeError("规则输入在编译期间持续变化，快照未保存")
    try:
        from .state_timeline import read_timeline
        tl = read_timeline(root) or {}
        if tl.get("bible_version") == v:
            for s in tl.get("shots", []):
                if s["shot_id"] in shots:
                    shots[s["shot_id"]]["inputs_sha"] = s.get("inputs_sha")
    except Exception:  # noqa: BLE001
        pass
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".", suffix=".tmp")   # 唯一临时文件：并发保存互不踩
    try:
        with os.fdopen(fd, "wb") as raw, gzip.open(raw, "wt", encoding="utf-8") as f:
            json.dump({"bible_version": v, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "shots": shots}, f,
                      ensure_ascii=False)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def load_snapshot(version: str, root: str | Path | None = None) -> dict | None:
    root = project_root(root)
    d = snapshot_dir(root)
    hits = sorted(d.glob(f"{version}*.json.gz")) if d.is_dir() and version else []
    if len(hits) != 1:
        return None
    with gzip.open(hits[0], "rt", encoding="utf-8") as f:
        return json.load(f)


def from_compiled_files(version: str, root: str | Path | None = None) -> dict:
    """退路：没有快照时，用 data/renders/epXX/SN/compiled.json 里 bible_version 匹配的那些（含画布修改）"""
    root = project_root(root)
    out = {}
    from . import run_paths as RP
    for p in sorted(RP.renders_dir(root).glob("ep*/S*/compiled.json")):   # I-14
        try:
            c = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if version and str(c.get("bible_version", "")).startswith(version):
            out[c["shot_id"]] = {k: c.get(k) for k in FIELDS}
    return out


def diff(old: dict, new: dict, fields=FIELDS + ("spec_qwen", "spec_qwen_last")) -> dict[str, list[str]]:
    """→ {shot_id: [变了的字段]}（新增/删除的镜头记为 ["added"] / ["removed"]）"""
    out = {}
    for sid in sorted(set(old) | set(new), key=_order):
        if sid not in old:
            out[sid] = ["added"]
        elif sid not in new:
            out[sid] = ["removed"]
        else:
            ch = [f for f in fields if f in old[sid] and old[sid].get(f) != new[sid].get(f)]
            if ch:
                out[sid] = ch
    return out


def _order(sid: str):
    try:
        ep, s = sid.split("-S")
        return int(ep[2:]), int(s)
    except ValueError:
        return 0, 0
