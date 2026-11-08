"""
场景底图（S2/S3，fredw-92/S-back）：渲染侧只读工具 + 定稿记录。

- 场景 id：剧本写了 **场景**（Shot.scene_id）就用它；否则 state_timeline.group_scenes 推断（与 scenes.json 同一口径）。
- 定稿底图：assets/scenes/<scene_id>/manifest.json
    plate            主底图文件名（默认 plate.png；state_timeline.plate_info 读它判 S2 闸门状态）
    variants         {stem: {file, …}}（gen_scenes 已有；画布定稿的机位 / 时段变体也记这里，stem = plate_<variant>）
    locked           {by: "canvas"|"owner", at, sha256, version, variant, file}  画布定稿（主底图）
    locked_variants  {variant: {同上}}                                        画布定稿（变体）
    owner_confirmed_at / owner_confirmed_ref / plate_sha256                    项目方确认（只在 by=owner 时写）
  “已定稿” = 画布定稿记录的 sha 与文件当前 sha 相同，或项目方确认（plate_info 的 owner_confirmed）。
- 本镜带哪张：画布参考覆盖（ref_overrides：本镜 > 场景层）选了底图就用它（"none" = 不带）；否则用该场景定稿的主底图。
- 首帧出图把它排在人物参考之后，用途说明 refs.ROLE_SCENE（“只取场景空间与陈设，不取人物”）；
  take.scene_plate 记下用了哪张（含 sha），底图换版本后 stale_reasons → 「场景底图已改」。
不做内容检测。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

STALE_REASON = "场景底图已改"
MAIN = "main"


def scenes_dir(root: Path) -> Path:
    return Path(root) / "assets" / "scenes"


def read_manifest(root: Path, scene_id: str) -> dict:
    try:
        m = json.loads((scenes_dir(root) / scene_id / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return m if isinstance(m, dict) else {}


def write_manifest(root: Path, scene_id: str, man: dict) -> None:
    p = scenes_dir(root) / scene_id / "manifest.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(man, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


_SHA: dict[str, tuple[float, int, str]] = {}


def sha_file(p: Path) -> str | None:
    """内容 sha256（按 mtime + 大小缓存）"""
    try:
        st = p.stat()
    except OSError:
        return None
    key = str(p)
    hit = _SHA.get(key)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    _SHA[key] = (st.st_mtime, st.st_size, sha)
    return sha


def variant_stem(variant: str | None) -> str:
    return "plate" if not variant or variant == MAIN else f"plate_{variant}"


def lock_record(root: Path, scene_id: str, variant: str = MAIN) -> dict | None:
    """某个变体的定稿记录（文件仍是定稿时的那张才算）→ {by, at, sha256, file, version, …, path, plate_id}"""
    man = read_manifest(root, scene_id)
    rec = man.get("locked") if variant == MAIN else (man.get("locked_variants") or {}).get(variant)
    if variant == MAIN:
        fname = man.get("plate") or "plate.png"
        p = scenes_dir(root) / scene_id / fname
        sha = sha_file(p) if p.is_file() else None
        owner = bool(sha and man.get("owner_confirmed_at") and str(man.get("owner_confirmed_ref") or "").strip()
                     and man.get("plate_sha256") == sha)
        if isinstance(rec, dict) and rec.get("sha256") and rec.get("sha256") == sha:
            return {**rec, "file": fname, "path": p.relative_to(root).as_posix(), "plate_id": f"{scene_id}/{fname}",
                    "owner_confirmed": owner}
        if owner:
            return {"by": "owner", "at": man.get("owner_confirmed_at"), "sha256": sha, "file": fname,
                    "path": p.relative_to(root).as_posix(), "plate_id": f"{scene_id}/{fname}", "owner_confirmed": True}
        return None
    if not isinstance(rec, dict) or not rec.get("file"):
        return None
    p = scenes_dir(root) / scene_id / rec["file"]
    sha = sha_file(p) if p.is_file() else None
    if not sha or sha != rec.get("sha256"):
        return None
    return {**rec, "path": p.relative_to(root).as_posix(), "plate_id": f"{scene_id}/{rec['file']}",
            "owner_confirmed": False}


# ------------------------------------------------------------------ 镜头 → 场景 / 底图

_GROUPS: dict[int, tuple[object, dict[str, str]]] = {}


def scene_map(script) -> dict[str, str]:
    """shot_id → scene_id（剧本 **场景** 优先，其余 group_scenes 推断；按剧本对象缓存）"""
    hit = _GROUPS.get(id(script))
    if hit and hit[0] is script:
        return hit[1]
    from ns_agent.state_timeline import group_scenes
    try:
        from ns_agent.looks import LookStore
        res = LookStore().resolver()
        present = lambda s: {c for c, _ in res.characters(s)}   # noqa: E731
        m = {r["shot_id"]: r["scene_id"] for r in group_scenes(script, present)}
    except Exception:  # noqa: BLE001  造型数据缺失时不看入镜角色（只影响房间归属推断）
        m = {r["shot_id"]: r["scene_id"] for r in group_scenes(script)}
    _GROUPS.clear()
    _GROUPS[id(script)] = (script, m)
    return m


def shot_scene_id(script, shot_id: str) -> str | None:
    for ep in script.episodes:
        for s in ep.shots:
            if s.id == shot_id and getattr(s, "scene_id", None):
                return s.scene_id
    return scene_map(script).get(shot_id)


def shot_plate(script, shot_id: str, root: Path, renders_dir: Path) -> dict | None:
    """本镜首帧要带的场景底图 → {scene_id, plate_id, path（相对项目根）, sha256, source: override|locked, by?}；
    没有 / 画布选了“不用底图” → None"""
    scene_id = shot_scene_id(script, shot_id)
    if not scene_id:
        return None
    from ns_agent.ref_overrides import effective_scene, load_canvas, plate_path
    ep = int(shot_id[2:4]) if shot_id.startswith("ep") and shot_id[2:4].isdigit() else 0
    try:
        plate_id, src = effective_scene(load_canvas(renders_dir), ep, scene_id, shot_id)
    except Exception:  # noqa: BLE001
        plate_id, src = None, "episode"
    if plate_id == "none":
        return None
    if plate_id:
        p = plate_path(root, plate_id)
        if p is None:
            return None
        return {"scene_id": scene_id, "plate_id": plate_id, "path": p.relative_to(Path(root).resolve()).as_posix()
                if p.is_relative_to(Path(root).resolve()) else p.as_posix(),
                "sha256": sha_file(p), "source": "override", "layer": src}
    rec = lock_record(root, scene_id)
    if not rec:
        return None
    return {"scene_id": scene_id, "plate_id": rec["plate_id"], "path": rec["path"], "sha256": rec["sha256"],
            "source": "locked", "by": rec.get("by")}


def stale_reasons(take: dict, root: Path) -> list[str]:
    """首帧版本带过场景底图，而那张底图文件现在不是当时那张（换了定稿版本）→ ["场景底图已改"]"""
    sp = take.get("scene_plate") if isinstance(take, dict) else None
    if not isinstance(sp, dict) or not sp.get("path") or not sp.get("sha256"):
        return []
    p = Path(root) / sp["path"]
    return [STALE_REASON] if sha_file(p) != sp["sha256"] else []


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
