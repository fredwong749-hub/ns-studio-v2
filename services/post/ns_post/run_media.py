"""按剧取配乐 / 音色（OVERSIGHT-F P1-13a，STYLE-V2 11-02；L-126 完全切割、L-131 配乐曲库、L-132 音色）。

资产包模式的剧（runs.json 里登记了 assets_manifest 的剧，= V2）：
  - 配乐：只认 config/music_v2.json（run 必须一致；登记曲目逐条 sha256 校验）。集级 BGM 目录 = data/music/<run>/，
    epNN.json 必须写 library_track（曲库里的 id）且 library_sha256 与曲库一致；剪辑工程换曲的文件也必须在本剧目录或曲库里。
    绝不读 data/music/series10（V1）。
  - 音色：只认 config/voices_v2.json 里 selected + enabled 的候选（ref.wav 逐个 sha256 校验）；技术段（模型 / 解释器 / 远程合成机器）
    沿用 config/voices.json 的同名键（不含任何 V1 角色）。
  - 没登记 / sha 不符 / run 不符 → V2MediaError（失败关闭，不退回 V1）。
legacy / 没登记资产包的剧：配乐目录 data/music/series10、音色 config/voices.json，行为不变。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
V1_MUSIC_DIR = "data/music/series10"
TECH_KEYS = ("tts_model", "asr_model", "speech_python", "remote_tts")   # voices.json 里与角色无关的技术段


class V2MediaError(RuntimeError):
    """资产包模式的剧取配乐 / 音色失败（没登记、sha 不符、读到 V1）"""


def _runs(root: Path):
    import sys
    sys.path.insert(0, str(root / "services" / "agent"))
    from ns_agent import run_paths as RP
    return RP.runs_module(root)


def is_asset_pack_run(run: str | None, root: Path = ROOT) -> bool:
    m = _runs(root)
    return bool(m is not None and run and m.asset_manifest(run, root))


def _rel(p: Path, root: Path) -> str:
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(p)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _load(p: Path) -> dict:
    if not p.is_file():
        raise V2MediaError(f"没有 {_rel(p, ROOT)}（V2 配乐 / 音色须先登记）")
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 配乐
def music_library(run: str, root: Path = ROOT) -> dict:
    """config/music_v2.json → {track_id: track}；逐条 sha 校验"""
    lib = _load(root / "config" / "music_v2.json")
    if lib.get("run") != run:
        raise V2MediaError(f"config/music_v2.json 登记的剧是 {lib.get('run')}，不是 {run}")
    out = {}
    for t in lib.get("tracks") or []:
        f = root / t["file"]
        if not f.is_file():
            raise V2MediaError(f"曲库文件不存在：{t['file']}")
        if _sha(f) != t["sha256"]:
            raise V2MediaError(f"曲库文件 sha 不符：{t['file']}")
        out[t["id"]] = t
    if not out:
        raise V2MediaError("config/music_v2.json 没有登记的曲目")
    return out


def music_dir(run: str | None, root: Path = ROOT) -> Path:
    """本剧的集级 BGM 目录：V2 = data/music/<run>/（先校验曲库）；legacy = data/music/series10"""
    if not is_asset_pack_run(run, root):
        return root / V1_MUSIC_DIR
    music_library(run, root)
    d = root / "data" / "music" / run
    if d.resolve() == (root / V1_MUSIC_DIR).resolve():
        raise V2MediaError("V2 的配乐目录解析到了 V1 的 data/music/series10")
    return d


def check_episode_music(run: str | None, ep: int, mdir: Path, edit_file: str | None = None, root: Path = ROOT) -> dict:
    """V2：本集 BGM（epNN.json）和剪辑工程换曲文件必须来自曲库；legacy 不查。返回 {checked, track?}"""
    if not is_asset_pack_run(run, root):
        return {"checked": False}
    lib = music_library(run, root)
    libfiles = {(root / t["file"]).resolve() for t in lib.values()}
    if edit_file:
        ef = Path(edit_file) if Path(edit_file).is_absolute() else root / edit_file
        if ef.resolve() not in libfiles and mdir.resolve() not in ef.resolve().parents:
            raise V2MediaError(f"剪辑工程里的配乐 {edit_file} 不在 V2 曲库，也不在 {_rel(mdir, root)}")
    j = mdir / f"ep{ep:02d}.json"
    if not j.is_file():
        return {"checked": True, "track": None}        # 本集还没配乐（L-118 剪辑锁定后再配）：合成跳过配乐层
    meta = json.loads(j.read_text(encoding="utf-8"))
    tid = meta.get("library_track")
    if tid not in lib:
        raise V2MediaError(f"{_rel(j, root)} 的 library_track={tid!r} 不在 config/music_v2.json")
    if meta.get("library_sha256") != lib[tid]["sha256"]:
        raise V2MediaError(f"{_rel(j, root)} 的 library_sha256 与曲库 {tid} 不符")
    return {"checked": True, "track": tid}


# ---------------------------------------------------------------- 音色
def _speaker_cast(run: str, root: Path) -> dict:
    import sys
    sys.path.insert(0, str(root / "services" / "agent"))
    from ns_agent import cast_table as CT
    cast = CT.speaker_cast(run)
    if not cast:
        raise V2MediaError(f"{run} 没有说话人表（ns_agent.cast_table.speaker_cast）")
    return cast


def voice_config(run: str | None, root: Path = ROOT) -> dict:
    """按剧的音色配置（与 config/voices.json 同形）。V2：characters 的键 = 人物 char_id（ns_agent.cast_table.speaker_cast，
    来自 P1 人物表），音色只取 voices_v2.json cast_voice.map 映射到的、项目方选定（selected + enabled）的角色槽；
    人物表里没映射的人进 unvoiced（配到时 voice_for 报错，不拿女主顶替）；cast = 说话人归属用的名字 / 男女 / 女主"""
    v1 = json.loads((root / "config" / "voices.json").read_text(encoding="utf-8"))
    if not is_asset_pack_run(run, root):
        return v1
    v2 = _load(root / "config" / "voices_v2.json")
    if not v2.get("enabled"):
        raise V2MediaError("config/voices_v2.json 还没启用（项目方没挑定）")
    slots = {}
    for role, r in (v2.get("roles") or {}).items():
        sel = [c for c in r.get("candidates") or [] if c.get("selected") and c.get("enabled")]
        if len(sel) != 1:
            raise V2MediaError(f"voices_v2.json 角色槽 {role} 启用的候选数 = {len(sel)}（应为 1）")
        c = sel[0]
        ref = root / c["ref_wav"]
        if not ref.is_file() or _sha(ref) != c["ref_sha256"]:
            raise V2MediaError(f"{role} 的 ref.wav 不存在或 sha 不符：{c.get('ref_wav')}")
        meta_p = root / c["dir"] / "meta.json"
        meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.is_file() else {}
        slots[role] = (c, meta, meta_p)
    cast = _speaker_cast(run, root)
    cmap = (v2.get("cast_voice") or {}).get("map") or {}
    id_name = {}
    for n, cid in cast["names"].items():
        id_name.setdefault(cid, n)
    chars, unvoiced = {}, {}
    for cid in dict.fromkeys(cast["names"].values()):
        role = cmap.get(cid)
        if role is None:
            unvoiced[cid] = f"{id_name.get(cid, cid)}（{cid}）还没有选定音色：待项目方选配角音色"
            continue
        if role not in slots:
            raise V2MediaError(f"cast_voice 把 {cid} 映射到 {role}，但 {role} 不是项目方选定的音色槽")
        c, meta, meta_p = slots[role]
        male = cid in cast["male"]
        if male != (meta.get("gender") == "male"):
            raise V2MediaError(f"{cid} 的性别和音色槽 {role} 不一致")
        chars[cid] = {"name": id_name.get(cid, cid), "gender": "male" if male else "female", "series": run,
                      "engine": (meta.get("clone") or {}).get("engine") or "cosyvoice3_zs",
                      "voice": f"{role}/{c['cand']}", "speed": 1.0, "vo_speed": 1.0, "ref_wav": c["ref_wav"],
                      "ref_sha256": c["ref_sha256"], "ref_text": meta.get("ref_text"),
                      "asset": meta_p.relative_to(root).as_posix() if meta_p.is_file() else None,
                      "source": f"config/voices_v2.json cast_voice → {role}（L-132）"}
    if cast["lead"] not in chars:
        raise V2MediaError(f"女主 {cast['lead']} 没有映射到音色")
    cfg = {k: v1[k] for k in TECH_KEYS if k in v1}
    cfg.update(series=run, characters=chars, unvoiced=unvoiced, groups={}, line_overrides={},
               voiceover_speaker=v2.get("voiceover_speaker") or cast["lead"],
               cast={"names": cast["names"], "male": cast["male"], "female": cast["female"], "lead": cast["lead"],
                     "source": cast["source"]},
               _source="config/voices_v2.json + ns_agent.cast_table.speaker_cast（按剧取，ns_post.run_media.voice_config）")
    return cfg
