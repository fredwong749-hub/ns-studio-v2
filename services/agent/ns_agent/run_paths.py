"""按剧取路径（AUDIT-M I-14，PROMPTFIX 11-02 19:0x；L-125 / L-126 V2 完全切割）。

ns_agent 的编译 / 造型 / 时间线 / 快照模块不许再写死 data/import/script_v2.json、data/looks、data/renders——
一律经这里转到 FACTORY 的唯一入口 tools/autopilot/runs.py（script_path / looks_path / renders_dir / asset_manifest）。
- root 下没有 config/pipeline/runs.json（沙箱 / 测试临时目录）→ 旧路径（行为不变）；
- 剧 = 环境变量 NS_RUN（多剧并行 run_env，K-OPS-E15）或 runs.json 的 primary；
- NS_SCRIPT_JSON 环境变量仍优先（与 ns_render.pipeline.SCRIPT_JSON 同一口径）；
- 失败关闭：非 legacy 剧解析出的路径落在 V1 的旧位置（data/import/script_v2.json、data/looks、data/renders 根）→ raise。
"""
from __future__ import annotations

import importlib.util
import os
from functools import lru_cache
from pathlib import Path


class V1PathError(RuntimeError):
    """V2（非 legacy 剧）读到了 V1 的旧路径"""


@lru_cache(maxsize=8)
def _runs_mod(root: str):
    p = Path(root) / "tools" / "autopilot" / "runs.py"
    if not p.is_file():
        return None
    sp = importlib.util.spec_from_file_location(f"runs_for_ns_agent_{abs(hash(root))}", p)
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


def runs_module(root=None):
    """services 侧加载 tools/autopilot/runs.py 的唯一入口（OVERSIGHT-F P2：副本收口）；root 默认本仓库；没有 runs.py → None"""
    return _runs_mod(str(Path(root) if root else Path(__file__).resolve().parents[3]))


def _ctx(root) -> tuple:
    root = Path(root)
    if not (root / "config" / "pipeline" / "runs.json").is_file():
        return root, None, None, True
    m = _runs_mod(str(root))
    if m is None:
        return root, None, None, True
    run = m.current_run(root) if hasattr(m, "current_run") else (os.environ.get("NS_RUN") or m.primary_run(root))   # OVERSIGHT-F P2 统一认法
    return root, m, run, bool(m.is_legacy(run, root))


def _guard(root: Path, p: Path, legacy: bool, legacy_paths: tuple) -> Path:
    if not legacy and any(p.resolve() == (root / x).resolve() for x in legacy_paths):
        raise V1PathError(f"非 V1 剧解析到了 V1 路径 {p}（L-126 完全切割）")
    return p


def current_run(root) -> str | None:
    return _ctx(root)[2]


def script_json(root) -> Path:
    env = os.environ.get("NS_SCRIPT_JSON")
    if env:
        return Path(env)
    root, m, run, legacy = _ctx(root)
    if m is None:
        return root / "data" / "import" / "script_v2.json"
    return _guard(root, Path(m.script_path(run, root)), legacy, ("data/import/script_v2.json",))


def looks_dir(root) -> Path:
    root, m, run, legacy = _ctx(root)
    if m is None:
        return root / "data" / "looks"
    return _guard(root, Path(m.looks_path(run, root)), legacy, ("data/looks",))


def renders_dir(root) -> Path:
    env = os.environ.get("NS_RENDERS_DIR")
    if env:
        return Path(env)
    root, m, run, legacy = _ctx(root)
    if m is None:
        return root / "data" / "renders"
    return _guard(root, Path(m.renders_dir(run, root)), legacy, ("data/renders",))


def asset_manifest(root) -> Path | None:
    root, m, run, _legacy = _ctx(root)
    return Path(m.asset_manifest(run, root)) if m is not None and m.asset_manifest(run, root) else None


# ------------------------------------------------------------------ 状态表 / 场景表按剧存放（V2CUT-O 11-03，L-126）
class TimelineMissing(FileNotFoundError):
    """当前剧（非 V1）的状态表 / 场景表还没生成：直接报错，不许退回读 V1 的 data/state_timeline.json / data/scenes.json"""


def state_dir(root, run: str | None = None) -> Path:
    """状态表 / 场景表所在目录：没有 runs.json 的临时根 / legacy 剧（V1）= data/（V1 全局文件，原样保留）；
    其它剧 = 本剧导入稿所在目录 data/import/runs/<run>/（由 tools/build_state_timeline.py 按本剧导入稿生成）。
    run 不给 = 当前剧（NS_RUN / primary）"""
    root, m, cur, legacy = _ctx(root)
    if m is not None and run:
        legacy = bool(m.is_legacy(run, root))
    run = run or cur
    if m is None or legacy:
        return root / "data"
    d = Path(m.import_path(run, root)).parent
    return _guard(root, d, legacy, ("data", "data/import"))


def timeline_json(root, run: str | None = None) -> Path:
    return state_dir(root, run) / "state_timeline.json"


def scenes_json(root, run: str | None = None) -> Path:
    return state_dir(root, run) / "scenes.json"


def is_legacy_run(root) -> bool:
    """当前剧是不是 V1（或没有 runs.json 的临时根）"""
    return _ctx(root)[3]


def require_state(root, which: str = "timeline") -> Path:
    """当前剧的状态表（which=timeline）/ 场景表（which=scenes）路径；非 V1 剧且文件不存在 → TimelineMissing（失败关闭）"""
    p = timeline_json(root) if which == "timeline" else scenes_json(root)
    if not is_legacy_run(root) and not p.is_file():
        raise TimelineMissing(f"当前剧 {current_run(root)} 还没有 {p.name}（{p}）：先跑 "
                              f"python tools/build_state_timeline.py --run {current_run(root)}；不读 V1 的 data/{p.name}")
    return p
