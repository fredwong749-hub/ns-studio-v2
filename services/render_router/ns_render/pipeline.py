"""
单镜渲染流水线：编译 + 人工修改 → ComfyUI → 存版本（take）。批量脚本和画布 API 共用。

目录: data/renders/epXX/SN/
  compiled.json   编译结果（含成人判定）
  edits.json      画布里的修改（ShotEdit）
  takes.json      所有版本 + 当前选用的版本
  visual_spec.json  画面说明（Frame Spec，ns_schema.frame_spec）
  takes/image_003.png（调色后）+ image_003_raw.png（原图）, takes/last_002.png + last_002_raw.png,
  takes/video_002.mp4, takes/video_002_last.png
  frame.png / last.png / video.mp4 / video_last.png   当前选用版本的副本（批量脚本和下游直接读）
    last.png = 当前选用的独立尾帧（stage "last"）；没有独立尾帧时才用视频抽出的最后一帧
data/renders/epXX/episode_edit.json   整集的导演加强（EpisodeEdit）

首帧/尾帧多轮多候选（C5）：每轮 CANDIDATES 张（不同种子）→ 调色 → 裸露质检 → 自动评分（score.py），
全部留档，最高分设为当前；最高分低于 SCORE_PASS 再来一轮（≤ROUNDS 轮），仍低就在 takes.json 记 review。
"""
import hashlib
import json
import shutil
import subprocess
import time
from dataclasses import asdict
import os
from pathlib import Path

from ns_agent import h3_prompt as H3
from ns_agent.compiler import CompiledShot, boost_text, compile_shot, insert_before_style, soundscape_block
from ns_schema.h3_lint import frames_for_seconds

# 只能经项目方专用接口写的 ShotEdit 字段（普通 PUT /edit 一律沿用已存值）
H3_OVERRIDE_FIELDS = ("h3_prompt_override", "audit_override_requests")
from ns_schema.edits import EpisodeEdit, ShotEdit, Take
from ns_schema.script import ScriptImport

from . import refs as R
from . import workflows as W
from . import comfy_client as CC
from . import render_paths as _RP   # W33：版本文件相对路径带剧名前缀
from .comfy_client import ComfyClient, ComfyError, ComfyUnavailable

ROOT = Path(os.environ.get("NS_PROJECT_ROOT") or Path(__file__).resolve().parents[3])  # T42：子进程可指向 projects/<slug>
DATA = ROOT / "data"
# 多剧并行（DISTILL §0.1，K-OPS-E15）：一部剧一个 producer 进程；非主剧进程设 NS_RENDERS_DIR / NS_SCRIPT_JSON，
# 渲染目录 data/renders/<run>/、导入稿 data/import/runs/<run>/script_v2.json，takes.json 跨剧绝不共用。不设 = 主剧原路径
def _legacy_and_primary() -> tuple[str | None, str | None]:
    """K-OPS-F24：统一调 tools/autopilot/runs.py 的 legacy_run / primary_run（不在这里另写解析）。
    没有登记表（沙箱 / 测试目录）→ (None, None) = 旧行为；登记表在却缺 legacy_unprefixed_run → runs.legacy_run 抛错（失败关闭）"""
    if not (ROOT / "config" / "pipeline" / "runs.json").is_file():
        return None, None
    from ns_agent import run_paths as _RPA     # OVERSIGHT-F P2：runs 加载器只此一处
    m = _RPA.runs_module(ROOT)
    return m.legacy_run(ROOT), m.current_run(ROOT)


def _default_renders() -> Path:
    leg, prim = _legacy_and_primary()
    return DATA / "renders" if prim == leg else DATA / "renders" / prim


def _default_script() -> Path:
    leg, prim = _legacy_and_primary()
    return DATA / "import" / "script_v2.json" if prim == leg else DATA / "import" / "runs" / prim / "script_v2.json"


# K-OPS-F24：不设环境变量时 = 当前 primary 的目录；只有 legacy_unprefixed_run 那部剧是不带前缀的旧路径
RENDERS = Path(os.environ["NS_RENDERS_DIR"]) if os.environ.get("NS_RENDERS_DIR") else _default_renders()
SCRIPT_JSON = Path(os.environ["NS_SCRIPT_JSON"]) if os.environ.get("NS_SCRIPT_JSON") else _default_script()

IMAGE_SIZE = (1088, 1920)
VIDEO_SIZE = (768, 1344)   # SOP S4 P-B3 锁定（短边 768、面积 ≤768×1344、32 的倍数）；旧值 576×1024
VIDEO_FRAMES = 243          # 10 秒；H3 帧数须落在 17k+5 网格上

# 首帧锁脸：编译后的首帧提示词含角色锁【名字】时，用该角色的参考图做 Qwen 2.1 单参考编辑
ASSETS = ROOT / "assets"
CHARACTER_REFS = {"林晚": ASSETS / "characters" / "lin_wan" / "lin_wan.png"}
FACE_LOCK_DEFAULT = True    # 2026-10-30 项目方要求林晚对齐 assets/linwan，并已确认参考图来源（config/face_reference.json）
# 参考图来源审批（历史字段；2026-10-30 取消过滤器后不再拦成人镜头）
FACE_REF_APPROVAL = ROOT / "config" / "face_reference.json"
# 只说明参考图的用途（技术指令），剧本提示词原文接在后面不改
# 非成人镜头：Turbo 在 CFG 1 下负面词无效，模型会在剧本没写衣着时自行加裸露。
# 所以非成人镜头用 Base 模型 + 真实 CFG，让负面词生效，并追加防裸露负面词（只追加到负面，不改正面提示词）。
QWEN_TURBO = W.QwenModel()
QWEN_BASE = W.QwenModel(unet="qwen_image_2.1_int8_convrot.safetensors", turbo=False, steps=25, cfg=3.0)
NON_ADULT_USE_BASE = False  # 2026-10-30 实测 Base+CFG 并不减少裸露且慢 3.8 倍，改回 Turbo，靠 QC 拦截
NON_ADULT_NEGATIVE_EXTRA = "裸体、裸露、裸背、赤裸上身、露出乳房、内衣外露"  # B10：不压制性感穿着，只防裸露
NUDITY_RETRIES = 2          # 非成人首帧质检判为裸露时，换种子重抽的次数
QC_LOG = RENDERS / "qc_log.jsonl"
FACE_LOCK_PREFIX = "图1是{name}的面部参考：保持图1人物的脸型、五官与发型特征完全一致，只参考面部，服装、首饰、场景、构图与动作按以下描述。"

# 参考图：plate = 中性身份底板（research v6，默认）；portrait = 旧做法（项目方成品定妆图，只留作 A/B 对照）
REF_MODE = "plate"
# 提示词：script = 剧本原文透传（§1.2，默认）；spec = 画面说明层拼装（试点，项目方确认前不推广）
PROMPT_METHOD = "script"
CANDIDATES = 3              # 每轮候选张数（C5）
ROUNDS = 3                  # 最多轮数
SCORE_PASS = 0.65           # 最高分低于此值进入下一轮（2026-10-30 校准：合格图 0.65–0.8）
GRADE_STRENGTH = 0.6        # 首帧向色调参考图迁移的力度
LAST_GRADE_STRENGTH = 0.7   # 尾帧向定稿首帧迁移的力度
LAST_MODEL = QWEN_TURBO     # 尾帧编辑模型（research v6 建议 Base；本机 A/B 见 golden/quality_v2）
LAST_FIRST_FRAME_SIZE = (768, 1344)   # 尾帧编辑时首帧参考的预缩放尺寸（同比例，控制编码 token）

# 两段式出图（2026-10-30 A/B，golden/quality_v2/sweep）：带脸部底板直接编辑时 Turbo 会把人物摆成底板的正脸居中构图
# （ep01-S3“低头侧脸”剧本核对 0.2）；先文生图出构图（核对 0.6），再用底板只换脸和发型，构图保持。
COMPOSE_WIDTH = 768                   # 构图图作为图1 时的预缩放宽度（768 与原尺寸得分相同，快 30%）
# 2026-10-30 实测（golden/quality_v2/idfix）：长篇“图2只提供…不要沿用…”作用说明会让换脸失效（发色仍是构图里的藏蓝），
# 明确写出要换成的发色发型才生效
COMPOSE_ROLE = ("图1是本镜头的画面构图。保持图1的构图、机位、景别、人物姿势与朝向、表情、服装、背景和光线不变，"
                "只把图1中{name}的五官长相、妆容、发色与发型换成图2人物的样子{look}；不要沿用图2的姿势、手势、首饰和背景。")
IDENTITY_LOOK = {"林晚": "（浅冰蓝色及腰直发、齐刘海、冷白皮肤、精致妆容）"}
IDENTITY_FACT = {"林晚": "人物是浅冰蓝色及腰长直发、齐刘海"}   # 评分核对身份（CLIP 脸部相似度区分不出发色）
# 剧本没写赤脚时，评分核对“穿着鞋”（Turbo CFG 1 下锁里的“不要无故赤脚”反而会招来赤脚，只能靠选片）
BAREFOOT_WORDS = ("赤脚", "光脚", "赤足", "脚趾", "裸足", "脱鞋", "脚踝", "脚背")
SHOD_FACT = "画面中的人物没有赤脚（看不到脚也算符合）"


def extra_facts(c: CompiledShot) -> list[str]:
    """不来自剧本原文、但质检要核对的条目：身份（发色发型）、未写赤脚时要穿鞋"""
    out = []
    for name, fact in IDENTITY_FACT.items():
        if f"【{name}】" in c.image_prompt:
            out.append(fact)
            if not any(w in c.image_body for w in BAREFOOT_WORDS):
                out.append(SHOD_FACT)
    return out
# 写实精修（research v8 §4 低重绘第二遍）：Base + 胶片纹理 LoRA + 纹理修复 VAE，denoise 0.3，单独一个批次跑
# （本机 Turbo/Base 两个 UNET 来回切换每次要重新加载约 60 秒）
REFINE_MODEL = W.QwenModel(unet="qwen_image_2.1_int8_convrot.safetensors", turbo=False, steps=12, cfg=1.0,
                           vae="texture_fix_vae_for_qwen_image_2.1_bf16.safetensors")
# 2026-10-30 A/B（golden/quality_v2/owner_ab）：项目方要保留参考图的感觉，写实只作质感 → 轻精修 PureLens 0.4 @ 0.22
REFINE_LORAS = [("purelens_qwen21.safetensors", 0.4)]
REFINE_DENOISE = 0.22
REFINE_PREFIX = "p3r4l4ns，真实相机拍摄，皮肤保留细小毛孔与自然纹理，"   # 只要质感，不写“无美颜/无妆”（会冲掉精致妆容）
REFINE_TOLERANCE = 0.08               # 精修版评分比原版低超过此值就不设为当前（留档待审）


def snap_frames(frames: int) -> int:
    """H3 的帧数网格是 17k+5（节点会向上取整），这里先取整，记录的帧数才是真实帧数"""
    k = max(0, -(-(frames - 5) // 17))
    return 17 * k + 5


def face_ref_approved() -> bool:
    """参考图来源审批只作展示；2026-10-30 取消成人过滤器后不再拦成人镜头。"""
    return True


def face_lock_default() -> bool:
    return FACE_LOCK_DEFAULT


def check_face_lock_allowed(c: CompiledShot):
    """参考编辑对所有镜头开放；保留函数给旧调用方。"""
    return None


def face_reference(c: CompiledShot) -> tuple[str, Path] | None:
    for name, path in CHARACTER_REFS.items():
        if f"【{name}】" in c.image_prompt and path.exists():
            return name, path
    return None


def image_model(c: CompiledShot) -> W.QwenModel:
    return QWEN_BASE if not c.adult and NON_ADULT_USE_BASE else QWEN_TURBO


def image_negative(c: CompiledShot, model: "W.QwenModel | None" = None) -> str:
    """所有镜头同一负面词：2026-10-30 项目方取消成人过滤器后不再给非成人镜头追加防裸露词。
    FIX（ep02_iter P0 ⑥）：给了 model 且实际 cfg ≤ 1（Turbo）→ ""：负面词不生效，也不写进 take.negative"""
    return W.negative_for(model, c.image_negative) if model is not None else c.image_negative


def qc_with_vlm(client):
    """非成人质检：NudeNet + CLIP v2 + 本地看图模型裁决（qc.py v3）"""
    from .qc import nudity_qc, vlm_judge
    judge = vlm_judge(client) if client is not None else None
    return lambda path: nudity_qc(path, vlm=judge)


def _default_qc(path: Path) -> dict:
    from .qc import nudity_qc
    return nudity_qc(path)


def _log_qc(shot_id: str, take: dict):
    QC_LOG.parent.mkdir(parents=True, exist_ok=True)
    with QC_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": now(), "shot": shot_id, "stage": take["stage"], "n": take["n"],
                            "qc": take.get("qc")}, ensure_ascii=False) + "\n")


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def load_script() -> ScriptImport:
    return ScriptImport.model_validate_json(SCRIPT_JSON.read_text(encoding="utf-8"))


def shot_dir(shot_id: str) -> Path:
    ep, s = shot_id.split("-")
    return RENDERS / ep / s


def find_shot(script: ScriptImport, shot_id: str):
    for ep in script.episodes:
        for s in ep.shots:
            if s.id == shot_id:
                return ep, s
    raise KeyError(shot_id)


def next_shot_id(script: ScriptImport, shot_id: str) -> str | None:
    ids = [s.id for ep in script.episodes for s in ep.shots]
    if shot_id not in ids:   # fredw-92/B-fix：不在剧本里（换剧本后的旧镜头）→ None，别在后台线程里 ValueError
        return None
    i = ids.index(shot_id)
    return ids[i + 1] if i + 1 < len(ids) else None


def seed_for(key: str, attempt: int) -> int:
    return int(hashlib.sha256(f"{key}#{attempt}".encode()).hexdigest()[:12], 16)


def _read_json(p: Path, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def _write_json(p: Path, obj):
    """封板修复 P1：唯一临时文件 + os.replace（被读到一半 / 进程崩溃都不会留下截断的 edits/takes/review.json）"""
    import tempfile
    from ns_agent.state_timeline import _replace_retry
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, indent=2))
        _replace_retry(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------------ 修改与版本

def load_edit(shot_id: str) -> ShotEdit:
    return ShotEdit.model_validate(_read_json(shot_dir(shot_id) / "edits.json", {}))


def save_edit(shot_id: str, edit: ShotEdit) -> ShotEdit:
    edit.updated_at = now()
    _write_json(shot_dir(shot_id) / "edits.json", edit.model_dump())
    return edit


import threading  # noqa: E402

_SHOT_LOCKS: dict[str, threading.RLock] = {}
_SHOT_LOCKS_GUARD = threading.Lock()


def shot_lock(shot_id: str) -> threading.RLock:
    """封板修复：每镜一把可重入锁。takes.json / edits.json 的读-改-写（渲染线程 _add_take、API 设当前 / 上传 / 改写）
    都在这把锁里做，避免互相覆盖、并发上传拿到同一个 n。只管本进程。"""
    with _SHOT_LOCKS_GUARD:
        lk = _SHOT_LOCKS.get(shot_id)
        if lk is None:
            lk = _SHOT_LOCKS[shot_id] = threading.RLock()
        return lk


_EP_LOCKS: dict[int, threading.RLock] = {}


def episode_lock(ep: int) -> threading.RLock:
    """fredw-92/B-fix：每集一把可重入锁。episode_edit.json 的读-改-写（PUT /api/episodes/{n}/edit、
    refs/apply scope=scene）都在这把锁里做。只管本进程。"""
    with _SHOT_LOCKS_GUARD:
        lk = _EP_LOCKS.get(int(ep))
        if lk is None:
            lk = _EP_LOCKS[int(ep)] = threading.RLock()
        return lk


def load_takes(shot_id: str) -> dict:
    return _read_json(shot_dir(shot_id) / "takes.json", {"takes": [], "active": {}})


def _save_takes(shot_id: str, data: dict, *, op: str = "legacy_save", by: str = "pipeline._save_takes"):
    """旧接口（调用方自己 load_takes → 改 → 存）：内部改走写入口的同一套落盘（跨进程锁、seq+1、last_op、备份）。
    调用方读到的 seq 比文件里的旧 = 中间有别的进程写过：记 data/state/takes_conflicts.jsonl 报警，仍按调用方的版本写
    （不打断渲染线程；新代码请直接用 mutate_takes(expect_seq=...)）"""
    p = shot_dir(shot_id) / "takes.json"
    with shot_lock(shot_id), _file_lock(p.with_name("takes.json.lock")):
        cur = int((_read_json(p, {}) or {}).get("seq") or 0)
        mine = int(data.get("seq") or 0)
        if mine < cur:
            _takes_conflict(shot_id, mine, cur, op, by)
        _commit_takes(p, data, max(cur, mine), op=op, by=by, op_id=None, run=None, shot_id=shot_id)


# ------------------------------------------------------------------ takes.json 唯一写入口（EDITOR_INTEGRATION 附录 A2）
TAKES_LAST_OPS = 50          # last_ops 只留最近 50 条（op_id 防重放用）
TAKES_BAK_KEEP = 10          # .takes_bak/ 只留最近 10 份


class TakesConflict(RuntimeError):
    """expect_seq 和文件里的 seq 对不上（底版 / 副本落后），调用方先拉最新再让人确认"""

    def __init__(self, sid: str, expect: int, actual: int):
        super().__init__(f"{sid} takes.json seq={actual}，调用方期望 {expect}")
        self.sid, self.expect, self.actual = sid, expect, actual


def _run_renders(run: str | None) -> Path:
    """run → 渲染根目录：不给 / 主剧（config/pipeline/runs.json primary）= 本进程 RENDERS 的主剧路径；其它剧 = data/renders/<run>"""
    if not run:
        return RENDERS
    import sys as _sys
    if str(ROOT / "tools" / "autopilot") not in _sys.path:
        _sys.path.insert(0, str(ROOT / "tools" / "autopilot"))
    import runs as RUNS                          # 登记表读不到就 raise（strict，失败关闭：不能猜错 takes 落到哪部剧）
    RUNS.primary_run(ROOT, strict=True)
    return DATA / "renders" if run == RUNS.legacy_run(ROOT) else DATA / "renders" / run   # K-OPS-F24


def takes_path(shot_id: str, run: str | None = None) -> Path:
    ep, s = shot_id.split("-")
    return _run_renders(run) / ep / s / "takes.json"


def _iso_jst() -> str:
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds")


def _file_lock(lp: Path, timeout: float = 30):
    """跨进程文件锁（Windows msvcrt / Linux fcntl），拿不到超时抛 TimeoutError"""
    import contextlib

    @contextlib.contextmanager
    def cm():
        lp.parent.mkdir(parents=True, exist_ok=True)
        f = open(lp, "a+b")
        t0 = time.time()
        try:
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        f.seek(0)
                        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.time() - t0 > timeout:
                        raise TimeoutError(f"拿不到 {lp}")
                    time.sleep(0.05)
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:   # arch-ok: 释放文件锁失败（文件已关）不影响已落盘的结果，关文件句柄即释放
                pass
            f.close()
    return cm()


def get_takes(shot_id: str, run: str | None = None) -> tuple[int, dict]:
    """读 takes.json → (seq, doc)；没有 seq 的旧文件按 0"""
    p = takes_path(shot_id, run)
    doc = _read_json(p, {"takes": [], "active": {}})
    return int(doc.get("seq") or 0), doc


def _takes_conflict(shot_id: str, mine: int, cur: int, op: str, by: str) -> None:
    try:
        f = DATA / "state" / "takes_conflicts.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": _iso_jst(), "sid": shot_id, "caller_seq": mine, "file_seq": cur, "op": op, "by": by},
                                ensure_ascii=False) + "\n")
    except OSError:   # arch-ok: 只是追加冲突报警，写失败不能挡 takes 落盘，不是读账本
        pass


def _commit_takes(p: Path, doc: dict, seq: int, *, op: str, by: str, op_id, run, shot_id: str) -> dict:
    """锁内落盘：备份旧文件（留 TAKES_BAK_KEEP 份，更旧的移隔离区，不删）→ seq+1 / last_op / last_ops → 原子写。调用方持锁"""
    if p.exists():
        bak = p.parent / ".takes_bak"
        bak.mkdir(exist_ok=True)
        shutil.copy2(p, bak / f"takes.{seq}.{time.strftime('%Y%m%dT%H%M%S')}.{os.getpid()}.json")
        def _bseq(x: Path) -> tuple:
            parts = x.name.split(".")              # takes.<seq>.<时间>[.<pid>].json → 按 seq、时间排，不按修改时间
            return (int(parts[1]) if len(parts) > 2 and parts[1].isdigit() else -1, parts[2] if len(parts) > 3 else "")
        olds = sorted(bak.glob("takes.*.json"), key=_bseq)
        if olds[:-TAKES_BAK_KEEP]:
            q = DATA / "_quarantine" / "takes_bak" / (run or "_") / shot_id
            q.mkdir(parents=True, exist_ok=True)
            for x in olds[:-TAKES_BAK_KEEP]:
                try:
                    shutil.move(str(x), str(q / x.name))
                except OSError:   # arch-ok: 旧备份挪隔离区失败就留在原处，下次再挪，不影响落盘
                    pass
    rec = {"seq": seq + 1, "op": op, "by": by, "op_id": op_id, "at": _iso_jst()}
    doc["seq"], doc["last_op"] = seq + 1, rec
    doc["last_ops"] = (list(doc.get("last_ops") or []) + [rec])[-TAKES_LAST_OPS:]
    _write_json(p, doc)
    return doc


def mutate_takes(shot_id: str, fn, *, op: str, by: str, op_id: str | None = None, run: str | None = None,
                 expect_seq: int | None = None, path: Path | None = None) -> dict:
    """takes.json 的唯一写入口：本进程每镜锁 + 跨进程 takes.json.lock 里读-改-写。
    - expect_seq 给了且和文件 seq（缺省 0）不等 → TakesConflict，不写；
    - op_id 已在 last_ops 里 → 视为重放，原样返回、不再执行 fn（幂等）；
    - fn(doc) 原地改或返回新 doc；写前把旧文件备份到 .takes_bak/takes.<seq>.<时间>.json（留 10 份，更旧的移到 data/_quarantine/takes_bak/）；
    - seq+1，last_op={seq, op, by, op_id, at(+09:00)}，last_ops 留最近 50 条。返回写入后的 doc"""
    p = Path(path) if path else takes_path(shot_id, run)   # path：调用方开算时钉死的文件（A-04：_pf_write 也走这里）
    with shot_lock(shot_id), _file_lock(p.with_name("takes.json.lock")):
        doc = _read_json(p, {"takes": [], "active": {}})
        seq = int(doc.get("seq") or 0)
        if op_id and any((x or {}).get("op_id") == op_id for x in doc.get("last_ops") or []):
            return doc
        if expect_seq is not None and int(expect_seq) != seq:
            raise TakesConflict(shot_id, int(expect_seq), seq)
        new = fn(doc)
        doc = new if isinstance(new, dict) and "takes" in new else doc   # fn 可能顺手返回 pop 出的 dict，只认整份 doc
        return _commit_takes(p, doc, seq, op=op, by=by, op_id=op_id, run=run, shot_id=shot_id)


class QCRejected(ValueError):
    pass


# 一镜一条链（项目方审计 2026-10-30，docs/audit/shot_readiness_and_continuity.md）：首帧/尾帧/视频由同一条流水线出。
# 规则在 config/shot_ownership.json（项目级 owner），另认 gpu3 每镜 status.json 里的 {owner: "gpu3"}。
# 本机已有的版本只作历史，不删。
import os  # noqa: E402

OWNERSHIP = ROOT / "config" / "shot_ownership.json"
ACTIVE_PROJECT = os.environ.get("NS_PROJECT", "weita")   # 本仓库当前项目：为他而骚
GPU3_RENDERS = ROOT / "data" / "renders_gpu3"
PROJECT_GATE_MSG = "gpu3 负责此项目的首尾帧与视频（config/shot_ownership.json）"


class Gpu3Owned(ComfyError):
    pass


def project_owner(project: str | None = None) -> str | None:
    cfg = _read_json(OWNERSHIP, {})
    return ((cfg.get("projects") or {}).get(project or ACTIVE_PROJECT) or {}).get("owner")


def project_gate_message() -> str | None:
    """项目整体由 gpu3 负责时返回提示（批量脚本据此直接退出）"""
    return PROJECT_GATE_MSG if project_owner() == "gpu3" else None


def gpu3_owns(shot_id: str) -> str | None:
    """返回 gpu3 负责的原因：项目规则，或该镜 data/renders_gpu3/epXX/SN/status.json 写了 owner=gpu3。
    项目 owner=canvas（2026-10-30 夜，fredw-92/W2：画布交互改图是主链路）时一律放行，gpu3 每镜 owner 只作历史。"""
    owner = project_owner()
    if owner == "gpu3":
        return PROJECT_GATE_MSG
    if owner == "canvas":
        return None
    try:
        ep, sn = shot_id.split("-")
    except ValueError:
        return None
    st = _read_json(GPU3_RENDERS / ep / sn / "status.json", {})
    if str(st.get("owner", "")).lower() == "gpu3":
        return f"gpu3 负责此镜头（data/renders_gpu3/{ep}/{sn}/status.json owner=gpu3）"
    return None


def check_not_gpu3(shot_id: str):
    if RENDERS != ROOT / "data" / "renders":   # 沙箱（A/B、测试）不受影响
        return
    why = gpu3_owns(shot_id)
    if why:
        raise Gpu3Owned(f"{shot_id}：{why}")


class LegacyPathBlocked(ComfyError):
    """G18（MASTER_PICTURE §4；WORKFLOW 11-01）：登记了资产的项目不许走旧出图路径"""


def check_not_legacy_project(script, shot_id: str, fn: str = "render_image"):
    """G18 堵旧路径（只隔离，不删）：剧本属于 config/recipes/asset_refs.json 登记的项目（例 series10_v1）时，
    旧的 render_image（身份底板两段式换脸 qwen_two_stage + plates/master.png 母版脸裁切 = data/cache/refs/master_face_*）
    和 render_last_frame 一律拒绝——首尾帧只走唯一出图路径 ns_render.unified_frame（K-REN-134、K-OPS-W01）。
    没登记的旧项目（60 集旧剧、A/B 沙箱）行为不变。无绕过开关"""
    try:
        from .unified_frame import project_for
        proj = project_for(script)
    except Exception:  # noqa: BLE001  判定失败不挡旧项目
        return
    if proj:
        raise LegacyPathBlocked(f"{shot_id}：项目 {proj} 已登记资产参考，旧出图路径 pipeline.{fn}"
                                f"（两段式换脸 / 母版脸缓存）已停用；请走 ns_render.unified_frame（画布重出 / frame-jobs / image_jobs）")


EXTERNAL_SOURCES = ("gpu3", "owner", "upload")   # 不是本机出的版本（gpu3 / 项目方上传）


_ADULT_CACHE: dict = {}


def shot_is_adult(shot_id: str) -> bool:
    """按当前剧本判定成人镜头（剧本文件变了就重算）；剧本里没有的镜头（测试）算非成人"""
    try:
        mtime = SCRIPT_JSON.stat().st_mtime
    except OSError:
        return False
    if _ADULT_CACHE.get("mtime") != mtime:
        script = load_script()
        _ADULT_CACHE.clear()
        _ADULT_CACHE.update({"mtime": mtime, "adult": {s.id for ep in script.episodes for s in ep.shots
                                                        if compile_shot(ep, s, script.globals, set()).adult}})
    return shot_id in _ADULT_CACHE["adult"]


class BadImage(ValueError):
    """fredw-92/UXF1：坏图不能设为当前 / 定稿（ns_render.image_check）"""


def bad_reason(take: dict) -> str | None:
    from . import image_check as IC
    return take.get("bad") or IC.check_take(take, DATA)


def ineligible_reason(take: dict) -> str | None:
    """K-OPS-Q23（RSI 11-01，ep01-S5 v3 事故）：不能设为当前的版本——坏图 / 已标“不要” / 总调度驳回过。
    驳回要先撤销（unreject 去掉 params.rejected；master_reject 由总调度清掉）才能再设为当前"""
    why = bad_reason(take)
    if why:
        return why
    if (take.get("params") or {}).get("rejected") if isinstance(take.get("params"), dict) else None:
        return "已标“不要”的版本"
    if take.get("master_reject"):
        return "总调度驳回过的版本"
    return None


def _caller() -> str:
    import inspect
    for fr in inspect.stack()[2:6]:
        mod = fr.frame.f_globals.get("__name__", "?")
        if not mod.endswith("ns_render.pipeline"):
            return f"{mod}.{fr.function}"
    return "?"


def audit_active(shot_id: str, stage: str, frm, to, reason: str, actor: str | None = None) -> None:
    """K-OPS-Q23：当前版本的每一次变化都记 data/producer/current_changes.jsonl（与 auto_current.audit_change 同格式）"""
    if frm == to:
        return
    try:
        f = DATA / "producer" / "current_changes.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "shot": shot_id, "stage": stage, "from": frm, "to": to,
               "reason": reason, "actor": actor or _caller(), "trigger_take": to}
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def clear_active(shot_id: str, stage: str, reason: str, actor: str | None = None) -> None:
    """撤掉当前版本（不退回旧版），带审计"""
    with shot_lock(shot_id):
        data = load_takes(shot_id)
        before = (data.get("active") or {}).pop(stage, None)
        _save_takes(shot_id, data)
    audit_active(shot_id, stage, before, None, reason, actor or _caller())


def set_active(shot_id: str, stage: str, n: int, reason: str = "", actor: str | None = None):
    """唯一改“当前版本”的入口（K-OPS-Q23）：拒绝坏图 / 已标不要 / 总调度驳回过的版本；每次变化都记审计。
    auto_current.on_contract 自己写带原因的审计，这里对它不重复记"""
    who = actor or _caller()
    with shot_lock(shot_id):   # 封板修复：每镜锁
        data = load_takes(shot_id)
        take = next(t for t in data["takes"] if t["stage"] == stage and t["n"] == n)
        why = ineligible_reason(take)
        if why:
            raise BadImage(f"{shot_id} {stage} v{n} 是{why}，不能设为当前")
        before = (data.get("active") or {}).get(stage)
        data["active"][stage] = n
        _save_takes(shot_id, data)
    if "auto_current" not in who:
        audit_active(shot_id, stage, before, n, reason or "设为当前", who)
    with shot_lock(shot_id):
        d = shot_dir(shot_id)
        if stage == "image":
            shutil.copyfile(DATA / take["file"], d / "frame.png")
        elif stage == "last":
            shutil.copyfile(DATA / take["file"], d / "last.png")
        else:
            shutil.copyfile(DATA / take["file"], d / "video.mp4")
            if take.get("last_frame"):
                shutil.copyfile(DATA / take["last_frame"], d / "video_last.png")
                if "last" not in data["active"]:   # 独立尾帧优先；没有时沿用旧行为
                    shutil.copyfile(DATA / take["last_frame"], d / "last.png")


def stamp_versions(shot_id: str, take: Take) -> Take:
    """T06/T51：每个新 take 记录出图时的规则版本 bible_version 与本镜 inputs_sha（信息用；算不出就留空）"""
    try:
        from ns_agent.compiler import bible_version
        from ns_agent.state_timeline import shot_inputs_sha
        if take.bible_version is None:
            take.bible_version = bible_version(ROOT)
        if take.inputs_sha is None:
            take.inputs_sha = shot_inputs_sha(shot_id, ROOT)
    except Exception:  # noqa: BLE001 — 盖章失败不影响存版本
        pass
    try:   # S2.1：签名 {script_sig, prompt_sha, rule_fp}（tools/autopilot/take_sig.py；producer 每拍拿它比当前编译）
        if getattr(take, "sig", None) is None:
            take.sig = _take_sig(shot_id, take)
    except Exception:  # noqa: BLE001  # arch-ok: 盖不上签名 = 这一版按“未知”处理（record 只计数），不影响存版本
        pass
    try:   # UX5-B P0：所用资产版本（定妆 / 身份 LoRA / 视频基底 / 附加视频 LoRA）；预设在 image_params 补
        from ns_agent.asset_revs import stamp
        stamp(shot_id, take, ROOT)
    except Exception:  # noqa: BLE001
        pass
    return take


_TAKE_SIG_MOD: dict = {}


def _take_sig(shot_id: str, take: Take) -> dict | None:
    """本进程渲染根（RENDERS）对应的剧 → 该剧导入稿（SCRIPT_JSON）+ 首帧作业表 → take_sig.for_new_take"""
    import importlib.util as _ilu   # 复审 11-07：按文件路径单独加载，不往本进程 sys.path 塞 tools/autopilot（那里有 policy / director 等同名模块）
    TS = _TAKE_SIG_MOD.get("m")
    if TS is None:
        spec = _ilu.spec_from_file_location("ns_take_sig", str(ROOT / "tools" / "autopilot" / "take_sig.py"))
        TS = _ilu.module_from_spec(spec)
        spec.loader.exec_module(TS)
        _TAKE_SIG_MOD["m"] = TS
    run = RENDERS.name if RENDERS.parent == DATA / "renders" else _legacy_and_primary()[0]
    if not run:
        return None
    jobs = DATA / "pipeline" / "runs" / run / "assets" / "first_frame_jobs.json"
    return TS.for_new_take(ROOT, shot_id, take.stage, take.model_dump(), SCRIPT_JSON, jobs)


def _add_take(shot_id: str, take: Take, activate: bool = True):
    from . import image_check as IC
    with shot_lock(shot_id):   # 封板修复：每镜锁
        stamp_versions(shot_id, take)
        if take.bad is None:   # fredw-92/UXF1：坏图（太小 / 尺寸不符）只记版本，不设为当前
            take.bad = IC.check_take(take.model_dump(), DATA)
        if take.bad:
            activate = False
        data = load_takes(shot_id)
        used = {t["n"] for t in data["takes"] if t["stage"] == take.stage}
        if take.n in used:   # 封板修复：渲染线程开工时取的号在出图期间被上传占了 → 顺延（文件名不变，只改版本号）
            take.n = max(used) + 1
        data["takes"].append(take.model_dump())
        _save_takes(shot_id, data)
        if activate:
            set_active(shot_id, take.stage, take.n)


def deactivate(shot_id: str, stage: str):
    """撤掉当前版本：有旧版本就退回到最新的那个（不退到坏图 / 已标不要 / 总调度驳回过的，K-OPS-Q23），否则清空（批量脚本会重新出）"""
    with shot_lock(shot_id):   # 封板修复：每镜锁
        data = load_takes(shot_id)
        clean = [t for t in data["takes"] if t["stage"] == stage
                 and t["n"] != data["active"].get(stage) and not ineligible_reason(t)]
        if clean:
            set_active(shot_id, stage, clean[-1]["n"], reason="撤掉当前，退回最新一张可用的旧版")
            return clean[-1]["n"]
        before = data["active"].pop(stage, None)
        _save_takes(shot_id, data)
        d = shot_dir(shot_id)
        files = {"image": ("frame.png",), "last": ("last.png",), "video": ("video.mp4", "video_last.png")}[stage]
        if stage == "video" and "last" not in data["active"]:
            files += ("last.png",)
        for f in files:
            (d / f).unlink(missing_ok=True)
    audit_active(shot_id, stage, before, None, "撤掉当前，没有可用的旧版，清空")
    return None


def qc_sweep(shot_id: str, adult: bool, qc_fn=None, recheck: bool = False, only_active: bool = False
             ) -> list[dict]:
    """给已有首帧补做裸露质检；只记录，不撤版本（成人过滤器已取消）。返回被标记的 take 列表"""
    qc_fn = qc_fn or _default_qc
    with shot_lock(shot_id):   # 封板修复：每镜锁
        data = load_takes(shot_id)
        flagged = []
        for t in data["takes"]:
            if only_active and data["active"].get(t["stage"]) != t["n"]:
                continue
            if t["stage"] not in ("image", "last") or (t.get("qc") and not recheck):
                if (t.get("qc") or {}).get("nudity"):
                    flagged.append(t)
                continue
            t["qc"] = qc_fn(DATA / t["file"])
            if t["qc"]["nudity"]:
                flagged.append(t)
                _log_qc(shot_id, t)
        _save_takes(shot_id, data)
        return flagged


def _next_n(shot_id: str, stage: str) -> int:
    """取下一个版本号；调用方要连同写入 takes.json 一起放在 shot_lock(shot_id) 里（封板修复）"""
    return 1 + max((t["n"] for t in load_takes(shot_id)["takes"] if t["stage"] == stage), default=0)


def extract_last_frame(video: Path, out: Path) -> Path | None:
    try:
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-sseof", "-0.1", "-i", str(video),
                            "-frames:v", "1", "-update", "1", str(out)], capture_output=True, timeout=120)   # K-OPS-S01
    except subprocess.TimeoutExpired:
        return None   # 和 ffmpeg 失败同样处理：调用方见 None 即“没抽到尾帧”
    return out if r.returncode == 0 and out.exists() else None


# ------------------------------------------------------------------ 编译（含修改）

def apply_edit(c: CompiledShot, e: ShotEdit, style: str = "") -> CompiledShot:
    """整段替换的提示词也要带上导演加强（原样，插在风格后缀前；已经含有就不重复）"""
    if e.image_prompt:
        c.image_prompt = e.image_prompt if not c.boost_first or c.boost_first in e.image_prompt \
            else insert_before_style(e.image_prompt, c.boost_first, style)
    if e.last_prompt:
        c.last_prompt = e.last_prompt if not c.boost_last or c.boost_last in e.last_prompt             else insert_before_style(e.last_prompt, c.boost_last, style)
    if e.image_negative:
        c.image_negative = e.image_negative
    # SOP §9A：H3 提示词只认项目方覆盖（PUT /api/shots/{id}/h3-override 写入，带确认原话）；
    # 通过 lint 的（renderable）才替换出视频用的 video_prompt，草稿（draft）只记录、不出视频
    c.h3_base_prompt = c.h3_base_prompt or c.video_prompt
    if e.h3_prompt_override:
        c.h3_override = e.h3_prompt_override.model_dump()
        if e.h3_prompt_override.status == "renderable":
            c.video_prompt = e.h3_prompt_override.text
            c.h3_missing = []
    # 旧版中文视频提示词的人工修改（整段 / 运镜 / 表情）只作用在 legacy_video_prompt（历史），不进 H3
    legacy = c.legacy_video_prompt
    if e.video_prompt:
        legacy = e.video_prompt if "overall_soundscape:" in e.video_prompt \
            else e.video_prompt.rstrip() + soundscape_block(c.video_soundscape, c.video_music)
    if e.camera and e.camera not in legacy:
        legacy = f"{e.camera}，{legacy}"
    if e.expression and e.expression not in legacy:
        body, sep, tail = legacy.partition("\n\noverall_soundscape:")
        legacy = f"{body}，表情：{e.expression}{sep}{tail}"
    c.legacy_video_prompt = legacy
    return c


def episode_dir(ep: int) -> Path:
    return RENDERS / f"ep{ep:02d}"


def load_episode_edit(ep: int) -> EpisodeEdit:
    return EpisodeEdit.model_validate(_read_json(episode_dir(ep) / "episode_edit.json", {}))


def save_episode_edit(ep: int, edit: EpisodeEdit) -> EpisodeEdit:
    edit.updated_at = now()
    _write_json(episode_dir(ep) / "episode_edit.json", edit.model_dump())
    return edit


CHAR_NAMES = {v: k for k, v in R.CHAR_IDS.items()}


def shot_looks_map(shot_id: str) -> dict[str, dict]:
    """data/looks 的解析结果 → {角色名: {look_id, makeup: [...], items: [...]}}；读不到就空（退回 M0 底板）"""
    try:
        from ns_agent.looks import LookStore
        from ns_agent.ref_overrides import canvas_part_images
        store = LookStore()
        res = store.resolver()
        sl = res.shot_looks(store.shot(shot_id))
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for r in sl.characters:
        name = CHAR_NAMES.get(r.character, r.character)
        makeup = r.slots["makeup"].items if "makeup" in r.slots else []
        out[name] = {"look_id": r.look_id, "makeup": makeup,
                     "items": [i for slot in r.slots.values() for i in slot.items]}
        try:   # W5：画布参考覆盖换过的单件 → 该单品的图优先当参考（refs.plan_refs part_images）
            parts = canvas_part_images(res, r.character, sl.episode, shot_id, ROOT)
        except Exception:  # noqa: BLE001
            parts = {}
        if parts:
            out[name]["part_images"] = parts
    return out


def _plate_for(name: str, text: str, look: dict) -> str | None:
    plan = R.plan_refs(text, [name], {name: look})
    return plan.face.path.relative_to(ROOT).as_posix() if plan.face else None


def compile_with_edits(script: ScriptImport, shot_id: str, loras: set[str], method: str | None = None) -> CompiledShot:
    from ns_agent.frame_spec import build_spec
    ep, s = find_shot(script, shot_id)
    edit = load_edit(shot_id)
    ee = load_episode_edit(ep.number)
    bf, bl = boost_text(ee.boost_first, edit.boost_first), boost_text(ee.boost_last, edit.boost_last)
    leg, cur = _legacy_and_primary()
    # MERGE-VID-O（ISS-1103-024206-0474）：H3 运镜规范化按本剧取词表（compile_shot 把 framing_project 传给 normalize_video_prompt）；
    # legacy 剧（V1）照旧不传，行为不变
    c = compile_shot(ep, s, script.globals, loras, boost_first=bf, boost_last=bl, soundscape=edit.soundscape,
                     music=edit.music, episode_music=ee.music, framing_project=cur if cur and cur != leg else None)
    looks = shot_looks_map(shot_id)
    for lk in looks.values():
        lk["makeup_design"] = R.makeup_design(lk.get("makeup", []), c.image_body)
    triggers = {t.trigger for t in script.globals.lora_triggers if t.file in loras}
    from ns_agent.frame_spec import load_timeline_index
    spec = build_spec(ep, s, script.globals, looks=looks, plate_for=_plate_for, available_triggers=triggers,
                      boost_first=bf, boost_last=bl, timeline_shot=load_timeline_index(ROOT).get(shot_id))
    method = method or PROMPT_METHOD
    if method == "spec":
        c.image_prompt = spec.model_prompt.qwen
        c.last_prompt = spec.model_prompt.qwen_last
    c.prompt_method = method
    c.visual_spec = spec.model_dump()
    try:   # T51：本镜输入哈希（与 take 里的比较可知是否过期）
        from ns_agent.state_timeline import shot_inputs_sha
        c.inputs_sha = shot_inputs_sha(shot_id, ROOT) or ""
    except Exception:  # noqa: BLE001
        pass
    c = apply_edit(c, edit, script.globals.image_suffix)
    _write_json(shot_dir(shot_id) / "visual_spec.json", c.visual_spec)
    _write_json(shot_dir(shot_id) / "compiled.json", asdict(c))
    return c


def available_loras(client: ComfyClient) -> set[str]:
    spec = client.object_info()["LoraLoaderModelOnly"]["input"]["required"]["lora_name"][0]
    return set(spec) if isinstance(spec, list) else set()


# ------------------------------------------------------------------ 渲染

def _clear_error(shot_id: str):
    (shot_dir(shot_id) / "errors.json").unlink(missing_ok=True)


_DEFAULT = object()


def _default_score(client, c: CompiledShot, plate: Path | None, *, last: bool = False, first: Path | None = None):
    """C5 评分（score.py）。首帧：facts = Frame Spec 里的首帧条目（剧本 img 分句 + motion 起点 + 导演加强）；
    尾帧：结束状态 + 导演加强 + 连续性，用“左首帧 | 右候选”拼图给看图模型"""
    from . import score as S
    spec = c.visual_spec or {}
    if last:
        facts = [f["text"] for f in spec.get("facts", []) if f.get("frame") in ("last", "both")] or [c.end_state]
        facts = [f for f in facts if f] + [S.CONTINUITY]
    else:
        facts = [f["text"] for f in spec.get("facts", []) if f.get("frame") in ("first", "both")] \
            or [x for x in c.image_body.split("，") if x]

    facts = facts + [f for f in extra_facts(c) if f not in facts and (plate is not None or f == SHOD_FACT)]

    def fn(path: Path, qc: dict | None) -> dict:
        if last:
            pair = S.side_by_side(first, path, path.with_name(path.stem + "_pair.jpg"))
            return S.score_image(path, client=client, facts=facts, plate=plate, dark=c.dark, qc=qc,
                                 colour_ref=first, vlm_image=pair, vlm_prompt=S.VLM_PROMPT_LAST)
        return S.score_image(path, client=client, facts=facts, plate=plate, dark=c.dark, qc=qc)
    return fn


def _visible_characters(c: CompiledShot) -> list[str]:
    """需要脸部底板的角色：画面内、非剪影（Frame Spec）；没有 spec 时看角色锁"""
    spec = c.visual_spec or {}
    if spec.get("characters") is not None:
        names = [x["name"] for x in spec["characters"] if x.get("clarity") not in ("silhouette",)]
    else:
        names = [n for n in R.CHAR_IDS if f"【{n}】" in c.image_prompt]
    return sorted(names, key=lambda n: n != "林晚")


def _ref_plan(c: CompiledShot, shot_id: str) -> R.RefPlan:
    looks = shot_looks_map(shot_id)
    text = c.image_body or c.image_prompt
    return R.plan_refs(text, _visible_characters(c), looks)


def _round_plan(plan: R.RefPlan, rnd: int, missing: list[str], back_shot: bool) -> tuple[R.RefPlan, float, str]:
    """下一轮只改技术参数（不改剧本文字）：缩小参考图（越小越不主导构图）；背影镜头核对不过就去掉脸部底板"""
    if rnd == 1 or not plan.refs:
        return plan, 1.0, ""
    if back_shot and any(w in m for m in missing for w in ("背", "背影", "背对")):
        return plan.without_face(), 1.0, "背影未成立：去掉脸部底板"
    return plan, 0.67, "参考图缩小到 2/3"


def _upload_refs(client, plan: R.RefPlan, scale: float) -> list[str]:
    return [client.upload_image(R.prescale(r, scale), subfolder="ns_final/refs") for r in plan.refs]


def _run_candidates(client, shot_id: str, stage: str, c: CompiledShot, *, build_wf, prompt_for, seeds_key: str,
                    fixed_seed, attempts: int, timeout_s: float, qc_fn, grade_fn, score_fn, candidates: int,
                    rounds: int, plan_for_round, meta: dict) -> Take:
    """多轮多候选的公共循环：出图 → 调色 → 质检 → 评分；全部留档，最高分设为当前"""
    d = shot_dir(shot_id)
    rel_dir = _RP.take_rel_dir(d, DATA, RENDERS)   # W33：带剧名前缀
    errors, tries, last_err = 0, 0, None
    best: Take | None = None
    missing: list[str] = []
    notes = []
    for rnd in range(1, rounds + 1):
        ctx = plan_for_round(rnd, missing)
        if ctx.get("note"):
            notes.append(f"第{rnd}轮：{ctx['note']}")
        got_round = []
        k = 0
        while k < candidates:
            n = _next_n(shot_id, stage)
            seed = fixed_seed if fixed_seed is not None and tries == 0 else seed_for(seeds_key, n + tries)
            tries += 1
            prompt = prompt_for(ctx)
            try:
                entry, secs = client.run(build_wf(ctx, prompt, seed), timeout_s=timeout_s)
                item = next(f for f in client.output_files(entry) if f["filename"].endswith(".png"))
            except ComfyUnavailable:
                raise
            except (ComfyError, StopIteration) as e:
                last_err = e
                errors += 1
                if errors > attempts:
                    raise ComfyError(f"{shot_id} {stage} 失败: {e}") from e
                continue
            k += 1
            raw = f"{rel_dir}/{stage}_{n:03d}_raw.png" if grade_fn else f"{rel_dir}/{stage}_{n:03d}.png"
            client.download(item, DATA / raw)
            rel, grade = raw, None
            if grade_fn:
                rel = f"{rel_dir}/{stage}_{n:03d}.png"
                grade = grade_fn(DATA / raw, DATA / rel)
            qc = qc_fn(DATA / rel) if qc_fn else None
            sc = score_fn(DATA / rel, qc) if score_fn else None
            take = Take(n=n, stage=stage, file=rel, raw_file=raw if grade_fn else None, seed=seed, prompt=prompt,
                        negative=meta.get("negative", ""), seconds=round(secs, 1), created_at=now(),
                        workflow=meta["workflow"](ctx), reference=ctx.get("reference"),
                        references=ctx.get("references"), qc=qc, grade=grade, score=sc, round=rnd,
                        remote=CC.remote_info(client, DATA / raw))
            _add_take(shot_id, take, activate=False)
            if qc and qc["nudity"]:   # 只记录：2026-10-30 取消成人过滤器后裸露不再否决
                _log_qc(shot_id, take.model_dump())
            got_round.append(take)
        for t in got_round:
            if best is None or (t.score or {}).get("total", 1.0) > (best.score or {}).get("total", 1.0):
                best = t
        if best is not None:
            total = (best.score or {}).get("total")
            if total is None or total >= SCORE_PASS:
                break
            missing = (best.score or {}).get("missing", [])
    if best is None:
        raise ComfyError(f"{shot_id} {stage} 失败: {last_err}")
    with shot_lock(shot_id):   # 封板修复：每镜锁（与 API 设当前 / 上传互斥）
        set_active(shot_id, stage, best.n)
        data = load_takes(shot_id)
        review = data.setdefault("review", {})
        total = (best.score or {}).get("total")
        if total is not None and total < SCORE_PASS:
            review[stage] = {"at": now(), "best": best.n, "total": total, "pass": SCORE_PASS,
                             "missing": (best.score or {}).get("missing", []), "notes": notes,
                             "reason": f"{rounds} 轮后最高分 {total} 低于 {SCORE_PASS}"}
        else:
            review.pop(stage, None)
        _save_takes(shot_id, data)
    _clear_error(shot_id)
    return best


def render_image(client: ComfyClient, script: ScriptImport, shot_id: str, *, seed: int | None = None,
                 attempts: int = 2, timeout_s: float = 900, size=IMAGE_SIZE,
                 face_lock: bool | None = None, qc_fn=None, candidates: int | None = None,
                 rounds: int | None = None, score_fn=_DEFAULT, grade: bool = True, ref_mode: str | None = None,
                 method: str | None = None, prompt_override: str | None = None,
                 scene_ref: bool | None = None) -> Take:
    """首帧（C5 多轮多候选）。scene_ref：带本镜场景定稿底图作参考（S3；None = SCENE_PLATE_REFS）。prompt_override：画布这一次整段替换的首帧提示词（原样，不写 edits.json）。gpu3 认领的镜头直接拒绝（Gpu3Owned）。face_lock: None = 按 face_lock_default()；True/False 强制开关。
    ref_mode: plate（身份底板 + 作用说明，默认）| portrait（旧：成品定妆图裁成画布比例，只做 A/B 对照）。
    参考编辑对所有镜头开放；裸露质检只记录，不否决（2026-10-30 取消成人过滤器）。
    score_fn=None 关闭评分（每轮第一张即当前）；grade=False 不调色。"""
    check_not_legacy_project(script, shot_id, "render_image")
    check_not_gpu3(shot_id)
    c = compile_with_edits(script, shot_id, available_loras(client), **({"method": method} if method else {}))
    if prompt_override:
        c.image_prompt = prompt_override
    edit = load_edit(shot_id)
    use_lock = face_lock_default() if face_lock is None else face_lock
    mode = ref_mode or REF_MODE
    plan = R.RefPlan()
    portrait = None
    if use_lock:
        if mode == "portrait":
            portrait = face_reference(c)
        else:
            plan = _ref_plan(c, shot_id)
    if portrait or plan.refs:
        check_face_lock_allowed(c)
    # S3（fredw-92/S-back）：本镜场景的定稿底图（或画布本镜 / 场景层选的底图）排在人物参考之后，
    # 用途说明由 RefPlan.role_lines 生成（refs.ROLE_SCENE）
    scene_plate = None
    if not portrait and (SCENE_PLATE_REFS if scene_ref is None else scene_ref):
        scene_plate = shot_scene_plate(script, shot_id)
        if scene_plate:
            plan = plan.with_scene(ROOT / scene_plate["path"])
    model = image_model(c)
    negative = image_negative(c, model)
    qc_fn = qc_fn or qc_with_vlm(client)
    if score_fn is _DEFAULT:
        score_fn = _default_score(client, c, plan.face.path if plan.face else
                                  (R.face_plate("lin_wan", R.MAKEUP_DEFAULT)[0] if portrait else None))
    back_shot = R.shot_angle(c.image_body) == "B"
    uploads: dict = {}
    n_compose = candidates or CANDIDATES
    facts = [f["text"] for f in (c.visual_spec or {}).get("facts", []) if f.get("frame") in ("first", "both")]
    facts += [f for f in extra_facts(c) if f not in facts]   # 构图预选也核对“穿鞋”
    holder: dict = {}
    two_stage = mode == "plate" and plan.face is not None   # 只有场景底图（没有身份底板）时直接多参考编辑
    fixed = seed if seed is not None else edit.image_seed

    def compose(rnd: int) -> dict | None:
        """文生图出 n_compose 张构图 → 裸露质检 → 看图模型核对剧本 → 取最好的一张"""
        rel_dir = _RP.take_rel_dir(shot_dir(shot_id), DATA, RENDERS)   # W33
        results = []
        for j in range(n_compose):
            s_ = fixed if fixed is not None and rnd == 1 and j == 0 else seed_for(shot_id + "/compose", rnd * 100 + j)
            try:
                entry, secs = client.run(W.qwen_t2i(c.image_prompt, negative, *size, s_, f"ns_final/{shot_id}_compose",
                                                    m=model, loras=c.image_loras), timeout_s=timeout_s)
                item = next(f for f in client.output_files(entry) if f["filename"].endswith(".png"))
            except ComfyUnavailable:
                raise
            except (ComfyError, StopIteration):
                continue
            rel = f"{rel_dir}/compose_{_next_n(shot_id, 'image'):03d}_r{rnd}_{j}.png"
            client.download(item, DATA / rel)
            qc = qc_fn(DATA / rel) if qc_fn else None
            r = {"file": rel, "seed": s_, "seconds": round(secs, 1), "nudity": bool(qc and qc["nudity"]),
                 "clip_bare": (qc or {}).get("clip_bare", 0.0)}
            if score_fn is not None and facts:
                from . import score as S
                try:
                    v = S.vlm_check(client, DATA / rel, facts)
                    r.update(fidelity=v["score"], missing=v["missing"])
                except Exception as e:  # noqa: BLE001
                    r["error"] = f"{type(e).__name__}"
            results.append(r)
        if not results:
            return {"all": results, "best": None}
        # 剧本核对优先；裸露只记录，不参与淘汰（2026-10-30 取消成人过滤器）
        best = max(results, key=lambda r: (r.get("fidelity") or 0))
        return {"all": results, "best": best}

    def plan_for_round(rnd, missing):
        if portrait:
            if "p" not in uploads:
                uploads["p"] = client.upload_image(portrait[1], subfolder="ns_final/refs")
            return {"portrait": uploads["p"], "reference": portrait[1].relative_to(ROOT).as_posix(),
                    "references": [portrait[1].relative_to(ROOT).as_posix()]}
        if two_stage:
            comp = compose(rnd)
            holder["compose"] = comp
            best = comp["best"]
            if best:
                from PIL import Image
                src = DATA / best["file"]
                small = src.with_name(src.stem + "_ref.png")
                im = Image.open(src).convert("RGB")
                h = round(im.height * COMPOSE_WIDTH / im.width / 32) * 32

                def make_small():
                    im.resize((COMPOSE_WIDTH, h), Image.LANCZOS).save(small)
                    return small
                key = (tuple(r.path for r in plan.refs), 1.0)
                if key not in uploads:
                    uploads[key] = _upload_refs(client, plan, 1.0)
                refs = [_rel_root(r.path) for r in plan.refs]
                # 构图刚从同一 ComfyUI 下载：[output] 引用 + 图内缩放，不上传（fredw-92/F1）
                compose_name = CC.input_image(client, src, "ns_final/refs", size=(COMPOSE_WIDTH, h),
                                              prepare=make_small)
                return {"plan": plan, "compose": compose_name,
                        "names": uploads[key], "reference": refs[0],
                        "references": [f"data/{best['file']}", *refs],
                        "note": f"构图 {len(comp['all'])} 选 1（剧本核对 {best.get('fidelity')}）"}
        rp, scale, note = _round_plan(plan, rnd, missing, back_shot)
        key = (tuple(r.path for r in rp.refs), scale)
        if key not in uploads:
            uploads[key] = _upload_refs(client, rp, scale) if rp.refs else []
        refs = [_rel_root(r.path) for r in rp.refs]
        return {"plan": rp, "names": uploads[key], "note": note, "reference": refs[0] if refs else None,
                "references": refs or None}

    def prompt_for(ctx):
        if ctx.get("portrait"):
            return f"{FACE_LOCK_PREFIX.format(name=portrait[0])}{c.image_prompt}"
        if ctx.get("compose"):
            face = ctx["plan"].face
            name = face.character if face else "人物"
            others = R.RefPlan([r for r in ctx["plan"].refs if r.kind != "face"])
            return (COMPOSE_ROLE.format(name=name, look=IDENTITY_LOOK.get(name, ""))
                    + others.role_lines(start=3, tail="") + c.image_prompt)
        return f"{ctx['plan'].role_lines()}{c.image_prompt}" if ctx["names"] else c.image_prompt

    def build_wf(ctx, prompt, s):
        prefix = f"ns_final/{shot_id}_frame"
        if ctx.get("portrait"):
            return W.qwen_edit(prompt, negative, ctx["portrait"], *size, s, prefix, m=model, loras=c.image_loras)
        if ctx.get("compose"):
            return W.qwen_edit(prompt, negative, [ctx["compose"], *ctx["names"]], *size, s, prefix, m=model,
                               loras=c.image_loras, ref_resolution=0, crop=False)
        if ctx["names"]:
            return W.qwen_edit(prompt, negative, ctx["names"], *size, s, prefix, m=model, loras=c.image_loras,
                               ref_resolution=0, crop=False)
        return W.qwen_t2i(prompt, negative, *size, s, prefix, m=model, loras=c.image_loras)

    def workflow_name(ctx):
        base = "qwen_edit" if ctx.get("portrait") else ("qwen_two_stage" if ctx.get("compose") else
                                                         "qwen_edit_plate" if ctx.get("names") else "qwen_t2i")
        return base + ("" if model.turbo else "_base")

    def grade_fn(src, dst):
        from . import grade as G
        return G.grade_file(src, dst, strength=GRADE_STRENGTH, dark=c.dark)

    final_score = score_fn
    if two_stage and score_fn is not None:
        def final_score(path, qc):
            sc = score_fn(path, qc)
            comp = holder.get("compose") or {}
            sc["compose"] = [{k: r.get(k) for k in ("file", "seed", "fidelity", "nudity", "clip_bare", "seconds")}
                             for r in comp.get("all", [])]
            return sc

    best = _run_candidates(
        client, shot_id, "image", c, build_wf=build_wf, prompt_for=prompt_for, seeds_key=shot_id,
        fixed_seed=fixed, attempts=attempts, timeout_s=timeout_s,
        qc_fn=qc_fn, grade_fn=grade_fn if grade else None, score_fn=final_score,
        # 两段式：每轮 n_compose 张构图里选一张，再做 1 次换脸编辑
        candidates=1 if two_stage else n_compose, rounds=rounds or ROUNDS, plan_for_round=plan_for_round,
        meta={"negative": negative, "workflow": workflow_name})
    if scene_plate:
        best = _note_take(shot_id, "image", best.n, scene_plate=scene_plate)
    return best


def _rel_root(p: Path) -> str:
    """参考图路径（相对项目根；不在项目根下时写绝对路径，不报错）"""
    try:
        return Path(p).relative_to(ROOT).as_posix()
    except ValueError:
        return Path(p).as_posix()


SCENE_PLATE_REFS = True     # S3：首帧自动带本镜场景定稿底图（False = 关）


def shot_scene_plate(script: ScriptImport, shot_id: str) -> dict | None:
    """本镜首帧要带的场景底图（ns_render.scene_plate.shot_plate）；出错 / 没有 → None（照常出图）"""
    try:
        from . import scene_plate as SP
        return SP.shot_plate(script, shot_id, ROOT, RENDERS)
    except Exception:  # noqa: BLE001
        return None


def _note_take(shot_id: str, stage: str, n: int, **fields) -> Take:
    """给一个已存的版本补记字段（takes.json），返回更新后的 Take"""
    with shot_lock(shot_id):
        data = load_takes(shot_id)
        t = next(t for t in data["takes"] if t["stage"] == stage and t["n"] == n)
        t.update(fields)
        _save_takes(shot_id, data)
    return Take.model_validate(t)


def refine_image(client: ComfyClient, script: ScriptImport, shot_id: str, *, seed: int | None = None,
                 timeout_s: float = 900, qc_fn=None, score_fn=_DEFAULT, grade: bool = True) -> Take:
    """写实精修（A1，research v8 §4）：当前首帧的原图 → Base + 胶片纹理 LoRA + 纹理修复 VAE，低重绘 0.3，
    同提示词、同身份底板。新版本留档；评分不比原版低太多才设为当前，否则记 review。"""
    check_not_gpu3(shot_id)
    data = load_takes(shot_id)
    act = data["active"].get("image")
    src_take = next((t for t in data["takes"] if t["stage"] == "image" and t["n"] == act), None)
    if not src_take:
        raise ComfyError(f"{shot_id} 还没有首帧，不能精修")
    if (src_take.get("workflow") or "").startswith("qwen_refine"):
        return Take.model_validate(src_take)
    c = compile_with_edits(script, shot_id, available_loras(client))
    loras_ok = available_loras(client)
    extra = [(f, w) for f, w in REFINE_LORAS if f in loras_ok]
    plan = _ref_plan(c, shot_id)
    if plan.refs:
        check_face_lock_allowed(c)
    names = _upload_refs(client, plan, 1.0) if plan.refs else []
    src = DATA / (src_take.get("raw_file") or src_take["file"])
    init = client.upload_image(src, subfolder="ns_final/refs")
    negative = image_negative(c, REFINE_MODEL)
    prompt = (plan.role_lines() if names else "") + REFINE_PREFIX + c.image_prompt
    qc_fn = qc_fn or qc_with_vlm(client)
    if score_fn is _DEFAULT:
        score_fn = _default_score(client, c, plan.face.path if plan.face else None)
    n = _next_n(shot_id, "image")
    s_ = seed if seed is not None else seed_for(shot_id + "/refine", n)
    # 有底板：底板作参考、首帧原图作初始 latent；没有底板：同一张图结构，只是不带参考
    wf = W._qwen_graph(prompt, negative, *IMAGE_SIZE, s_, f"ns_final/{shot_id}_refine", REFINE_MODEL,
                       [*c.image_loras, *extra], reference=names or None, ref_resolution=0, crop=False,
                       init_image=init, denoise=REFINE_DENOISE)
    entry, secs = client.run(wf, timeout_s=timeout_s)
    item = next(f for f in client.output_files(entry) if f["filename"].endswith(".png"))
    rel_dir = _RP.take_rel_dir(shot_dir(shot_id), DATA, RENDERS)   # W33
    raw, rel = f"{rel_dir}/image_{n:03d}_raw.png", f"{rel_dir}/image_{n:03d}.png"
    client.download(item, DATA / raw)
    gr = None
    if grade:
        from . import grade as G
        gr = G.grade_file(DATA / raw, DATA / rel, strength=GRADE_STRENGTH, dark=c.dark)
    else:
        rel = raw
    qc = qc_fn(DATA / rel) if qc_fn else None
    sc = score_fn(DATA / rel, qc) if score_fn and not (qc and qc["nudity"]) else None
    if sc is not None:
        sc["refined_from"] = act
    take = Take(n=n, stage="image", file=rel, raw_file=raw if grade else None, seed=s_, prompt=prompt,
                negative=negative, seconds=round(secs, 1), created_at=now(),
                workflow="qwen_refine_base", reference=plan.refs[0].path.relative_to(ROOT).as_posix()
                if plan.refs else None,
                references=[f"data/{src_take['file']} (image v{act})",
                            *(r.path.relative_to(ROOT).as_posix() for r in plan.refs)],
                qc=qc, grade=gr, score=sc, round=src_take.get("round"), remote=CC.remote_info(client, DATA / raw))
    _add_take(shot_id, take, activate=False)
    if qc and qc["nudity"]:
        _log_qc(shot_id, take.model_dump())
    prev = (src_take.get("score") or {}).get("total")
    new = (sc or {}).get("total")
    if prev is not None and new is not None and new < prev - REFINE_TOLERANCE:
        with shot_lock(shot_id):   # 封板修复：每镜锁
            data = load_takes(shot_id)
            data.setdefault("review", {})["refine"] = {"at": now(), "refined": take.n, "kept": act, "total": new,
                                                       "prev": prev, "reason": "精修版评分明显更低，保留原版"}
            _save_takes(shot_id, data)
        return Take.model_validate(src_take)
    set_active(shot_id, "image", take.n)   # 封板修复：_add_take 可能顺延版本号
    return take


LAST_ROLE_PLATE = "图{i}只提供{name}的面部身份与发型，用来保持人物长相一致；不要沿用图{i}的姿势、服装、背景和光线。"


def _last_prompt_with_plate(last_prompt: str, plate_line: str) -> str:
    """尾帧提示词第一句是“图1是本镜头的首帧画面。”，底板说明接在它后面"""
    if not plate_line:
        return last_prompt
    head, sep, tail = last_prompt.partition("。")
    return f"{head}{sep}{plate_line}{tail}" if sep else f"{plate_line}{last_prompt}"


def render_last_frame(client: ComfyClient, script: ScriptImport, shot_id: str, seed: int | None = None, *,
                      attempts: int = 2, timeout_s: float = 900, candidates: int | None = None,
                      rounds: int | None = None, qc_fn=None, score_fn=_DEFAULT, grade: bool = True,
                      model: W.QwenModel | None = None, method: str | None = None,
                      prompt_override: str | None = None) -> Take:
    """尾帧（C3，research v6 §5）：以当前首帧为图1做 Qwen 2.1 编辑，画面改为 motion 的结束状态
    （同一场景同一人物同一服装）；图2 = 该镜的身份底板（补身份）。调色以首帧为参考，避免首尾闪色。
    存为 takes/last_NNN.png（stage "last"），当前版本复制为 last.png；render_video 用它作 H3 尾帧。"""
    check_not_legacy_project(script, shot_id, "render_last_frame")
    check_not_gpu3(shot_id)
    d = shot_dir(shot_id)
    if not (d / "frame.png").exists():
        raise ComfyError(f"{shot_id} 还没有首帧，不能做尾帧")
    c = compile_with_edits(script, shot_id, available_loras(client), **({"method": method} if method else {}))
    if prompt_override:   # 画布这一次整段替换的尾帧提示词（原样）
        c.last_prompt = prompt_override
    if not c.last_prompt:
        raise ComfyError(f"{shot_id} 从 motion 里取不到结束状态，没有尾帧提示词")
    plan = _ref_plan(c, shot_id)
    plate = plan.face
    if plate:
        check_face_lock_allowed(c)
    m = model or LAST_MODEL
    negative = image_negative(c, m)
    qc_fn = qc_fn or qc_with_vlm(client)
    first_png = d / "frame.png"
    # 首帧参考：同比例预缩放（控制编码 token），本镜固定这一份
    from PIL import Image
    snap = DATA / "cache" / "last_src" / f"{shot_id}_{LAST_FIRST_FRAME_SIZE[0]}x{LAST_FIRST_FRAME_SIZE[1]}.png"
    snap.parent.mkdir(parents=True, exist_ok=True)

    def make_snap():
        Image.open(first_png).convert("RGB").resize(LAST_FIRST_FRAME_SIZE, Image.LANCZOS).save(snap)
        return snap
    # 首帧是同一 ComfyUI 的输出原样（画布首帧不调色）→ [output] 引用 + 图内缩放，不上传（fredw-92/F1）
    first_name = CC.input_image(client, first_png, "ns_final/refs", size=LAST_FIRST_FRAME_SIZE, prepare=make_snap)
    names = [first_name] + ([client.upload_image(R.prescale(plate), subfolder="ns_final/refs")] if plate else [])
    plate_line = LAST_ROLE_PLATE.format(i=2, name=plate.character) if plate else ""
    prompt = _last_prompt_with_plate(c.last_prompt, plate_line)
    active_first = load_takes(shot_id)["active"].get("image")
    refs = [f"data/{_RP.shot_rel_dir(d, DATA, RENDERS)}/frame.png (image v{active_first})"] +         ([plate.path.relative_to(ROOT).as_posix()] if plate else [])
    if score_fn is _DEFAULT:
        score_fn = _default_score(client, c, plate.path if plate else None, last=True, first=first_png)

    def grade_fn(src, dst):
        from . import grade as G
        return G.grade_file(src, dst, reference=first_png, strength=LAST_GRADE_STRENGTH, dark=c.dark)

    return _run_candidates(
        client, shot_id, "last", c,
        build_wf=lambda ctx, p, s: W.qwen_edit(p, negative, names, *IMAGE_SIZE, s, f"ns_final/{shot_id}_last",
                                               m=m, loras=c.image_loras, ref_resolution=0, crop=False),
        prompt_for=lambda ctx: prompt, seeds_key=shot_id + "/last",
        fixed_seed=seed if seed is not None else load_edit(shot_id).last_seed, attempts=attempts,
        timeout_s=timeout_s, qc_fn=qc_fn, grade_fn=grade_fn if grade else None, score_fn=score_fn,
        candidates=candidates or CANDIDATES, rounds=rounds or ROUNDS,
        plan_for_round=lambda rnd, missing: {"reference": refs[0], "references": refs},
        meta={"negative": negative, "workflow": lambda ctx: "qwen_edit_last" + ("" if m.turbo else "_base")})


def resolve_last_frame(script: ScriptImport, shot_id: str, edit: ShotEdit) -> Path | None:
    if not edit.last_frame:
        return None
    if edit.last_frame == "next":
        nxt = next_shot_id(script, shot_id)
        p = shot_dir(nxt) / "frame.png" if nxt else None
        return p if p and p.exists() else None
    p = DATA / edit.last_frame
    return p if p.exists() else None


def active_last_frame(shot_id: str) -> Path | None:
    data = load_takes(shot_id)
    n = data["active"].get("last")
    t = next((t for t in data["takes"] if t["stage"] == "last" and t["n"] == n), None)
    return DATA / t["file"] if t and (DATA / t["file"]).exists() else None


def frames_for_duration(seconds: int) -> int:
    """剧本时长 → H3 帧数：SOP §6 对照表 / §9A.3（ceil(秒×24) 向上取到 17k+5，124–362；8 s = 192 帧）"""
    return frames_for_seconds(seconds)


def render_video(client: ComfyClient, script: ScriptImport, shot_id: str, *, seed: int | None = None,
                 frames: int | None = None, attempts: int = 2, timeout_s: float = 3600, size=VIDEO_SIZE) -> Take:
    check_not_gpu3(shot_id)
    d = shot_dir(shot_id)
    if not (d / "frame.png").exists():
        raise ComfyError(f"{shot_id} 还没有首帧")
    c = compile_with_edits(script, shot_id, available_loras(client))
    edit = load_edit(shot_id)
    first = client.upload_image(d / "frame.png")
    # 尾帧来源：画布指定（next / 上传）优先，其次当前独立尾帧（stage "last"），都没有就只用首帧
    last_path = resolve_last_frame(script, shot_id, edit) or active_last_frame(shot_id)
    last = client.upload_image(last_path) if last_path else None
    n = _next_n(shot_id, "video")
    if frames is None:
        _, shot = find_shot(script, shot_id)
        frames = frames_for_duration(getattr(shot, "duration", 10) or 10)
    frames = min(max(snap_frames(frames), W.H3_FRAMES_MIN), W.H3_FRAMES_MAX)   # 17k+5，训练范围 124–362
    # 失败时：先换种子，再缩短到 158 帧
    plans = [(frames, 0), (frames, 1), (min(frames, 158), 2)][:attempts + 1]
    fixed = seed if seed is not None else edit.video_seed
    last_err = None
    for fr, attempt in plans:
        s = fixed if fixed is not None and attempt == 0 else seed_for(shot_id + "/v", n + attempt)
        wf = W.h3_fl2va(c.video_prompt, first, *size, fr, s, f"ns_final/{shot_id}_video",
                        loras=c.video_loras, last_frame=last)
        try:
            entry, secs = client.run(wf, timeout_s=timeout_s)
            item = next(f for f in client.output_files(entry) if f["filename"].endswith(".mp4"))
            base = f"{_RP.take_rel_dir(d, DATA, RENDERS)}/video_{n:03d}"   # W33
            client.download(item, DATA / f"{base}.mp4")
            lf = extract_last_frame(DATA / f"{base}.mp4", DATA / f"{base}_last.png")
            take = Take(n=n, stage="video", file=f"{base}.mp4", seed=s, prompt=c.video_prompt,
                        seconds=round(secs, 1), frames=fr, created_at=now(),
                        last_frame=f"{base}_last.png" if lf else None, workflow="h3_fl2va")
            _add_take(shot_id, take)
            _clear_error(shot_id)
            return take
        except ComfyUnavailable:
            raise
        except (ComfyError, StopIteration) as e:
            last_err = e
    raise ComfyError(f"{shot_id} 视频失败: {last_err}")
