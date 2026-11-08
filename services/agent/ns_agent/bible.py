"""
视觉圣经加载器（T06）：config/visual_bible.toml → 编译器 / 画面说明层用的规则对象，外加规则版本号 bible_version。

- 每个键缺失时退回 compiler.py 里的原常量（旧项目 / 新故事模板缺键也能编译，行为不变）。
- 项目根目录：参数 > 环境变量 NS_PROJECT_ROOT > 仓库根目录（T42 子进程可指向 projects/<slug>）。
- 按文件 mtime 缓存，改 toml 后下一次编译自动生效。

    from ns_agent.bible import load_bible, bible_version
    b = load_bible()                 # 当前项目
    bible_version()                  # 'a1b2c3d4e5f6'（12 位）
"""
import hashlib
import json
import os
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
# 编译器拼装逻辑（不在 toml 里的部分）改动时手动递增，让 bible_version 跟着变
RULES_CODE_REV = "t06.1"
VERSION_LEN = 12


def project_root(root: str | Path | None = None) -> Path:
    return Path(root or os.environ.get("NS_PROJECT_ROOT") or REPO)


# ------------------------------------------------------------------ 规则对象

@dataclass(frozen=True)
class Material:
    id: str
    match: tuple[str, ...]
    term: str
    character: str = ""          # 非空：该角色入镜才追加


@dataclass(frozen=True)
class ShotRule:
    id: str
    match: tuple[str, ...]
    term: str


@dataclass(frozen=True)
class CharacterRule:
    id: str                      # slug，如 lin_wan
    name: str                    # 中文名，与剧本角色锁一致
    detect: tuple[str, ...]
    she_trigger: str = ""        # 首帧写“她”且 motion 带此触发词 → 该角色
    identity_extra: str = ""     # 追加在剧本角色锁之后（项目方确认后填写）
    raw: dict = field(default_factory=dict, compare=False, hash=False)


@dataclass
class Bible:
    root: Path
    path: Path
    exists: bool
    data: dict
    image_style_fallback: str
    lens_template: str
    lens_default: tuple[str, str]
    lens_skip_if: tuple[str, ...]
    video_style: str
    video_tail: str
    video_negative: str
    style_negative: str
    last_frame_keep: str
    banned_words: tuple[str, ...]
    materials: tuple[Material, ...]
    shot_rules: tuple[ShotRule, ...]
    safe_term: str
    safe_negative: str
    characters: tuple[CharacterRule, ...]
    minor_guard: str
    extra_minor_guards: tuple[str, ...]
    overrides_path: Path
    looks_inject: bool
    looks_inject_slots: tuple[str, ...]

    # ---- 派生
    @property
    def dark_words(self) -> tuple[str, ...]:
        r = self.rule("dark")
        return r.match if r else ()

    @property
    def dark_term(self) -> str:
        r = self.rule("dark")
        return r.term if r else ""

    def rule(self, rid: str) -> ShotRule | None:
        return next((r for r in self.shot_rules if r.id == rid), None)

    def character(self, name: str) -> CharacterRule | None:
        return next((c for c in self.characters if c.name == name or c.id == name), None)

    @property
    def character_hints(self) -> dict[str, tuple[str, ...]]:
        return {c.name: c.detect for c in self.characters}

    def forced_adult(self) -> dict[str, str]:
        v = _read_overrides(self.overrides_path).get("force_adult", {})
        # 封板修复：写成列表 ["ep01-S1", …] 时按“原因未写”处理（原来编译器 forced[shot.id] 会 TypeError）
        if isinstance(v, list):
            return {str(x): "（未写原因）" for x in v}
        return v if isinstance(v, dict) else {}

    def adult_per_shot(self) -> bool:
        """adult_overrides.json "per_shot"：true = 逐镜判定；缺省 false = 集标题 🔴 整集继承（项目方 2026-10-30）"""
        v = _read_overrides(self.overrides_path).get("per_shot", False)
        # 封板修复：只认 JSON 布尔 true（或字符串 "true"）；原来 bool("false") 为 True，会悄悄切成逐镜判定
        return v is True or (isinstance(v, str) and v.strip().lower() == "true")

    @property
    def version(self) -> str:
        """编译器每镜调用：0.5 秒内复用上次结果（全剧编译时省掉上千次文件 stat）；需要即时值用 bible_version()"""
        now = time.monotonic()
        hit = _VERSION_TTL.get(str(self.root))
        if hit and now - hit[0] < VERSION_TTL_S:
            return hit[1]
        v = bible_version(self.root)
        _VERSION_TTL[str(self.root)] = (now, v)
        return v

    def scrub(self, text: str) -> str:
        """去掉禁用词（只用于系统追加的文字；剧本原文与项目方导演加强从不改动）"""
        for w in sorted(self.banned_words, key=len, reverse=True):
            text = text.replace(w, "")
        return text

    def banned_hits(self, text: str) -> list[str]:
        return [w for w in self.banned_words if w and w in text]


def _read_overrides(p: Path) -> dict:
    """adult_overrides.json 整个对象（按 mtime/size 缓存）；读不了 → {}"""
    try:
        st = p.stat()
    except OSError:
        return {}
    key = (str(p), st.st_mtime_ns, st.st_size)
    hit = _OVR_CACHE.get(str(p))
    if hit and hit[0] == key:
        return hit[1]
    try:
        val = json.loads(p.read_text(encoding="utf-8"))
        val = val if isinstance(val, dict) else {}
    except (OSError, ValueError):
        val = {}
    _OVR_CACHE[str(p)] = (key, val)
    return val


_OVR_CACHE: dict = {}
_VERSION_TTL: dict = {}
VERSION_TTL_S = 0.5
_BIBLE_CACHE: dict = {}


def _defaults():
    from . import compiler as C   # 常量是兜底值（延迟导入，避免循环）
    return C


def _tup(v, default) -> tuple:
    if v is None:
        return tuple(default)
    return tuple(v) if isinstance(v, (list, tuple)) else (v,)


def _check_types(path: Path, data: dict) -> None:
    """封板修复：类型写错时和坏 TOML 一样报清楚的 ValueError（原来是 IndexError / AttributeError，看不出哪里错）"""
    def bad(key, want):
        raise ValueError(f"{path} 的 {key} 必须是{want}")
    for key in ("look", "realism", "assembly", "classification", "looks", "characters", "shot_rules_safe"):
        if key in data and not isinstance(data[key], dict):
            bad(f"[{key}]", "表（table）")
    for key in ("materials", "shot_rules"):
        if key in data and not (isinstance(data[key], list) and all(isinstance(x, dict) for x in data[key])):
            bad(f"[[{key}]]", "表数组（[[...]]）")
    ld = (data.get("look") or {}).get("lens_default")
    if ld is not None and not (isinstance(ld, (list, tuple)) and len(ld) == 2):
        bad("[look].lens_default", '两个元素的数组，如 ["50", "1.4"]')


def _build(root: Path, path: Path, data: dict) -> Bible:
    C = _defaults()
    _check_types(path, data)
    look = data.get("look") or {}
    video = look.get("video") or {}
    realism = data.get("realism") or {}
    assembly = data.get("assembly") or {}
    cls = data.get("classification") or {}
    looks_cfg = data.get("looks") or {}

    mats = data.get("materials")
    if mats is None:
        materials = (Material("toenails_lin_wan", C.TOENAIL_WORDS, C.TOENAIL_TERM, "林晚"),
                     Material("patent_gloss", C.PATENT_WORDS, C.PATENT_TERM),
                     Material("needle_heel", C.HEEL_WORDS, C.HEEL_TERM))
    else:
        materials = tuple(Material(str(m.get("id", "")), _tup(m.get("match"), ()), str(m.get("term", "")),
                                   str(m.get("character", "") or "")) for m in mats if m.get("term"))

    rules = data.get("shot_rules")
    if rules is None:
        shot_rules = (ShotRule("closeup", C.CLOSEUP_WORDS, C.CLOSEUP_TERM), ShotRule("dark", C.DARK_WORDS, C.DARK_TERM))
    else:
        shot_rules = tuple(ShotRule(str(r.get("id", "")), _tup(r.get("match"), ()), str(r.get("term", "")))
                           for r in rules if r.get("term"))
    safe = data.get("shot_rules_safe") or {}

    chars_cfg = data.get("characters")
    if chars_cfg is None:
        characters = tuple(CharacterRule(sid, name, hints, "lnwan01" if name == "林晚" else "")
                           for sid, (name, hints) in zip(("lin_wan", "gu_chen", "chen_yu"), C.CHARACTER_HINTS.items()))
    else:
        characters = tuple(CharacterRule(cid, str(c.get("name") or cid), _tup(c.get("detect"), (c.get("name") or cid,)),
                                         str(c.get("detect_she_with_trigger", "") or ""),
                                         str(c.get("identity_extra", "") or ""), dict(c))
                           for cid, c in chars_cfg.items() if isinstance(c, dict))

    guard = str(cls.get("minor_guard") or C.MINOR_GUARD)
    overrides = cls.get("overrides") or "config/adult_overrides.json"
    lens_default = look.get("lens_default") or ["50", "1.4"]
    return Bible(
        root=root, path=path, exists=path.exists(), data=data,
        image_style_fallback=str(look.get("fallback_suffix") or C.DEFAULT_IMAGE_STYLE),
        lens_template=str(look.get("lens_template") or C.LENS_TEMPLATE),
        lens_default=(str(lens_default[0]), str(lens_default[1])),
        lens_skip_if=_tup(look.get("lens_skip_if"), ("Leica", "徕卡")),
        video_style=str(video.get("style") or C.VIDEO_STYLE),
        video_tail=str(video.get("tail") or C.VIDEO_TAIL),
        video_negative=str(video.get("negative") or C.VIDEO_NEGATIVE),
        style_negative=str(look.get("style_negative") or C.STYLE_NEGATIVE),
        last_frame_keep=str(assembly.get("last_frame_keep") or C.LAST_FRAME_KEEP),
        banned_words=_tup(realism.get("banned_words"), ()),
        materials=materials, shot_rules=shot_rules,
        safe_term=str(safe.get("term") or C.SAFE_SHOT_TERM),
        safe_negative=str(safe.get("negative") or C.SAFE_SHOT_NEGATIVE),
        characters=characters,
        # 硬规则：内置“未成年人外观”永远在；toml 写了别的词就一起加，不能替换掉内置词
        minor_guard=C.MINOR_GUARD,
        extra_minor_guards=tuple(x for x in (guard,) if x != C.MINOR_GUARD),
        overrides_path=(root / overrides) if not Path(overrides).is_absolute() else Path(overrides),
        looks_inject=bool(looks_cfg.get("inject", False)),
        looks_inject_slots=_tup(looks_cfg.get("slots"), ("outfit", "shoes", "makeup", "hair")),
    )


class V1BibleRefused(RuntimeError):
    """资产包模式（V2）的剧没有登记自己的编译规则文件——不退回 V1 的 visual_bible（L-126）"""


def bible_path(root: str | Path | None = None) -> Path:
    """本剧编译规则文件：资产包模式（V2）= config/compile/run_style.json runs.<剧>.bible（只有技术纠正规则）；
    没登记 → V1BibleRefused（失败关闭）；其它剧 = config/visual_bible.toml（PROMPTFIX 21:1x，总调度：V2 编译完全不读 V1 圣经）"""
    root = project_root(root)
    try:
        from . import run_paths as RP
        from . import asset_refs as AR
        run = RP.current_run(root)
        if run and AR.project_cfg(run) and AR.manifest_rel(run):
            rs = root / "config" / "compile" / "run_style.json"
            ent = ((json.loads(rs.read_text(encoding="utf-8")).get("runs") or {}).get(run) or {}) if rs.is_file() else {}
            if not ent.get("bible"):
                raise V1BibleRefused(f"{run} 是资产包模式，config/compile/run_style.json 没有它的 bible；不读 V1 visual_bible（L-126）")
            return root / ent["bible"]
    except V1BibleRefused:
        raise
    except ImportError:   # arch-ok: 沙箱里没有 ns_agent 其它模块时按旧路径（只影响没有资产包配置的旧剧）
        pass
    return root / "config" / "visual_bible.toml"


def load_bible(root: str | Path | None = None, path: str | Path | None = None) -> Bible:
    root = project_root(root)
    path = Path(path) if path else bible_path(root)
    try:
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    cached = _BIBLE_CACHE.get((str(root), str(path)))
    if cached and cached[0] == key:
        return cached[1]
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8")) if key else {}
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{path} 不是合法 TOML（{e}）") from e
    b = _build(root, path, data)
    _BIBLE_CACHE[(str(root), str(path))] = (key, b)
    return b


# ------------------------------------------------------------------ bible_version

def _sha_file(p: Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def _sha_json(p: Path) -> str | None:
    """JSON 文件按规范化内容哈希（sort_keys、ensure_ascii=False）：CRLF/LF、缩进、键序不影响版本。
    不是合法 JSON 时退回原始字节哈希"""
    try:
        text = p.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return _sha_file(p)
    try:
        canon = json.dumps(json.loads(text), sort_keys=True, ensure_ascii=False)
    except ValueError:
        return _sha_file(p)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _normalized_toml(p: Path) -> str | None:
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except OSError:
        return None
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{p} 不是合法 TOML（{e}）：修好后才能计算 bible_version") from e
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
                          .encode("utf-8")).hexdigest()


_SRC_CACHE: dict = {}


def _stat_key(paths: list[Path]):
    out = []
    for p in paths:
        try:
            st = p.stat()
            out.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((str(p), None, None))
    return tuple(out)


def source_hashes(project_root_: str | Path | None = None) -> dict[str, str | None]:
    """bible_version 的全部输入（gpu3 可据此判断是哪一份输入变了）"""
    root = project_root(project_root_)
    bible = bible_path(root)                              # V2 = 本剧规则文件（不读 V1 圣经）
    from . import run_paths as RP                         # I-14
    script = RP.script_json(root)
    own = root / "config" / "shot_ownership.json"
    looks_dir = RP.looks_dir(root)
    looks = sorted(looks_dir.glob("*.json")) if looks_dir.is_dir() else []
    key = _stat_key([bible, script, own, *looks])
    hit = _SRC_CACHE.get(str(root))
    if hit and hit[0] == key:
        return dict(hit[1])
    out: dict[str, str | None] = {"visual_bible": _normalized_toml(bible), "script": _sha_json(script)}
    for p in looks:
        out[f"looks/{p.name}"] = _sha_json(p)
    out["shot_ownership"] = _sha_json(own)
    out["rules_code"] = RULES_CODE_REV
    _SRC_CACHE[str(root)] = (key, out)
    return dict(out)


def bible_version(project_root: str | Path | None = None) -> str:
    """规则版本短哈希：visual_bible.toml（解析后规范化）+ script_v2.json + data/looks/*.json + shot_ownership.json
    + 编译器规则代码版本。任一变化 → 版本变化。"""
    h = source_hashes(project_root)
    v = hashlib.sha256(json.dumps(h, sort_keys=True).encode()).hexdigest()[:VERSION_LEN]
    _VERSION_TTL[str(globals()["project_root"](project_root))] = (time.monotonic(), v)
    return v
