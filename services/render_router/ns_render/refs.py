"""
参考图选择（research v6 §4.2）：每镜只带 1 张角色身份底板，不再用项目方的成品定妆图（会照搬姿势/服装/首饰/背景）。

资产目录（另一会话持续补充，出现即生效）：
  assets/characters/<id>/{plates,expressions,items/<kind>,looks}/manifest.json
manifest 条目至少有 "file"；可选字段：
  design   妆容编号 M0..M6（items/makeup）
  angle    F | 34L | 34R | P | B（plates：正脸/3/4 左右/正侧/背面）
  makeup   底板对应的妆容编号（没有 = 任何妆容都可用）
  look_id  造型 id（looks：上身照）
  kind     如 "item/makeup" "plate/face" "item/nails" "item/shoes" "look/full"
  selected / locked  人工选定（优先）；rejected 为 true 的条目跳过

妆容 → 底板：data/looks 解析结果 lw-makeup-bare → M1（素颜通勤 REF-LW-01），lw-makeup-heavy → M4（浓妆女王 REF-LW-02），
其余 M0（定妆原版甜妆）；镜头原文写“减半红唇/减半” → M2（ep06 过渡妆），写“正红唇” → M3，写“泪痕/流泪/泪水/哭” → M6。
"""
import hashlib
import json
from dataclasses import dataclass, field
import os
from pathlib import Path

ROOT = Path(os.environ.get("NS_PROJECT_ROOT") or Path(__file__).resolve().parents[3])  # T42：子进程可指向 projects/<slug>
CHAR_DIR = ROOT / "assets" / "characters"
CACHE = ROOT / "data" / "cache" / "refs"

CHAR_IDS = {"林晚": "lin_wan", "顾沉": "gu_chen", "陈宇": "chen_yu"}
MAKEUP_BY_ITEM = {"lw-makeup-bare": "M1", "lw-makeup-heavy": "M4"}
MAKEUP_DEFAULT = "M0"
# 镜头原文关键词覆盖（按顺序，先命中先用）
MAKEUP_BY_TEXT = (("减半", "M2"), ("正红唇", "M3"), ("泪痕", "M6"), ("流泪", "M6"), ("泪水", "M6"), ("哭", "M6"))
BACK_WORDS = ("背影", "背对", "背抵", "后脑")
PROFILE_WORDS = ("侧脸", "侧影", "侧面")
CLOSE_WORDS = ("特写", "极近", "近景")
WIDE_WORDS = ("全景", "全身", "远景", "中景")
HAND_WORDS = ("手指", "指尖", "指腹", "指节", "手部", "手背", "美甲", "指甲")
FOOT_WORDS = ("高跟", "鞋", "脚", "足弓", "脚趾")

FACE_AREA = 768          # 脸部底板预缩放到约 768²
ITEM_LONG = 640          # 单品长边 512–768
LOOK_SIZE = (768, 1344)
MAX_REFS = 4             # Qwen 最多 4 张（Klein 3 张）

# 参考图作用说明（research v6 §4.2 骨架 + 商用指南“先声明每张图的角色”）
ROLE_FACE = ("图{i}只提供{name}的面部身份与发型：五官、脸型、发色发型、肤色与妆容；"
             "不要沿用图{i}的姿势、表情、服装、首饰、背景、构图和光线。")
ROLE_LOOK = "图{i}只提供{name}的服装款式与材质，不要沿用图{i}的姿势和背景。"
ROLE_NAILS = "图{i}只提供指甲的甲型、长度、透明度与钻饰布局，不要沿用其手势、肤色和背景。"
ROLE_SHOES = "图{i}只提供鞋子款式与材质，不要沿用其角度和背景。"
ROLE_TAIL = "画面内容完全按以下描述："
# S3（fredw-92/S-back）：本镜场景的定稿底图，排在人物参考之后；系统生成的用途说明（与上面同一机制）
ROLE_SCENE = "图{i}只取场景空间与陈设，不取人物。"
SCENE_LONG = 1024        # 场景底图预缩放长边


@dataclass
class RefImage:
    path: Path
    kind: str                 # face | look | nails | shoes | scene
    character: str = ""
    design: str = ""
    angle: str = ""
    size: tuple[int, int] = (0, 0)   # 预缩放尺寸


@dataclass
class RefPlan:
    refs: list[RefImage] = field(default_factory=list)
    makeup: str = ""
    note: str = ""

    @property
    def face(self) -> RefImage | None:
        return next((r for r in self.refs if r.kind == "face"), None)

    def role_lines(self, start: int = 1, tail: str = ROLE_TAIL) -> str:
        lines = []
        for i, r in enumerate(self.refs, start):
            tpl = {"face": ROLE_FACE, "look": ROLE_LOOK, "nails": ROLE_NAILS, "shoes": ROLE_SHOES,
                   "scene": ROLE_SCENE}[r.kind]
            lines.append(tpl.format(i=i, name=r.character))
        return "".join(lines) + tail if lines else ""

    @property
    def scene(self) -> RefImage | None:
        return next((r for r in self.refs if r.kind == "scene"), None)

    def with_scene(self, path: Path | None) -> "RefPlan":
        """S3：场景底图排在人物参考之后（已有就不重复）"""
        if path is None or self.scene is not None:
            return self
        return RefPlan([*self.refs, RefImage(Path(path), "scene")], self.makeup, self.note)

    def without_face(self) -> "RefPlan":
        return RefPlan([r for r in self.refs if r.kind != "face"], self.makeup, self.note + "；去掉脸部底板")


def _manifests(char_id: str) -> list[tuple[Path, dict]]:
    base = CHAR_DIR / char_id
    out = []
    for mf in sorted(base.rglob("manifest.json")) if base.exists() else []:
        try:
            entries = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(entries, dict):
            entries = entries.get("items") or entries.get("assets") or []
        for e in entries:
            # superseded：被 v2 资产取代的旧图（tools/gen_assets_v2.py）不再当参考
            if (isinstance(e, dict) and e.get("file") and not e.get("rejected") and not e.get("superseded")
                    and (mf.parent / e["file"]).exists()):
                out.append((mf.parent / e["file"], e))
    return out


def _sub(path: Path, char_id: str) -> str:
    """资产所在子目录：plates / expressions / items / looks"""
    return path.relative_to(CHAR_DIR / char_id).parts[0]


def _rank(e: dict, path: Path) -> tuple:
    return (not e.get("locked"), not e.get("selected"), not path.stem.endswith("_s0"), path.name)


def makeup_design(makeup_items: list[str], text: str) -> str:
    for word, design in MAKEUP_BY_TEXT:
        if word in text:
            return design
    for item in makeup_items:
        if item in MAKEUP_BY_ITEM:
            return MAKEUP_BY_ITEM[item]
    return MAKEUP_DEFAULT


def shot_angle(text: str) -> str:
    if any(w in text for w in BACK_WORDS):
        return "B"
    if any(w in text for w in PROFILE_WORDS):
        return "P"
    return "F"


# 项目方指定的身份参考（2026-10-30：“我喜欢我给你的 linwan 参考图”）：林晚的身份统一用项目方参考图的脸部裁切
# （tools/char_assets/refs/lw_face_closeup_768.png，取自 assets/characters/lin_wan/lin_wan.png，有来源记录），
# 优先于写实底板与 M0–M6。写实只作为质感（精修阶段）。设为 {} 可退回底板逻辑。
OWNER_FACE = {"lin_wan": ROOT / "tools" / "char_assets" / "refs" / "lw_face_closeup_768.png"}


def owner_face(char_id: str) -> tuple[Path, dict] | None:
    """项目方认可的身份参考：plates/master.png（2026-10-30 项目方通过的 CL_s0：参考图的样子 + 轻度写实）优先，
    其次参考图脸部裁切"""
    master = CHAR_DIR / char_id / "plates" / "master.png"
    if char_id in OWNER_FACE and master.exists():
        # 母版是带场景的成品照（玫瑰/项链/托腮）：只用脸和刘海的裁切做身份参考，避免把项链和服装带进镜头
        return face_crop(master), {"design": "master", "name": "项目方通过的母版（脸部裁切）", "owner": True,
                                   "source": master.relative_to(ROOT).as_posix()}
    p = OWNER_FACE.get(char_id)
    if p and p.exists():
        return p, {"design": "owner", "name": "项目方参考图脸部裁切", "owner": True}
    return None


def realistic_plate(char_id: str, design: str) -> tuple[Path, dict] | None:
    """写实底板（research v8 §5，资产会话制作中）：<design>_real.png（妆容写实版）优先，其次 plates/master.png。
    文件出现即生效，不要求已登记 manifest"""
    base = CHAR_DIR / char_id
    for p in (base / "items" / "makeup" / f"{design}_real.png", base / "plates" / f"{design}_real.png",
              base / "plates" / "master.png"):
        if p.exists():
            return p, {"design": design if "_real" in p.stem else "master", "name": p.stem, "realistic": True}
    return None


def face_plate(char_id: str, design: str, angle: str = "F") -> tuple[Path, dict] | None:
    """角度底板（plates/，angle 匹配且妆容匹配或不限）优先；正脸用写实底板（*_real / master），
    还没有写实底板时用妆容底板（items/makeup Mx_s0）"""
    assets = _manifests(char_id)
    if angle != "F":
        plates = [(p, e) for p, e in assets if _sub(p, char_id) == "plates" and e.get("angle") == angle
                  and e.get("makeup") in (None, "", design)]
        if plates:
            return min(plates, key=lambda pe: _rank(pe[1], pe[0]))
    owner = owner_face(char_id)
    if owner:
        return owner
    real = realistic_plate(char_id, design)
    if real:
        return real
    makeup = [(p, e) for p, e in assets if e.get("design") == design and "makeup" in (e.get("kind") or p.parent.name)
              and "_real" not in p.stem]
    if not makeup and design != MAKEUP_DEFAULT:
        makeup = [(p, e) for p, e in assets if e.get("design") == MAKEUP_DEFAULT]
    if makeup:
        return min(makeup, key=lambda pe: _rank(pe[1], pe[0]))
    fronts = [(p, e) for p, e in assets if _sub(p, char_id) == "plates" and e.get("angle") in (None, "", "F")]
    return min(fronts, key=lambda pe: _rank(pe[1], pe[0])) if fronts else None


def _item(char_id: str, kind: str) -> Path | None:
    hits = [(p, e) for p, e in _manifests(char_id)
            if kind in (e.get("kind") or "") or (_sub(p, char_id) == "items" and p.parent.name == kind)]
    return min(hits, key=lambda pe: _rank(pe[1], pe[0]))[0] if hits else None


def _look(char_id: str, look_id: str | None) -> Path | None:
    hits = [(p, e) for p, e in _manifests(char_id) if _sub(p, char_id) == "looks"
            and (not look_id or e.get("look_id") in (None, look_id))]
    return min(hits, key=lambda pe: _rank(pe[1], pe[0]))[0] if hits else None


def plan_refs(text: str, characters: list[str], looks: dict[str, dict] | None = None) -> RefPlan:
    """text: 镜头原文（img，必要时含 motion）；characters: 画面中出现的角色名（按重要性）；
    looks: {角色名: {"makeup": [...], "look_id": ...}}（data/looks 解析结果，可缺省）"""
    looks = looks or {}
    plan = RefPlan()
    for name in characters:
        cid = CHAR_IDS.get(name)
        if not cid:
            continue
        lk = looks.get(name, {})
        design = makeup_design(lk.get("makeup", []), text)
        angle = shot_angle(text)
        hit = face_plate(cid, design, angle)
        if not hit:
            continue
        path, e = hit
        plan.refs.append(RefImage(path, "face", name, e.get("design", design), e.get("angle", "F")))
        if not plan.makeup:
            plan.makeup = design
            plan.note = f"{name} 妆容 {design}（{e.get('name', path.name)}），机位 {angle}"
    lead = plan.refs[0].character if plan.refs else None
    lead_id = CHAR_IDS.get(lead or "")
    if lead_id:
        close = any(w in text for w in CLOSE_WORDS)
        # W5：项目方在画布上换过的单件（looks[名]["part_images"]，相对项目根）优先于资产目录里的默认图
        parts = {k: ROOT / v for k, v in (looks.get(lead, {}).get("part_images") or {}).items() if (ROOT / v).is_file()}
        if not close and any(w in text for w in WIDE_WORDS):
            p = parts.get("outfit") or _look(lead_id, looks.get(lead, {}).get("look_id"))
            if p:
                plan.refs.append(RefImage(p, "look", lead))
        if any(w in text for w in FOOT_WORDS):
            p = parts.get("shoes") or _item(lead_id, "shoes")
            if p:
                plan.refs.append(RefImage(p, "shoes", lead))
        if close and any(w in text for w in HAND_WORDS):
            p = parts.get("nails") or _item(lead_id, "nails")
            if p:
                plan.refs.append(RefImage(p, "nails", lead))
    # 优先级：脸 > Look > 鞋 > 甲；Qwen 最多 4 张
    order = {"face": 0, "look": 1, "shoes": 2, "nails": 3}
    plan.refs = sorted(plan.refs, key=lambda r: order[r.kind])[:MAX_REFS]
    return plan


def face_crop(src: Path, size: int = 768, expand: float = 1.6) -> Path:
    """脸部方形裁切（NudeNet 脸框外扩 expand 倍，略向上偏以包含刘海），按内容哈希缓存；找不到脸就用原图"""
    digest = hashlib.sha256(src.read_bytes()).hexdigest()[:12]
    out = CACHE / f"{src.stem}_face_{digest}_{size}_{expand}.png"
    if out.exists():
        return out
    from PIL import Image
    from .score import face_box
    box = face_box(src)
    if not box:
        return src
    x, y, w, h = box
    im = Image.open(src).convert("RGB")
    half = expand * max(w, h) / 2
    cx, cy = x + w / 2, y + h / 2 - 0.15 * h
    half = min(half, cx, cy + 0.15 * h, im.width - cx, im.height - cy) if half > min(im.width, im.height) / 2 else half
    crop = im.crop((int(max(0, cx - half)), int(max(0, cy - half)), int(min(im.width, cx + half)),
                    int(min(im.height, cy + half))))
    CACHE.mkdir(parents=True, exist_ok=True)
    crop.resize((size, size), Image.LANCZOS).save(out)
    return out


def _fit(w: int, h: int, area: int) -> tuple[int, int]:
    k = (area * area / (w * h)) ** 0.5
    return max(32, round(w * k / 32) * 32), max(32, round(h * k / 32) * 32)


def target_size(r: RefImage, w: int, h: int) -> tuple[int, int]:
    if r.kind == "face":
        return _fit(w, h, FACE_AREA)
    if r.kind == "look":
        return LOOK_SIZE
    k = (SCENE_LONG if r.kind == "scene" else ITEM_LONG) / max(w, h)
    return max(32, round(w * k / 32) * 32), max(32, round(h * k / 32) * 32)


def prescale(r: RefImage, scale: float = 1.0) -> Path:
    """预缩放到 v6 规格（32 的倍数），缓存到 data/cache/refs；scale<1 用于“参考图越小越不主导构图”的重试"""
    from PIL import Image
    im = Image.open(r.path).convert("RGB")
    w, h = target_size(r, *im.size)
    if scale != 1.0:
        w, h = max(32, round(w * scale / 32) * 32), max(32, round(h * scale / 32) * 32)
    digest = hashlib.sha256(r.path.read_bytes()).hexdigest()[:12]
    out = CACHE / f"{r.path.stem}_{digest}_{w}x{h}.png"
    if not out.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        im.resize((w, h), Image.LANCZOS).save(out)
    r.size = (w, h)
    return out
