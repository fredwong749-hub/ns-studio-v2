"""
图片精修循环（canvas-ui-spec-v3-merge §7、api_v1_contract §C；fredw-92/W2）。

四种改法里的前三种，作用在某一版首帧 / 尾帧上，每次生成一个新版本挂在父版本下面（修改链）：
- regen：整张重出（首帧 = pipeline.render_image；尾帧 = pipeline.render_last_frame，由定稿首帧编辑，C3）
- instruct：指令改图——父版本作 图1 + 底图（init_image），denoise 按幅度（EDIT_DENOISE）
- refs：换参考再改——图1 = 父版本，图2… = 新参考（Image 2.1 多参考编辑）；给了幅度时同样以父版本作底图

画布出图不做内容检测：每个任务只出 1 张，不评分、不看图判定、不做裸露否决（项目方 2026-10-30 夜）；
只留文件和技术元数据（种子、耗时、工作流、参考、父版本）。提示词 / 修改原话原样传给 Image 2.1。

定稿（lock）：takes.json locked[stage] = n。尾帧只能从定稿首帧做；定稿首帧换了 → 尾帧、视频标过期
（takes.json stale[stage]），新尾帧 / 视频由当前定稿首帧得到后清掉。
"""
from __future__ import annotations

from pathlib import Path

from . import pipeline as P
from . import render_paths as RP   # W33
from . import workflows as W
from .comfy_client import ComfyError, input_image, remote_info

# 改动幅度 → denoise（项目方在画布上选 小 / 中 / 大）；只在这一处定义
EDIT_DENOISE = {"small": 0.35, "medium": 0.55, "large": 0.75}
DEFAULT_STRENGTH = "medium"
MODES = ("regen", "instruct", "refs")
STAGE_OF = {"first": "image", "last": "last"}       # 接口的 stage → takes.json 的 stage
EDIT_REF_SIZE = (768, 1344)                          # 父版本作 图1 时的预缩放（与尾帧 C3 的首帧参考一致）
REF_MAX_SIDE = 1024                                  # 新参考图预缩放的长边上限（控制编码 token）
UPLOAD_JPEG_QUALITY = 95                             # 上传 gpu3 的底图 / 参考图格式（见 _scaled）
CANVAS_GRADE_FIRST = False                           # 画布首帧不自动调色（只出图）；尾帧沿用 C3 向首帧调色


def no_qc(_path) -> None:
    """画布出图不做质检（不做内容检测）"""
    return None


def identity_lora_files() -> set[str]:
    """config/auto_lora_triggers.json 里的出图身份 LoRA 文件（如 lin_wan_qi_v1.safetensors）"""
    try:
        from ns_agent.compiler import auto_lora_triggers
        return {str(e.get("file")) for es in auto_lora_triggers().values() for e in es if e.get("file")}
    except Exception:  # noqa: BLE001
        return set()


# ------------------------------------------------------------------ 定稿 / 过期

def locks(shot_id: str) -> dict:
    return dict(P.load_takes(shot_id).get("locked") or {})


def stale_map(shot_id: str) -> dict:
    return dict(P.load_takes(shot_id).get("stale") or {})


def _find(data: dict, stage: str, n: int) -> dict:
    t = next((t for t in data["takes"] if t["stage"] == stage and t["n"] == n), None)
    if t is None:
        raise KeyError(f"没有 {stage} v{n}")
    return t


def first_equivalents(data: dict, n: int | None) -> set:
    """fredw-92/Q1：与首帧 vN 构图相同的版本号——定稿档版本（mode=final）与它的父预览版同构图。
    由预览版做出的尾帧，在预览版换成其定稿档后不算过期（视频照常过期：关键帧像素变了，应由定稿版重出）"""
    out = {n}
    t = next((t for t in data.get("takes") or [] if t["stage"] == "image" and t["n"] == n), None)
    if t is not None and t.get("mode") == "final" and t.get("parent") is not None:
        out.add(t["parent"])
    return out


def lock(shot_id: str, stage: str, n: int) -> dict:
    """定稿某一版（同时设为当前）。首帧定稿换了版本 → 尾帧、视频过期（由旧首帧得到的）"""
    with P.shot_lock(shot_id):
        data = P.load_takes(shot_id)
        why = P.bad_reason(_find(data, stage, n))
        if why:   # fredw-92/UXF1：坏图不能定稿
            raise P.BadImage(f"{stage} v{n} 是{why}，不能定稿")
        prev = (data.get("locked") or {}).get(stage)
        data.setdefault("locked", {})[stage] = n
        data.setdefault("locked_src", {})[stage] = {"stage": stage, "n": n, "chain": "canvas"}
        for t in data["takes"]:
            if t["stage"] == stage:
                t["locked"] = t["n"] == n
        if stage == "image" and prev != n:
            stale = data.setdefault("stale", {})
            for st in ("last", "video"):
                act = (data.get("active") or {}).get(st)
                cur = next((t for t in data["takes"] if t["stage"] == st and t["n"] == act), None)
                same = first_equivalents(data, n) if st == "last" else {n}
                if cur is not None and cur.get("from_first") in same:
                    stale.pop(st, None)
                elif cur is not None or (data.get("locked") or {}).get(st) is not None:
                    stale[st] = {"reason": f"定稿首帧已改为 v{n}" + (f"（原 v{prev}）" if prev else ""),
                                 "at": P.now(), "first": n}
        P._save_takes(shot_id, data)
        P.set_active(shot_id, stage, n)
        return P.load_takes(shot_id)


def lock_last_from(shot_id: str, *, video_n: int | None = None, file: str | None = None,
                   chain: str = "canvas") -> dict:
    """尾帧来自视频版本（视频抽出的最后一帧，含 gpu3 链：尾帧挂在 video take 的 last_frame 上）时，
    也要在顶层 locked 里单独记 last：把那张图登记成一个 stage=last 的版本（source=video|gpu3，refs 记来源 take），
    再定稿它。file 相对 data/（gpu3 链传 renders_gpu3/... 的路径）。"""
    with P.shot_lock(shot_id):
        data = P.load_takes(shot_id)
        src = None
        if video_n is not None and chain == "canvas":
            vt = _find(data, "video", video_n)
            file = file or vt.get("last_frame")
            src = {"stage": "video", "n": video_n, "chain": "canvas", "file": file}
        else:
            src = {"stage": "video", "n": video_n, "chain": chain, "file": file}
        if not file or "\\" in file or ":" in file or ".." in file.split("/") or not (P.DATA / file).is_file():
            raise KeyError(f"找不到视频尾帧图片：{file}")
        n = P._next_n(shot_id, "last")
        take = P.Take(n=n, stage="last", file=file, seed=0, prompt="", created_at=P.now(),
                      workflow="video_last_frame", source="gpu3" if chain == "gpu3" else "video",
                      refs=[src], from_first=(data.get("locked") or {}).get("image"))
        P._add_take(shot_id, take, activate=False)
        out = lock(shot_id, "last", take.n)
        out.setdefault("locked_src", {})["last"] = src
        P._save_takes(shot_id, out)
        return out


def unlock(shot_id: str, stage: str) -> dict:
    with P.shot_lock(shot_id):
        data = P.load_takes(shot_id)
        (data.get("locked") or {}).pop(stage, None)
        (data.get("locked_src") or {}).pop(stage, None)
        for t in data["takes"]:
            if t["stage"] == stage:
                t["locked"] = False
        P._save_takes(shot_id, data)
        return data


def frames_ready(shot_id: str) -> tuple[bool, str]:
    """出视频前：首帧、尾帧都已定稿，且尾帧不过期（W3 video-jobs 用）"""
    data = P.load_takes(shot_id)
    lk, st = data.get("locked") or {}, data.get("stale") or {}
    missing = [name for k, name in (("image", "首帧"), ("last", "尾帧")) if lk.get(k) is None]
    if missing:
        return False, "还没定稿：" + "、".join(missing)
    if st.get("last"):
        return False, f"尾帧已过期（{st['last'].get('reason')}），请由定稿首帧重做尾帧并定稿"
    return True, ""


NO_PROMPT_SOURCES = ("upload", "next", "video", "gpu3")
NO_PROMPT_WORKFLOWS = ("upload", "video_last_frame")
EDIT_MODES = ("instruct", "refs")


def chain_basis(data: dict, take: dict) -> str:
    """fredw-92/F4：一个版本的过期比较基准（出图时编译器给的提示词）。
    指令改图 / 换参考的 prompt 是项目方原话，内容来自父版本 → 沿修改链取父版本的基准；
    上传 / 引用来的版本没有提示词可比 → ""。旧版本没记 basis_prompt 时按 prompt 兜底"""
    seen = set()
    t = take
    while t is not None and (t["stage"], t["n"]) not in seen:
        seen.add((t["stage"], t["n"]))
        if t.get("basis_prompt") is not None:
            return t["basis_prompt"]
        if t.get("workflow") in NO_PROMPT_WORKFLOWS or t.get("source") in NO_PROMPT_SOURCES:
            return ""
        if t.get("mode") not in EDIT_MODES:
            return t.get("prompt") or ""
        if t.get("parent") is None:
            return ""
        t = next((x for x in data["takes"] if x["stage"] == t["stage"] and x["n"] == t["parent"]), None)
    return ""


def annotate(shot_id: str, stage: str, n: int, **fields) -> dict:
    """新版本补记精修字段；由当前定稿首帧得到的尾帧 / 视频 → 清掉该阶段的过期标记"""
    with P.shot_lock(shot_id):
        data = P.load_takes(shot_id)
        t = _find(data, stage, n)
        t.update({k: v for k, v in fields.items() if v is not None})
        first = (data.get("locked") or {}).get("image")
        same = first_equivalents(data, first) if stage == "last" else {first}
        if stage in ("last", "video") and first is not None and t.get("from_first") in same:
            (data.get("stale") or {}).pop(stage, None)
        P._save_takes(shot_id, data)
        return t


# ------------------------------------------------------------------ 出图

def _parent(shot_id: str, stage: str, parent: int | None) -> dict:
    data = P.load_takes(shot_id)
    n = parent if parent is not None else (data.get("active") or {}).get(stage)
    if n is None:
        raise ComfyError(f"{shot_id} 还没有{'首帧' if stage == 'image' else '尾帧'}，不能在它上面改")
    return _find(data, stage, n)


def _scaled(src: Path, size: tuple[int, int] | None, max_side: int | None = None) -> Path:
    """预缩放到缓存（同一源同一尺寸只做一次）。存 JPEG q95：经隧道上传 gpu3 时 PNG 动辄几 MB（实测 60 s 超时）"""
    import hashlib

    from PIL import Image
    im = Image.open(src).convert("RGB")
    size = size or _fit_size(im.size, max_side)
    digest = hashlib.sha256(src.read_bytes()).hexdigest()[:12]
    out = P.DATA / "cache" / "frame_edit" / f"{src.stem}_{digest}_{size[0]}x{size[1]}.jpg"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        (im if im.size == tuple(size) else im.resize(size, Image.LANCZOS)).save(out, quality=UPLOAD_JPEG_QUALITY)
    return out


def _fit_size(wh, max_side: int) -> tuple[int, int]:
    k = min(1.0, max_side / max(wh))
    return max(32, round(wh[0] * k / 32) * 32), max(32, round(wh[1] * k / 32) * 32)


def _input(client, path: Path, size: tuple[int, int] | None = None, max_side: int | None = None) -> str:
    """父版本 / 参考图 → LoadImage 名：gpu3 同实例输出目录里已有（下载时记过）就 [output] 引用 + 图内缩放，
    否则上传本地缩放好的 JPEG（fredw-92/F1）"""
    if size is None:
        from PIL import Image
        with Image.open(path) as im:
            size = _fit_size(im.size, max_side)
    return input_image(client, path, "ns_final/refs", size=size, prepare=lambda: _scaled(path, size))


def unique_tag(job: dict | None) -> str:
    """版本文件名后缀：任务 id（只留字母数字），没有任务时随机 12 位"""
    import re
    import uuid
    jid = re.sub(r"[^0-9A-Za-z]", "", str((job or {}).get("id") or ""))[:16]
    return jid or uuid.uuid4().hex[:12]


def resolve_ref_path(ref: dict) -> Path:
    """refs 里的 path：相对项目根或 data/ 的图片文件（asset_id 由 API 层先解析成 path）"""
    raw = str(ref.get("path") or "")
    # 只收相对路径：拒绝 \、盘符、以 / 开头（Windows 上 ROOT / "/x.png" 会变成 D:\x.png）、..（F3 审计）
    if (not raw or "\\" in raw or ":" in raw or raw.startswith("/") or ".." in raw.split("/")
            or Path(raw).is_absolute() or Path(raw).drive or Path(raw).root):
        raise ValueError(f"参考图路径不合法：{raw!r}")
    for base in (P.ROOT, P.DATA):
        p = base / raw
        try:
            inside = p.resolve().is_relative_to(base.resolve())
        except OSError:
            inside = False
        if inside and p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            return p
    raise ValueError(f"找不到参考图：{raw}")


def edit_frame(client, script, shot_id: str, stage: str, *, mode: str, parent: int | None = None,
               instruction: str = "", strength: str | None = None, refs: list[dict] | None = None,
               seed: int | None = None, timeout_s: float = 900, size=P.IMAGE_SIZE, job: dict | None = None):
    """instruct / refs：在父版本上编辑，出 1 张，存新版本并设为当前。返回 Take"""
    if mode not in ("instruct", "refs"):
        raise ValueError(mode)
    if not (instruction or "").strip():
        raise ValueError("指令改图 / 换参考需要修改要求（原话）")
    src_take = _parent(shot_id, stage, parent)
    basis = chain_basis(P.load_takes(shot_id), src_take)   # F4：内容来自父版本 → 过期基准、造型、输入哈希都随父版本
    c = P.compile_with_edits(script, shot_id, P.available_loras(client))
    negative = P.image_negative(c, P.QWEN_TURBO)   # FIX P0 ⑥：Turbo cfg 1 → 不记负面词
    # LCA：父版本有后处理（缩回 / 锐化，image_params.postprocess）时用后处理前的原图，免得锐化在改图链上叠加
    post_from = ((src_take.get("params") or {}).get("post") or {}).get("from")
    src = P.DATA / (src_take.get("raw_file") or post_from or src_take["file"])
    if not src.is_file():
        src = P.DATA / src_take["file"]
    names = [_input(client, src, EDIT_REF_SIZE)]
    ref_rec = []
    for r in refs or []:
        path = resolve_ref_path(r)
        names.append(_input(client, path, None, REF_MAX_SIDE))
        ref_rec.append({k: v for k, v in {"asset_id": r.get("asset_id"), "path": r.get("path"),
                                           "role": r.get("role"), "resolved_from": r.get("resolved_from")}.items() if v})
    if mode == "refs" and not ref_rec:
        raise ValueError("换参考再改需要至少一张参考图")
    st = strength or (DEFAULT_STRENGTH if mode == "instruct" else None)
    if st is not None and st not in EDIT_DENOISE:
        raise ValueError(f"strength 只能是 {tuple(EDIT_DENOISE)}")
    init = _input(client, src, tuple(size)) if st else None
    denoise = EDIT_DENOISE[st] if st else 1.0
    with P.shot_lock(shot_id):
        n = P._next_n(shot_id, stage)
    s_ = seed if seed is not None else P.seed_for(f"{shot_id}/{stage}/{mode}", n)
    wf = W.qwen_edit(instruction, negative, names, *size, s_, f"ns_final/{shot_id}_{stage}_{mode}",
                     m=P.QWEN_TURBO, loras=c.image_loras, ref_resolution=0, crop=False,
                     init_image=init, denoise=denoise)
    entry, secs = client.run(wf, timeout_s=timeout_s)
    item = next(f for f in client.output_files(entry) if f["filename"].endswith(".png"))
    rel_dir = RP.shot_take_rel_dir(shot_id)   # W33：带剧名前缀
    # 文件名带任务 id（无任务时随机）：两条 GPU 道同时给同一镜同一阶段出图会拿到同一个 n，不能互相覆盖（F3 审计）
    rel = f"{rel_dir}/{stage}_{n:03d}_{unique_tag(job)}.png"
    client.download(item, P.DATA / rel)
    take = P.Take(n=n, stage=stage, file=rel, seed=s_, prompt=instruction, negative=negative,
                  seconds=round(secs, 1), created_at=P.now(), workflow=f"qwen_edit_{mode}",
                  reference=f"data/{src_take['file']}",
                  references=[f"data/{src_take['file']} ({stage} v{src_take['n']})",
                              *(r["path"] for r in ref_rec if r.get("path"))],
                  parent=src_take["n"], mode=mode, instruction=instruction, strength=st, refs=ref_rec or None,
                  from_first=src_take.get("from_first") if stage == "last" else None,
                  gpu=(job or {}).get("gpu"), job_id=(job or {}).get("id"),
                  source="gen" if stage == "last" else None, remote=remote_info(client, P.DATA / rel),
                  basis_prompt=basis, look_hash=src_take.get("look_hash"),
                  inputs_sha=src_take.get("inputs_sha"), bible_version=src_take.get("bible_version"),
                  deps={k: v for k, v in (src_take.get("deps") or {}).items() if not k.startswith("preset:")} or None)
    # G3（项目方 11-01）：编辑链的新版本也先不设当前，过了契约才由 auto_current.on_contract 设（契约参考沿用父版本的槽位）
    P._add_take(shot_id, take, activate=False)
    if stage == "last":
        annotate(shot_id, stage, take.n)
    try:
        from . import unified_frame as UF
        parent_c = src_take.get("contract") or {}
        annotate(shot_id, stage, take.n, contract={"asset_refs": parent_c.get("asset_refs") or {}})
        UF.postflight_async(shot_id, stage, take.n, take.file, {"cast": (parent_c.get("pf_ctx") or {}).get("cast")},
                            _host_of(client), {"contract": {"asset_refs": parent_c.get("asset_refs") or {}}})
    except Exception:  # noqa: BLE001  契约起不来：版本留着可手动采用，但不会自动设为当前
        pass
    P._clear_error(shot_id)
    return take


def canvas_face_lock(c) -> bool | None:
    """画布首帧：单张出图，不走旧的“身份底板两段式换脸”（qwen_two_stage）。
    RECALL 2026-11-01：原逻辑“带身份 LoRA 才单张、否则两段换脸”依赖 lin_wan_qi_v1；该 LoRA 已按项目方决定撤下并隔离，
    且 K-REN-134 定“不再用旧首帧作图1换脸”。登记了资产的项目（series10_v1）本来就走 unified_frame，不经这里。"""
    return False


def _host_of(client) -> str | None:
    """出图道 → 机器（postflight 原地打分用）：隧道端口 181xx = gpu33、182xx = gpu36"""
    u = str(getattr(client, "base", None) or getattr(client, "url", None) or getattr(client, "server", None) or "")
    return "gpu36" if ":182" in u else ("gpu33" if ":181" in u else None)


def regen(client, script, shot_id: str, stage: str, *, prompt: str | None = None, parent: int | None = None,
          seed: int | None = None, job: dict | None = None):
    """整张重出：1 张、不评分、不看图判定、不否决。尾帧须由定稿首帧做（C3）。
    K-REN-134（总调度 11-01 P0）：剧本属于登记了资产的项目（config/recipes/asset_refs.json，例 series10_v1）时，
    首帧 / 尾帧一律走唯一出图路径 ns_render.unified_frame（frame_v9 + asset_refs 槽位 + 生成契约），不再用旧首帧作图1换脸"""
    from . import unified_frame as UF
    if UF.project_for(script):
        host = _host_of(client)
        if stage == "image":
            take, meta, ujob = UF.render_first(client, script, shot_id, seed=seed, prompt_override=prompt, host=host,
                                               postflight=False)
            out = annotate(shot_id, "image", take["n"], parent=parent, mode="regen", gpu=(job or {}).get("gpu"),
                           job_id=(job or {}).get("id"), basis_prompt=None, path="unified_frame（K-REN-134）", **meta)
            UF.postflight_async(shot_id, "image", take["n"], take["file"], ujob, host, meta)   # 不占出图任务时长（IMGPERF）
            return UF.record_params(shot_id, "image", take["n"])            # 交付尺寸 + 坏图检查后才设当前
        first = locks(shot_id).get("image")
        if first is None:
            raise ComfyError(f"{shot_id} 首帧还没定稿，不能做尾帧")
        ft = _find(P.load_takes(shot_id), "image", first)
        take, meta, ujob = UF.render_last(client, script, shot_id, ft["file"], seed=seed, prompt_override=prompt, host=host,
                                          postflight=False)
        out = annotate(shot_id, "last", take["n"], parent=parent, mode="regen", from_first=first,
                       gpu=(job or {}).get("gpu"), job_id=(job or {}).get("id"), source="gen",
                       path="unified_frame（K-REN-134）", **meta)
        UF.postflight_async(shot_id, "last", take["n"], take["file"], {**ujob, "asset_refs": meta["contract"]["asset_refs"]}, host, meta)
        return UF.record_params(shot_id, "last", take["n"])
    if stage == "image":
        c = P.compile_with_edits(script, shot_id, P.available_loras(client))
        take = P.render_image(client, script, shot_id, seed=seed, candidates=1, rounds=1, score_fn=None,
                              qc_fn=no_qc, grade=CANVAS_GRADE_FIRST, face_lock=canvas_face_lock(c),
                              prompt_override=prompt)
        # F4：一次性整段替换的提示词不和剧本比 → 记下出图时的编译提示词作过期基准
        return annotate(shot_id, "image", take.n, parent=parent, mode="regen", gpu=(job or {}).get("gpu"),
                        job_id=(job or {}).get("id"), basis_prompt=c.image_prompt if prompt else None)
    first = locks(shot_id).get("image")
    if first is None:
        raise ComfyError(f"{shot_id} 首帧还没定稿，不能做尾帧")
    basis = P.compile_with_edits(script, shot_id, P.available_loras(client)).last_prompt if prompt else None
    P.set_active(shot_id, "image", first)      # 尾帧以定稿首帧为图1（frame.png = 定稿版本）
    take = P.render_last_frame(client, script, shot_id, seed=seed, candidates=1, rounds=1, score_fn=None,
                               qc_fn=no_qc, prompt_override=prompt)
    return annotate(shot_id, "last", take.n, parent=parent, mode="regen", from_first=first,
                    gpu=(job or {}).get("gpu"), job_id=(job or {}).get("id"), source="gen", basis_prompt=basis)
