"""
本镜参考切换（canvas-ui-spec-v3 §6，W5）：三层继承 集默认 → 场景 → 镜头。

存放（契约 docs/api_v1_contract.md §E）：
    data/renders/epXX/SN/edits.json         ShotEdit.ref_overrides
    data/renders/epXX/episode_edit.json     EpisodeEdit.scene_overrides {scene_id: RefOverrides}
    data/renders/ref_overrides.json         上面两处的汇总索引（解析器只读这一份，避免每次扫上千个 edits.json；
                                            由 API 写覆盖时同步更新，rebuild_index() 可从源文件重建）

解析：data/looks 的 默认 → 集计划 → [场景覆盖] → 本镜覆盖(schedule) → [画布本镜覆盖]（ns_agent.looks.Resolver）。
look → LookPatch.look_id；parts → LookPatch.slots {槽位: [单品 id]}。场景底图覆盖只进 inputs_sha（state_timeline）。
只读写 JSON，从不触发渲染。
"""
import json
import os
import re
import threading
from pathlib import Path

from ns_schema.edits import RefOverrides
from ns_schema.looks import LookPatch

INDEX_NAME = "ref_overrides.json"
_SHOT_DIR_RE = re.compile(r"^ep(\d+)$")
_lock = threading.RLock()


def index_path(renders_dir: Path | str) -> Path:
    return Path(renders_dir) / INDEX_NAME


def empty_index() -> dict:
    return {"version": 1, "shots": {}, "scenes": {}}


def load_index(renders_dir: Path | str) -> dict:
    """→ {"shots": {shot_id: RefOverrides}, "scenes": {"<ep>": {scene_id: RefOverrides}}}；文件缺失 / 坏了 = 空"""
    try:
        raw = json.loads(index_path(renders_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    out = {"shots": {}, "scenes": {}}
    for sid, v in (raw.get("shots") or {}).items():
        try:
            ro = RefOverrides.model_validate(v)
        except ValueError:
            continue
        if not ro.is_empty():
            out["shots"][sid] = ro
    for ep, scenes in (raw.get("scenes") or {}).items():
        if not isinstance(scenes, dict):
            continue
        for sc, v in scenes.items():
            try:
                ro = RefOverrides.model_validate(v)
            except ValueError:
                continue
            if not ro.is_empty():
                out["scenes"].setdefault(str(int(ep)), {})[sc] = ro
    return out


def _write(renders_dir: Path, data: dict) -> None:
    p = index_path(renders_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def _dump(idx: dict) -> dict:
    return {"version": 1,
            "shots": {k: v.model_dump(exclude_none=True) for k, v in sorted(idx["shots"].items())},
            "scenes": {ep: {k: v.model_dump(exclude_none=True) for k, v in sorted(sc.items())}
                       for ep, sc in sorted(idx["scenes"].items(), key=lambda kv: int(kv[0])) if sc}}


def set_shot(renders_dir: Path | str, shot_id: str, ro: RefOverrides | None) -> None:
    with _lock:
        idx = load_index(renders_dir)
        if ro is None or ro.is_empty():
            idx["shots"].pop(shot_id, None)
        else:
            idx["shots"][shot_id] = ro
        _write(Path(renders_dir), _dump(idx))


def set_episode_scenes(renders_dir: Path | str, ep: int, scenes: dict[str, RefOverrides]) -> None:
    with _lock:
        idx = load_index(renders_dir)
        clean = {k: v for k, v in scenes.items() if not v.is_empty()}
        if clean:
            idx["scenes"][str(ep)] = clean
        else:
            idx["scenes"].pop(str(ep), None)
        _write(Path(renders_dir), _dump(idx))


def rebuild_index(renders_dir: Path | str) -> dict:
    """从 edits.json / episode_edit.json 重建索引（人工改过源文件后用）"""
    renders_dir = Path(renders_dir)
    idx = {"shots": {}, "scenes": {}}
    for ep_dir in sorted(renders_dir.glob("ep*")):
        m = _SHOT_DIR_RE.match(ep_dir.name)
        if not m or not ep_dir.is_dir():
            continue
        ep = int(m.group(1))
        try:
            ee = json.loads((ep_dir / "episode_edit.json").read_text(encoding="utf-8"))
            for sc, v in ((ee or {}).get("scene_overrides") or {}).items():
                ro = RefOverrides.model_validate(v)
                if not ro.is_empty():
                    idx["scenes"].setdefault(str(ep), {})[sc] = ro
        except (OSError, ValueError, AttributeError):
            pass
        for sd in sorted(ep_dir.glob("S*")):
            try:
                e = json.loads((sd / "edits.json").read_text(encoding="utf-8"))
                ro = RefOverrides.model_validate((e or {}).get("ref_overrides") or {})
            except (OSError, ValueError, AttributeError):
                continue
            if not ro.is_empty():
                idx["shots"][f"{ep_dir.name}-{sd.name}"] = ro
    with _lock:
        _write(renders_dir, _dump(idx))
    return idx


def to_patch(ro: RefOverrides | None, char: str) -> LookPatch | None:
    """一层覆盖里某人物的部分 → LookPatch（look_id + slots）；没有 → None"""
    if not ro:
        return None
    c = ro.characters.get(char)
    if not c or c.is_empty():
        return None
    return LookPatch(look_id=c.look or None, slots={k: [v] for k, v in c.parts.items()} or None)


class Canvas:
    """解析器用的画布覆盖：shot_id → 本镜覆盖；(集, scene_id) → 场景覆盖。scene_of 由解析器按需算"""

    def __init__(self, idx: dict | None = None):
        idx = idx or {"shots": {}, "scenes": {}}
        self.shots: dict[str, RefOverrides] = idx.get("shots") or {}
        self.scenes: dict[str, dict[str, RefOverrides]] = idx.get("scenes") or {}

    def __bool__(self) -> bool:
        return bool(self.shots or self.scenes)

    def has_scenes(self, ep: int) -> bool:
        return bool(self.scenes.get(str(ep)))

    def scene(self, ep: int, scene_id: str | None) -> RefOverrides | None:
        return self.scenes.get(str(ep), {}).get(scene_id) if scene_id else None

    def shot(self, shot_id: str | None) -> RefOverrides | None:
        return self.shots.get(shot_id) if shot_id else None


def load_canvas(renders_dir: Path | str) -> Canvas:
    return Canvas(load_index(renders_dir))


def effective_scene(canvas: Canvas | None, ep: int, scene_id: str | None, shot_id: str | None
                    ) -> tuple[str | None, str]:
    """场景底图覆盖 → (plate_id | "none" | None, 来源 shot|scene|episode)；None = 沿用场景默认底图"""
    if canvas:
        ro = canvas.shot(shot_id)
        if ro and ro.scene:
            return ro.scene, "shot"
        ro = canvas.scene(ep, scene_id)
        if ro and ro.scene:
            return ro.scene, "scene"
    return None, "episode"


def plate_path(root: Path | str, plate_id: str | None) -> Path | None:
    """plate_id（assets/scenes 下的相对路径）→ 文件；"none" / 越界 / 不存在 → None"""
    if not plate_id or plate_id == "none":
        return None
    base = (Path(root) / "assets" / "scenes").resolve()
    p = (base / plate_id).resolve()
    return p if p.is_relative_to(base) and p.is_file() else None


def item_image_file(item, root: Path | str) -> str | None:
    """单品主图（相对项目根）：最新版本的 images，其次 reference_images 里的真实文件"""
    if item is None:
        return None
    root = Path(root)
    imgs = sorted((im for im in item.images if (root / im.file).is_file()), key=lambda im: -im.version)
    if imgs:
        return imgs[0].file
    return next((r for r in item.reference_images if (root / r).is_file()), None)


def canvas_part_images(res, char: str, ep: int, shot_id: str, root: Path | str) -> dict[str, str]:
    """画布覆盖换过的槽位 → 该槽位单品的图（相对项目根）；给参考图选择（ns_render.refs.plan_refs）优先用"""
    if not hasattr(res, "canvas_slots"):
        return {}
    r = res.core(char, ep, shot_id)
    out: dict[str, str] = {}
    for slot in res.canvas_slots(char, ep, shot_id):
        for iid in (r.slots[slot].items if slot in r.slots else []):
            f = item_image_file(res.items.get(iid), root)
            if f:
                out[slot] = f
                break
    return out
