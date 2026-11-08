"""多剧并行的 run 登记表（WORKFLOW 11-02，总调度批准；docs/AUTOPILOT.md §0.1；规则 K-OPS-W17）。

config/pipeline/runs.json：{"primary": "series10_v1", "runs": {"<run_id>": {"priority": 0, "status": "producing", "episode": N?,
"created_at": ISO, "source": "import|flow-start"}}}。config/pipeline/run.json 保留为 primary 的指针（旧读法兼容）。
导入新剧本 = register_run 追加一条（priority 默认 10），不改 primary、不替换 series10。

路径约定（K-OPS-F24：只有 legacy_unprefixed_run 那部剧用不带前缀的旧路径；primary 切到别的剧也一律带前缀）：
  导入稿      primary: data/import/script_v2.json              其它: data/import/runs/<run>/script_v2.json
  运行目录    data/pipeline/runs/<run>/（作业 assets/first_frame_jobs.json）
  ready       primary: data/autopilot/ready_episodes.json（并写 runs/<run>/ 一份）  其它: data/autopilot/runs/<run>/ready_episodes.json
  对象 id     primary: <kind>:<shot>（过渡期不加前缀）           其它: <run>/<kind>:<shot>
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "config/pipeline/runs.json"
RUN_PTR = ROOT / "config/pipeline/run.json"
DEFAULT_PRIMARY = "series10_v1"


def _jl(p: Path, d=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return d


def _js(p: Path, d, tries: int = 8) -> None:
    """原子写（tmp + os.replace）。W8 / A1-1：Windows 上 os.replace 撞上正在读的进程会 PermissionError（producer.js 早有同样重试），
    这里也退避重试，不让一次撞车把登记表写失败"""
    p.parent.mkdir(parents=True, exist_ok=True)
    t = p.with_suffix(p.suffix + f".{os.getpid()}.tmp")
    t.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    for i in range(tries):
        try:
            os.replace(t, p)
            return
        except PermissionError:
            if i == tries - 1:
                t.unlink(missing_ok=True)
                raise
            time.sleep(0.05 * (i + 1))


class RunRegistryMissing(RuntimeError):
    """服务器侧（strict=True）读不到 / 读坏 runs.json：失败关闭，不静默回退 DEFAULT_PRIMARY（K-OPS-F09 / K-OPS-W17）"""


_REG_CACHE: dict[str, tuple[tuple, dict]] = {}   # runs.json 路径 → ((mtime_ns, size), 解析结果)


def _jl_cached(p: Path):
    """runs.json 按 (mtime_ns, size) 记住一次解析（UIUX-LOOP-3 r11：/api/readiness 一次请求经 compiler / asset_refs / run_paths
    读 runs.json 4202 次，占 7.4 s 里的 5.4 s）。文件一换就重读；返回两层拷贝（顶层 + 每部剧的条目；deepcopy 一次 0.5 ms × 3000 次又回到秒级）——
    register_run / set_status 就地改条目再写回，不能改到缓存；条目里更深的 status_history 列表只由 set_status 追加（它读的是新鲜的 _jl）"""
    p = Path(p)
    try:
        st = p.stat()
    except OSError:
        _REG_CACHE.pop(str(p), None)
        return None
    key = (st.st_mtime_ns, st.st_size)
    hit = _REG_CACHE.get(str(p))
    if hit is None or hit[0] != key:
        d = _jl(p, None)
        if d is None:
            _REG_CACHE.pop(str(p), None)
            return None
        hit = (key, d)
        _REG_CACHE[str(p)] = hit      # B1（Fable 复审）：写完就用本地元组，不再回读字典——另一线程 pop 会 KeyError
    d = hit[1]
    if not isinstance(d, dict):
        return d
    return {**d, "runs": {k: (dict(v) if isinstance(v, dict) else v) for k, v in (d.get("runs") or {}).items()}}


def registry(root: Path | None = None, strict: bool = False) -> dict:
    root = root or ROOT
    d = _jl_cached(root / "config/pipeline/runs.json")
    if d and d.get("runs"):
        return d
    if strict:
        raise RunRegistryMissing(f"runs.json 缺失或无 runs：{root / 'config/pipeline/runs.json'}（strict：服务器侧不回退 {DEFAULT_PRIMARY}）")
    prim = (_jl(root / "config/pipeline/run.json", {}) or {}).get("run_id") or DEFAULT_PRIMARY
    return {"primary": prim, "runs": {prim: {"priority": 0, "status": "producing", "source": "import"}}}


def primary_run(root: Path | None = None, strict: bool = False) -> str:
    """strict=True：服务器侧 / 驱动服务器的工具用，runs.json 缺失、损坏或没写 primary 就 raise（失败关闭）。
    环境变量 NS_PRIMARY_RUN = 进程级显式钉剧（总调度 19:1x：读“真实当前剧”的测试一律钉到固定剧，不随 primary 切换漂移）"""
    pin = os.environ.get("NS_PRIMARY_RUN")
    if pin and (root is None or Path(root).resolve() == ROOT.resolve()):   # 只钉真实仓库；测试的临时目录照读自己的登记表
        return pin
    p = registry(root, strict=strict).get("primary")
    if not p and strict:
        raise RunRegistryMissing("runs.json 没写 primary（strict）")
    return p or DEFAULT_PRIMARY


_FOR_ROOT: dict = {}


def for_root(root: Path | str | None = None):
    """tools 侧加载 runs 的唯一入口（OVERSIGHT-F P2：加载器副本收口）：root 是本仓库 → 本模块；
    别的树（cut 冻结快照 / 测试临时仓库）→ 按文件加载那棵树里的 runs.py（缓存一份）"""
    import importlib.util as _u
    import sys as _s
    if root is None or Path(root).resolve() == ROOT.resolve():
        return _s.modules[__name__]
    key = str(Path(root).resolve())
    if key not in _FOR_ROOT:
        sp = _u.spec_from_file_location(f"runs_for_root_{abs(hash(key))}", Path(root) / "tools" / "autopilot" / "runs.py")
        m = _u.module_from_spec(sp)
        sp.loader.exec_module(m)
        _FOR_ROOT[key] = m
    return _FOR_ROOT[key]


class RunNotGiven(SystemExit):
    pass


def require_run(argv: list[str] | None = None) -> str:
    """I-16（总调度 21:3x）：离线工具对哪部剧动手必须显式说——命令行 --run <剧> 或环境变量 NS_RUN；都没有 → 退出并说明（不再默认第一季）"""
    import sys as _s
    a = list(_s.argv[1:] if argv is None else argv)
    if "--run" in a and a.index("--run") + 1 < len(a):
        return a[a.index("--run") + 1]
    for x in a:
        if x.startswith("--run="):
            return x.split("=", 1)[1]
    v = os.environ.get("NS_RUN")
    if v:
        return v
    raise RunNotGiven("这个离线工具必须显式指定剧：加 --run <剧 id>（例 --run series10_v2）或设环境变量 NS_RUN（I-16：不再默认第一季）")


def strip_run(argv: list[str]) -> list[str]:
    """去掉 --run X / --run=X（给自己解析 sys.argv 的旧工具用）"""
    out, skip = [], False
    for i, x in enumerate(argv):
        if skip:
            skip = False
            continue
        if x == "--run":
            skip = i + 1 < len(argv)
            continue
        if x.startswith("--run="):
            continue
        out.append(x)
    return out


def gate_legacy_tool(argv: list[str] | None, tool: str, root: Path | None = None) -> list[str]:
    """I-16：第一季专用的历史离线工具（路径 / 资产都是 legacy run 的，还没按剧参数化）入口闸门。
    必须显式 --run（或 NS_RUN）；给的不是 legacy run → 退出并说明（不会拿第一季的东西去动 V2）。
    返回去掉 --run 的参数；argv=None 时同时改 sys.argv（工具后面自己读 sys.argv 也不会撞上 --run）"""
    import sys as _s
    a = list(_s.argv[1:] if argv is None else argv)
    run = require_run(a)
    leg = legacy_run(root)
    if run != leg:
        raise RunNotGiven(f"{Path(tool).name} 是第一季（{leg}）专用的历史工具，还没按剧参数化；--run {run} 不支持（I-16）。"
                          f"要对 {run} 做同样的事，先把这个工具改成按 runs.script_path / renders_dir / looks_path 取路径")
    out = strip_run(a)
    if argv is None:
        _s.argv[1:] = out
    return out


def legacy_run(root: Path | None = None) -> str:
    """K-OPS-F24（总调度 11-02 18:4x，NETFIX 提醒）：只有这部剧（runs.json legacy_unprefixed_run，= series10_v1）用不带 run 前缀的旧路径
    （data/renders/epNN、data/import/script_v2.json、data/autopilot/ready_episodes.json、对象 id 无前缀）。primary 可以切到别的剧，
    别的剧一律 data/renders/<run>/ 等带前缀路径——V2 当上 primary 也不会写进 V1 的目录"""
    reg = registry(root)
    v = reg.get("legacy_unprefixed_run")
    if v:
        return v
    if (root or ROOT).joinpath("config/pipeline/runs.json").is_file():
        # 总调度 19:1x：剧名不写进代码；登记表在、却没写 legacy_unprefixed_run → 失败关闭（不能猜旧路径属于哪部剧）
        raise RunRegistryMissing("runs.json 没写 legacy_unprefixed_run（K-OPS-F24：哪部剧用不带前缀的旧路径必须登记）")
    return reg.get("primary")          # 没有登记表（沙箱 / 测试目录）= 旧行为：主剧就是旧路径那部


def is_legacy(run: str | None, root: Path | None = None) -> bool:
    return (run or primary_run(root)) == legacy_run(root)


def current_run(root: Path | None = None) -> str:
    """本进程在做的剧（OVERSIGHT-F P2：两种环境变量统一认法）：NS_RUN（多剧并行进程，producer.run_env 设）优先，
    否则 primary_run（真实仓库可被 NS_PRIMARY_RUN 钉住，测试用）"""
    v = os.environ.get("NS_RUN")
    if v and (root is None or Path(root).resolve() == ROOT.resolve()):
        return v
    return primary_run(root)


def list_runs(root: Path | None = None, status: str | None = "producing", strict: bool = False) -> list[str]:
    """按 priority 升序（数字小 = 优先）"""
    r = registry(root, strict=strict)
    items = [(v.get("priority", 10), k) for k, v in (r.get("runs") or {}).items() if status is None or v.get("status") == status]
    return [k for _p, k in sorted(items)]


def is_primary(run: str | None, root: Path | None = None) -> bool:
    return run is None or run == primary_run(root)


def import_path(run: str | None, root: Path | None = None) -> Path:
    root = root or ROOT
    r = run or primary_run(root)
    return root / "data/import/script_v2.json" if is_legacy(r, root) else root / "data/import/runs" / r / "script_v2.json"


def run_dir(run: str | None, root: Path | None = None) -> Path:
    root = root or ROOT
    return root / "data/pipeline/runs" / (run or primary_run(root))


# ---------------------------------------------------------------- 按剧取路径的唯一入口（AUDIT-M I-14 / I-15，总调度 11-02 18:5x）
# 编译 / 验收 / 合成 / API 一律调这几个函数，不许写死 data/import/script_v2.json、data/looks、data/renders 根目录（arch_scan hard_import_path）。
# 规则 K-OPS-F24：只有 legacy_unprefixed_run（series10_v1）用不带前缀的旧路径；不给 run = 当前 primary
def script_path(run: str | None = None, root: Path | None = None) -> Path:
    """导入稿（剧本）"""
    return import_path(run or current_run(root), root)


def renders_dir(run: str | None = None, root: Path | None = None) -> Path:
    """渲染根目录（epNN/SK/takes.json、版本图、_final 成片都在下面）"""
    root = root or ROOT
    r = run or current_run(root)
    return root / "data/renders" if is_legacy(r, root) else root / "data/renders" / r


def looks_path(run: str | None = None, root: Path | None = None) -> Path:
    """造型 / 身份脸覆盖 / 衣橱等“造型数据”目录：legacy = data/looks；其它剧 = data/pipeline/runs/<run>/looks（不读 V1 的 data/looks）"""
    root = root or ROOT
    r = run or current_run(root)
    return root / "data/looks" if is_legacy(r, root) else root / "data/pipeline/runs" / r / "looks"


def producer_dir(run: str | None = None, root: Path | None = None) -> Path:
    """producer 的账本 / registry / 心跳 / control 目录：legacy = data/producer；其它剧 = data/producer/runs/<run>"""
    root = root or ROOT
    r = run or current_run(root)
    return root / "data/producer" if is_legacy(r, root) else root / "data/producer/runs" / r


def asset_manifest(run: str | None = None, root: Path | None = None) -> Path | None:
    """资产包清单（runs.json 该剧的 assets_manifest；legacy / 没登记 = None，走旧资产来源）"""
    root = root or ROOT
    r = run or current_run(root)
    v = ((registry(root).get("runs") or {}).get(r) or {}).get("assets_manifest")
    return root / v if v else None


def ready_paths(run: str | None, root: Path | None = None) -> list[Path]:
    root = root or ROOT
    r = run or primary_run(root)
    out = [root / "data/autopilot/runs" / r / "ready_episodes.json"]
    if is_legacy(r, root):
        out.insert(0, root / "data/autopilot/ready_episodes.json")      # 旧路径（NETFIX dispatch / FACTORY 现读）
    return out


def obj_id(run: str | None, kind: str, shot: str, root: Path | None = None) -> str:
    """过渡期：primary 不加前缀（producer / dispatch 认前缀之前不影响现生产）；其它 run 一律 <run>/<kind>:<shot>"""
    r = run or primary_run(root)
    return f"{kind}:{shot}" if is_legacy(r, root) else f"{r}/{kind}:{shot}"


# ---------------------------------------------------------------- 单一来源（PRODMERGE-O 11-03，PLAN_V2_MASTER v4 1-3）
# “当前剧”只认 runs.json 的 primary。config/pipeline/run.json 是由它派生的只读指针（旧读法兼容），只由 sync_run_ptr 写；
# NS_RUN / NS_RENDERS_DIR / NS_SCRIPT_JSON 只由 run_env 按登记表派生（producer / night 的子进程环境）。
RUN_STATUSES = ("preparing", "producing", "paused", "frozen", "archived")
RUN_ENV_KEYS = ("NS_RUN", "NS_RENDERS_DIR", "NS_SCRIPT_JSON")


class RunNotRegistered(RuntimeError):
    """要按剧派生环境 / 改状态的剧不在 runs.json 里（失败关闭）"""


def is_registered(run: str | None, root: Path | None = None) -> bool:
    return bool(run) and run in (registry(root).get("runs") or {})


def run_ptr(root: Path | None = None) -> dict:
    """由 runs.json 派生的 run.json 内容（只有 run_id 一个事实字段）"""
    return {"_doc": "派生文件，勿手改（PRODMERGE-O 11-03 / PLAN_V2_MASTER v4 1-3）：当前剧只认 config/pipeline/runs.json 的 primary；"
                    "本文件由 tools/autopilot/runs.py sync_run_ptr 按它重写，只给旧读法兼容",
            "run_id": primary_run(root), "derived_from": "config/pipeline/runs.json#primary"}


def run_ptr_drift(root: Path | None = None) -> str | None:
    """run.json 与 runs.json primary 不一致 → 一句原因；一致 / 没有 run.json → None"""
    root = root or ROOT
    p = root / "config/pipeline/run.json"
    if not p.is_file() or not (root / "config/pipeline/runs.json").is_file():
        return None
    have = (_jl(p, {}) or {}).get("run_id")
    want = registry(root).get("primary")
    return None if have == want else f"config/pipeline/run.json run_id={have} 与 runs.json primary={want} 不一致（以 runs.json 为准，跑 sync_run_ptr）"


def sync_run_ptr(root: Path | None = None) -> dict:
    """按 runs.json 重写 run.json（原子写）；返回写入内容"""
    root = root or ROOT
    d = run_ptr(root)
    _js(root / "config/pipeline/run.json", d)
    return d


def run_env(run: str, root: Path | None = None) -> dict:
    """按剧运行的进程环境（唯一派生处；producer.run_env / night 转调）：legacy 剧 = {}（旧路径）；
    其它剧必须在 runs.json 登记，否则 RunNotRegistered（不给没登记的剧拼目录）"""
    root = root or ROOT
    if run == legacy_run(root):
        return {}
    if (root / "config/pipeline/runs.json").is_file() and not is_registered(run, root):
        raise RunNotRegistered(f"{run} 不在 config/pipeline/runs.json（只认登记表里的剧，不按任意 --run / NS_RUN 拼路径）")
    return {"NS_RUN": run, "NS_RENDERS_DIR": str(root / "data" / "renders" / run),
            "NS_SCRIPT_JSON": str(root / "data" / "import" / "runs" / run / "script_v2.json")}


def set_status(run: str, status: str, by: str, why: str, root: Path | None = None) -> dict:
    """改一部剧的 status（状态机唯一写入口；preparing → producing 的条件见 tools/master/sop_gate.py promote）。
    每次改动写 status_history（谁、何时、旧值、新值、依据），runs.json 原子写后同步 run.json"""
    root = root or ROOT
    if status not in RUN_STATUSES:
        raise ValueError(f"不认识的 status {status}（只认 {RUN_STATUSES}）")
    p = root / "config/pipeline/runs.json"
    d = _jl(p, None)
    if not d or run not in (d.get("runs") or {}):
        raise RunNotRegistered(f"{run} 不在 {p}")
    e = d["runs"][run]
    old = e.get("status")
    if old == status:
        return e
    e["status"] = status
    e.setdefault("status_history", []).append({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "from": old, "to": status,
                                               "by": by, "why": why})
    _js(p, d)
    sync_run_ptr(root)
    return e


def register_run(run: str, source: str = "import", episode: int | None = None, priority: int = 10,
                 root: Path | None = None) -> dict:
    """导入新剧本 = 追加一条（已有就只补缺失字段）；不改 primary。
    PRODMERGE-O 11-03：新剧默认 status=preparing（不再直接 producing 绕过 SOP）；preparing → producing 只由
    tools/master/sop_gate.py promote（L-154 自动 / 总调度 --apply）按阶段证据提升。已有剧的 status 不动"""
    root = root or ROOT
    p = root / "config/pipeline/runs.json"
    d = registry(root)
    e = dict((d.get("runs") or {}).get(run) or {})
    e.setdefault("priority", priority)
    e.setdefault("status", "preparing")
    e.setdefault("created_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
    e.setdefault("source", source)
    if episode is not None:
        e["episode"] = episode
    d.setdefault("runs", {})[run] = e
    _js(p, d)
    return e
