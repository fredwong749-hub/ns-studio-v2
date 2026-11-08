"""K-REN-134 全系统唯一出图路径的 ComfyUI 一侧：ns_agent.frame_job 作业 → 图 → 出图 → 契约。

    build_graph(job, seed, upload, prefix) → (wf, eff, 送进模型的提示词)
        预设 = config/render_quality.json frame_redo_preset（frame_v9：Turbo 8 步、krea2 0.5、无 lin_wan_qi、1.5× 出图 +
        lanczos 缩回 + USM）；提示词已含 S★ 后缀 → style_suffix 置空不重复接；参考 = job["refs"] 按槽位顺序（qwen_edit 多参考，
        ref_resolution 0、不裁）。与 tools/series10_imggen/imgfix_ab.build(sstar, no_suffix) 的 vR / v9 同一张图。
    render_first(client, script, shot_id, ...) / render_last(...)：画布 frame-jobs 重出走这里（ns_render.frame_edit.regen）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import image_params as IP
from . import workflows as W
from . import render_paths as RP   # W33

ROOT = Path(__file__).resolve().parents[3]


def _sha16(s: str) -> str:
    import hashlib
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:16]


def preset_id() -> str:
    try:
        rq = json.loads((ROOT / "config" / "render_quality.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rq = {}
    return rq.get("frame_redo_preset") or "frame_v9"


def build_graph(job: dict, seed: int, upload, prefix: str, size=(768, 1344), overrides: dict | None = None):
    names = [upload(ROOT / r if not Path(r).is_absolute() else Path(r)) for r in job["refs"]]
    # PRODUCER：总调度驳回重出可指定预设（如 frame_v9_heavy）；overrides 只给 scope=ab 实验用（RESEARCH 11-01），生产调用不传
    eff = IP.resolve(job.get("preset") or preset_id(), {"style_suffix": "", **(job.get("tier_overrides") or {}), **(overrides or {})})
    wf = W.qwen_edit(job["prompt"], job.get("negative") or "", names, size[0], size[1], seed, prefix, m=W.QwenModel(), loras=[],   # W-40
                     ref_resolution=0, crop=False)
    wf = IP.apply(wf, eff, set())
    # CARRYOVER 第 11 项（MERGE-IMG-O 11-03）：图先按 Turbo 默认模型建（negative_for 把负面词清空），预设换成 Base（cfg>1）时
    # 负面分支真正参与采样 → 把作业负面词（含未成年人外观硬负面）写回编码器；Turbo 照旧不送
    cfg_ = float(((wf.get("38") or {}).get("inputs") or {}).get("cfg") or 1.0)
    if eff and eff.get("unet") != "turbo" and cfg_ > 1.0:
        for n in wf.values():
            if "TextEncodeQwenImage" in n.get("class_type", "") and "negative_prompt" in n.get("inputs", {}):
                n["inputs"]["negative_prompt"] = job.get("negative") or ""
    if job.get("previz"):
        wf = add_fun_controlnet(wf, job["previz"], upload)
    sent = next((n["inputs"].get("prompt") for n in wf.values() if "TextEncodeQwenImage" in n.get("class_type", "")), job["prompt"])
    return wf, eff, sent


FUN_CN_PATCH = "qwen_image_2.1_fun_controlnet_union_int8_convrot.safetensors"


def add_fun_controlnet(wf: dict, previz: dict, upload) -> dict:
    """L-197 B：previz 深度图 → Qwen-Image 2.1 Fun ControlNet Union（model_patches）。节点链
    LoraLoaderModelOnly → ZImageFunControlnet → QwenImage21Cache（把 Cache 的 model 输入改接到 ControlNet 节点，其余不动）。
    previz = {control: 相对仓库根的深度图路径, strength, start_percent, end_percent}。没有 previz 不调用，图逐字节不变。"""
    ctl = Path(previz["control"])
    name = upload(ROOT / ctl if not ctl.is_absolute() else ctl)
    src = wf["13"]["inputs"]["model"]
    wf = {**wf}
    wf["200"] = {"class_type": "ModelPatchLoader", "inputs": {"name": previz.get("patch") or FUN_CN_PATCH}}
    wf["201"] = {"class_type": "LoadImage", "inputs": {"image": name}}
    wf["202"] = {"class_type": "ZImageFunControlnet",
                 "inputs": {"model": src, "model_patch": ["200", 0], "vae": ["12", 0], "strength": float(previz.get("strength", 0.55)),
                            "image": ["201", 0], "start_percent": float(previz.get("start_percent", 0.0)),
                            "end_percent": float(previz.get("end_percent", 0.7))}}
    wf["13"] = {**wf["13"], "inputs": {**wf["13"]["inputs"], "model": ["202", 0]}}
    return wf


def project_for(script) -> str | None:
    """系统剧本对应的资产项目（config/recipes/asset_refs.json projects 的键出现在剧本来源路径里）"""
    from ns_agent import asset_refs as AR
    keys = list((AR._cfg().get("projects") or {}))
    for s in getattr(script, "sources", None) or []:
        path = str(getattr(s, "path", None) or (s.get("path") if isinstance(s, dict) else "") or "")
        k = next((k for k in keys if k in path), None)
        if k:
            return k
    return None


def _job_for(script, shot_id: str, project: str, prompt_override: str | None):
    from ns_agent import frame_job as FJ
    from . import pipeline as P
    ep, shot = P.find_shot(script, shot_id)
    return FJ.build_first(project, ep, shot, script.globals, prompt_override=prompt_override)


def _contract(job: dict):
    from ns_agent.gen_contract import preflight as PF
    req = {k: job.get(k) for k in ("model", "cast", "prompt", "refs", "asset_refs", "loras", "compiled_by", "route", "route_reasons", "lane",
                                   "continuity_ref")}     # continuity_ref：L-203 尾帧 compose 的首帧连续性参考（preflight I9 认它）
    req.update(kind="image", project=job.get("framing_project"), shot_id=job["shot"])
    if job.get("stage") == "last":
        req["kind"] = "image"
    return PF.check(req)


def _run(client, shot_id: str, stage: str, job: dict, seed: int, timeout_s: float = 900, *,
         rel_dir: str | None = None, overrides: dict | None = None, prefix: str | None = None):
    """rel_dir / overrides / prefix 只给 scope=ab 实验（render_ab）用：图存到 data/ab/<实验>/，版本号不占生产序号"""
    from ns_schema.edits import Take
    from . import pipeline as P
    up = lambda p: client.upload_image(p, subfolder="ns_final/refs")   # noqa: E731
    n = 0 if rel_dir else P._next_n(shot_id, stage)
    wf, eff, sent = build_graph(job, seed, up, prefix or f"ns_final/{shot_id}_{stage}_v9", overrides=overrides)
    entry, secs = client.run(wf, timeout_s=timeout_s)
    client.last_image_wf = wf
    item = next(f for f in client.output_files(entry) if f["filename"].endswith(".png"))
    rel_dir = rel_dir or RP.shot_take_rel_dir(shot_id)   # W33：带剧名前缀
    # 总调度 11-01：文件名防撞——n 只是开工时的估计（takes.json 被改写 / 并发时会重复），文件名再带一段随机串，绝不覆盖已有文件
    import uuid as _uuid
    raw = f"{rel_dir}/{stage}_{n:03d}_{_uuid.uuid4().hex[:8]}_raw.png"
    client.download(item, P.DATA / raw)
    post = IP.post_file(P.DATA / raw, eff)
    final = Path(post["path"]) if post else P.DATA / raw
    rel = final.relative_to(P.DATA).as_posix()
    take = Take(n=n, stage=stage, file=rel, raw_file=raw, seed=seed, prompt=sent, negative="", seconds=round(secs, 1),
                created_at=P.now(), workflow="frame_v9_unified", reference=job["refs"][0] if job["refs"] else None,
                references=list(job["refs"]))
    # PROMPTFIX 11-02（ALIGNMENT 第 2 / 6 步）：compile_trace 随 take 落账（提示词 sha、参考图、种子、种子来源）
    ct = dict(job.get("compile_trace") or {})
    if ct:
        ct.update(seed=seed, seed_policy=job.get("seed_policy") or "caller", sent_prompt_sha=_sha16(sent),
                  final_prompt_sha=_sha16(job.get("prompt") or ""))   # final ≠ compile 的 prompt_sha = 编译后又被改过（owner_edit / vqa_delta）
        take.params = {**(take.params or {}), "compile_trace": ct}
    if job.get("previz"):              # L-197 B：控制图（深度）与参数记进 take，重出判定 / 复盘据此
        take.params = {**(take.params or {}), "previz": {k: job["previz"].get(k) for k in ("control", "sha", "strength", "start_percent", "end_percent", "shot_spec")}}
    if job.get("tier_overrides"):      # L-191：记档（producer.redo_inputs_changed 据此判 8 步旧版 ≠ 35 步）
        take.params = {**(take.params or {}), "tier": {"id": job.get("tier"), "overrides": job["tier_overrides"]}}
    if job.get("prompt_template"):     # 阶段 2.1：提示词模板 v2 记进 take（producer.redo_inputs_changed 据此判“模板变了”）
        take.params = {**(take.params or {}), "prompt_template": job["prompt_template"]}
    # 重出时追加 / 替换进提示词的文本（producer.apply_owner_edit 的项目方修改要求、vqa judge 的一维修正）记进 params.redo_fix_clause：
    # ns_api.unified_stale.strip_redo_fix 比较前按它剥掉，否则重出版本一律被标“编译器升级”（D-FRESH，ep01-S13 / S16）。只记录，不改出图行为
    fixes = []
    if job.get("owner_edit_prompt"):
        fixes.append({"kind": "owner_edit", "id": job.get("owner_edit"), "prompt": job["owner_edit_prompt"]})
    if job.get("vqa_delta"):
        d_ = job["vqa_delta"]
        fixes.append({"kind": "vqa_delta", "replace": [list(x) for x in (d_.get("replace") or [])], "append": d_.get("append") or ""})
    if fixes:
        take.params = {**(take.params or {}), "redo_fix_clause": fixes}
    # 交付尺寸记录（11-01 bug：画布 take 7 被判“坏图：768×1344 与请求的 1152×2016 不符”——图是对的（1.5× 出图 →
    # lanczos 缩回 768×1344 → USM），是 params 只记了放大出图的图尺寸）：后处理记成 params.post，effective 记交付尺寸
    post_rec = ({k: v for k, v in post.items() if k != "path"} | {"file": rel, "from": raw}) if post else None
    return take, eff, post_rec


def _postflight(final_rel: str, job: dict, host: str | None):
    """出图后契约（Q1–Q8）：一次 ssh 到出图机器（gpu33）全部原地算——ArcFace / OCR / mediapipe + CLIP 在 GPU 上
    （tools/gen_contract/remote_score.postflight_full，度量代码与笔记本同一份 metrics.py，5 张图核对差 ≤0.004）。
    机器那边失败 → 退回笔记本 CPU 算（慢，但不冒充通过：拿不到远端分时 Q1/Q3/Q5/Q6 记 untested）"""
    import sys
    sys.path.insert(0, str(ROOT / "tools" / "gen_contract"))
    from ns_agent.gen_contract import postflight as PO
    from . import pipeline as P
    req = {k: job.get(k) for k in ("cast", "asset_refs")}
    img = P.DATA / final_rel
    if host == "local":      # LOCALGPU（11-01）：本机道 laptop:* 出的图 → 本机 CPU 全测（Q1–Q8，与 gpu33 同代码同模型），断网也判全
        try:
            import local_score as LS
            if LS.available():
                d = LS.postflight_local([{"id": "x", "file": str(img), "request": req}])["x"]
                if not d.get("measure_error"):
                    return d
        except Exception:  # noqa: BLE001  本机打分失败 → 照旧试 gpu33，再退回 CPU 部分测
            pass
    try:
        import remote_score as RS
        h = "gpu33"          # CLIP 权重只在 gpu33（/root/work/fredw/reflock）；host=local 本机打分失败时也退到这里
        d = RS.postflight_full(h, [{"id": "x", "file": str(img), "post": None, "request": req}])["x"]
        if not d.get("measure_error"):
            return d
        err = d["measure_error"]
    except Exception as e:  # noqa: BLE001
        err = repr(e)[:200]
    try:
        d = PO.run("image", {"image": str(img), "request": req, "remote": None, "cross": None, "hands": None})
    except Exception as e:  # noqa: BLE001  # arch-ok: 本机退回打分也跑不了（服务器 producer venv 没 torch）= 没量成，不是不过；保留远端真因
        # FIX-SCORE36（11-06）：原来这里抛 ModuleNotFoundError('torch') 把远端真因（full_score 崩）盖掉，日志只见 torch
        return {"ok": None, "items": [], "error": "本机退回打分失败：" + repr(e)[:160], "remote_error": err}
    d.pop("local", None)
    d["remote_error"] = err
    return d


def render_first(client, script, shot_id: str, *, seed: int | None = None, prompt_override: str | None = None,
                 host: str | None = None, postflight: bool = True):
    """画布重出首帧（frame-jobs mode=regen stage=first）：唯一出图路径；preflight 不过 → 抛错不提交"""
    from . import pipeline as P
    from .comfy_client import ComfyError
    P.check_not_gpu3(shot_id)
    project = project_for(script)
    job = _job_for(script, shot_id, project, prompt_override)
    pf = _contract(job)
    if not pf.ok:
        raise ComfyError(f"{shot_id} 生成契约 preflight 不通过，未提交：" + "；".join(pf.reasons()))
    s = seed if seed is not None else P.seed_for(shot_id + "/v9", P._next_n(shot_id, "image"))
    try:
        take, _eff, post_rec = _run(client, shot_id, "image", job, s)
    except FileNotFoundError:                     # 设定图裁切 / 参考板被别的进程重建（换了选定设定图）→ 重新规划一次
        job = _job_for(script, shot_id, project, prompt_override)
        pf = _contract(job)
        if not pf.ok:
            raise ComfyError(f"{shot_id} 生成契约 preflight 不通过，未提交：" + "；".join(pf.reasons()))
        take, _eff, post_rec = _run(client, shot_id, "image", job, s)
    P._add_take(shot_id, take, activate=False)                # 先不设当前：record_params 写好尺寸、过了坏图检查才设
    take_d = take.model_dump()
    meta = {"contract": {"preflight": pf.to_dict(), "asset_refs": job["asset_refs"]}, "v9_post": post_rec,
            "v9_preset": preset_id()}
    if postflight:
        meta["contract"]["postflight"] = _postflight(take.file, job, host)
    return take_d, meta, job


AB_ID = __import__("re").compile(r"^[a-z0-9][a-z0-9_]{1,40}$")


def ab_dir(experiment: str) -> Path:
    """scope=ab 实验产物目录（data/ab/<实验>/）；实验名只许小写字母、数字、下划线"""
    from . import pipeline as P
    if not AB_ID.match(experiment or ""):
        raise ValueError(f"实验名 {experiment!r} 只能用小写字母、数字、下划线（2–41 个字符）")
    return P.DATA / "ab" / experiment


def render_ab(client, script, shot_id: str, *, experiment: str, tier: str, seed: int | None = None,
              prompt_edits: list | None = None, overrides: dict | None = None, preset: str | None = None,
              postflight: bool = False, host: str | None = None, tag: dict | None = None,
              job_patch: dict | None = None) -> dict:
    """scope=ab（RESEARCH 11-01，总调度 fredw-2b 方案 ②）：走唯一出图路径的同一套作业 / 契约 preflight / 图 / 后处理，但
    ① 不写 takes.json（不调 P._add_take / record_params）→ auto_current、rescore_takes、画布都看不到；
    ② 不写契约账本（postflight 结果只进实验清单，不调 _pf_write）；
    ③ 图存 data/ab/<experiment>/<shot>/，每张一行追加到 data/ab/<experiment>/takes.jsonl。
    prompt_edits = [[原文, 替换]…]：在编译好的完整提示词上逐个替换（原文不在提示词里就报错，防止静默无效）；
    overrides = ns_render.image_params DEFAULTS 字段的覆盖（例 {"loras": [{"name": …, "strength": 0.6}]}）。"""
    import uuid as _uuid
    from . import pipeline as P
    from .comfy_client import ComfyError
    out = ab_dir(experiment)
    if not AB_ID.match(tier or ""):
        raise ValueError(f"档名 {tier!r} 只能用小写字母、数字、下划线")
    P.check_not_gpu3(shot_id)
    project = project_for(script)
    if not project:
        raise ComfyError(f"{shot_id} 不在登记了资产的项目里，scope=ab 只支持唯一出图路径")
    job = dict(_job_for(script, shot_id, project, None))
    if preset:
        job["preset"] = preset
    # job_patch（IMGMASTER 11-02）：新编译入口的 prompt / refs / asset_refs 直接替换作业对应字段（只给 scope=ab；
    # 不写账本；字段限 prompt、refs、asset_refs、previz、tier、tier_overrides）
    if job_patch:
        bad = set(job_patch) - {"prompt", "refs", "asset_refs", "previz", "tier", "tier_overrides"}      # L-197 B：previz / tier 供 turbo_previz 小样
        if bad:
            raise ValueError(f"job_patch 只允许 prompt / refs / asset_refs / previz / tier / tier_overrides，收到 {sorted(bad)}")
        job.update(job_patch)
    prompt = job["prompt"]
    for pair in prompt_edits or []:
        old, new = pair[0], pair[1]
        if old not in prompt:
            raise ValueError(f"prompt_edits：原文「{old[:30]}」不在 {shot_id} 的编译提示词里")
        prompt = prompt.replace(old, new)
    job["prompt"] = prompt
    # preflight 照查（I1–I10）；只有 I7 后缀一项按现成的实验口径放行：scope=bakeoff + suffix_variant（preflight 第 217 行，
    # “BAKEOFF A/B 可换后缀，禁用词照查；生产不放”）——改写了提示词的实验才声明
    from ns_agent.gen_contract import preflight as PF
    req = {k: job.get(k) for k in ("model", "cast", "prompt", "refs", "asset_refs", "loras", "compiled_by", "route", "route_reasons", "lane")}
    req.update(kind="image", project=job.get("framing_project"), shot_id=job["shot"])
    if prompt_edits:
        req.update(scope="bakeoff", suffix_variant=True)
    pf = PF.check(req)
    if not pf.ok:
        raise ComfyError(f"{shot_id} 生成契约 preflight 不通过，未提交：" + "；".join(pf.reasons()))
    s = seed if seed is not None else P.seed_for(shot_id + "/v9", 1)
    if tag is not None:
        client.ns_tag = tag
    rel_dir = (out / shot_id).relative_to(P.DATA).as_posix()
    (P.DATA / rel_dir).mkdir(parents=True, exist_ok=True)
    take, eff, post_rec = _run(client, shot_id, "image", job, s, rel_dir=rel_dir, overrides=overrides,
                               prefix=f"ns_ab/{experiment}/{shot_id}_{tier}")
    rec = {"scope": "ab", "experiment": experiment, "tier": tier, "shot": shot_id, "seed": s,
           "file": take.file, "raw_file": take.raw_file, "seconds": take.seconds, "created_at": take.created_at,
           "prompt": take.prompt, "prompt_edits": prompt_edits or [], "overrides": overrides or {},
           "preset": job.get("preset") or preset_id(), "loras": eff.get("loras"), "refs": job["refs"],
           "preflight": pf.to_dict(), "post": post_rec, "id": _uuid.uuid4().hex[:12]}
    if postflight:
        rec["postflight"] = _postflight(take.file, job, host)
    with (out / "takes.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + chr(10))
    return rec


def render_last(client, script, shot_id: str, first_file_data_rel: str, *, seed: int | None = None,
                prompt_override: str | None = None, host: str | None = None, postflight: bool = True):
    from ns_agent import frame_job as FJ
    from . import pipeline as P
    from .comfy_client import ComfyError
    project = project_for(script)
    first = _job_for(script, shot_id, project, None)
    ep, shot = P.find_shot(script, shot_id)
    # L-203：尾帧方式按 run_style last_frame_mode（compose = 落幅单独构图；edit = 旧法），与 producer 同一入口 FJ.build_last
    job = FJ.build_last(first, f"data/{first_file_data_rel}", prompt_override=prompt_override, project=project, ep=ep, shot=shot,
                        g=script.globals)
    s = seed if seed is not None else P.seed_for(shot_id + "/v9last", P._next_n(shot_id, "last"))
    take, _eff, post_rec = _run(client, shot_id, "last", job, s)
    P._add_take(shot_id, take, activate=False)
    meta = {"contract": {"asset_refs": first["asset_refs"]}, "v9_post": post_rec, "v9_preset": preset_id()}
    if postflight:
        pf = _postflight(take.file, {**job, "asset_refs": first["asset_refs"]}, host)
        try:                                   # L-203：首尾帧比对（FL）并入尾帧 postflight；太像 = 不过
            from ns_agent.gen_contract import first_last as FL
            pf = FL.apply(pf, ROOT / f"data/{first_file_data_rel}", P.DATA / take.file)
        except Exception:  # noqa: BLE001  # arch-ok: 画布旧入口比对失败不改其它结论（producer 路径失败关闭）
            pass
        meta["contract"]["postflight"] = pf
    return take.model_dump(), meta, job


import threading as _th

_PF_RUNNING: set = set()
_PF_LOCK = _th.Lock()
_SWEEPER = {"started": False}
PF_MAX_ATTEMPTS = 3


def _pf_done(pf: dict | None) -> bool:
    return bool(pf) and (pf.get("status") in ("done", "error") or ("items" in pf and pf.get("status") is None))


def pf_of(t: dict | None) -> dict:
    """A-03（V2_READINESS，总调度 19:2x）：一版的出图后契约结果只认这一个读法——take.postflight（唯一字段）优先，
    旧数据只有 contract.postflight / contract.ok 时退到它们。以前两个字段各写各读，一边看不到结果就重新打分（重复打分）"""
    if not t:
        return {}
    p_ = t.get("postflight")
    if isinstance(p_, dict) and (p_.get("ok") is not None or p_.get("status") or p_.get("state")):
        return p_
    c = t.get("contract") or {}
    if isinstance(c, dict):
        if isinstance(c.get("postflight"), dict) and c["postflight"]:
            return c["postflight"]
        if c.get("ok") is not None:
            return c
    return {}


def _pf_write(shot_id: str, stage: str, n: int, pf: dict, ctx: dict | None = None, *, path: Path | None = None,
              expect_file: str | None = None) -> None:
    """写回 contract.postflight。path：开算时就钉死的 takes.json 路径（11-01 事故：后台线程跨过测试的临时目录切换，
    把临时数据写进了真实 data/renders/ep01/S1/takes.json）；expect_file：版本文件不一致就不写（不是同一版）"""
    from . import pipeline as P
    path = Path(path) if path else P.shot_dir(shot_id) / "takes.json"
    if not P._read_json(path, None):
        return

    def fn(data):                                # A-04：takes.json 只经 mutate_takes 写（锁 + seq + 备份 + last_op）
        t = next((x for x in data["takes"] if x["stage"] == stage and x["n"] == n), None)
        if t is None or (expect_file and t.get("file") != expect_file):
            return data
        c = dict(t.get("contract") or {})
        c["postflight"] = pf                     # 兼容旧读者（镜像）
        if ctx is not None:
            c["pf_ctx"] = ctx
        t["contract"] = c
        t["postflight"] = pf                     # A-03：唯一字段（pf_of 先读它）
        return data
    P.mutate_takes(shot_id, fn, op="postflight_write", by="unified_frame._pf_write", path=path)


def mark_pending(shot_id: str, stage: str, n: int, ctx: dict) -> None:
    """开算前先在版本上落“待算”标记（持久化）：进程中途死掉，重启后的扫描会接着算（总调度 11-01）"""
    from . import pipeline as P
    _pf_write(shot_id, stage, n, {"status": "pending", "ok": None, "queued_at": P.now(), "attempts": 0}, ctx)


def _ctx_for(shot_id: str, t: dict) -> dict:
    c = t.get("contract") or {}
    ctx = dict(c.get("pf_ctx") or {})
    if not ctx.get("cast"):
        try:
            from ns_agent import frame_job as FJ
            from . import pipeline as P
            _ep, shot = P.find_shot(P.load_script(), shot_id)
            ctx["cast"] = FJ.cast_of(shot)
        except Exception:  # noqa: BLE001
            ctx["cast"] = []
    return ctx


def run_postflight(shot_id: str, stage: str, n: int) -> dict | None:
    """幂等：已有结果（status done / error，或旧版本已有 items）不重算；同一版本同时只算一次。失败记 attempts，满 3 次标 error"""
    from . import pipeline as P
    key = (shot_id, stage, n)
    with _PF_LOCK:
        if key in _PF_RUNNING:
            return None
        _PF_RUNNING.add(key)
    try:
        tpath = P.shot_dir(shot_id) / "takes.json"            # 开算时钉死路径，写回只写这一个文件
        t = next((x for x in (P._read_json(tpath, None) or {"takes": []})["takes"] if x["stage"] == stage and x["n"] == n), None)
        if t is None:
            return None
        cur = pf_of(t)                           # A-03：两个字段任一已有结果都算算过，不重复打分
        if _pf_done(cur):
            return cur
        ctx = _ctx_for(shot_id, t)
        job = {"cast": ctx.get("cast"), "asset_refs": (t.get("contract") or {}).get("asset_refs") or {}}
        try:
            pf = _postflight(t["file"], job, ctx.get("host"))
            pf.update(status="done", finished_at=P.now())
        except Exception as e:  # noqa: BLE001
            att = int(cur.get("attempts") or 0) + 1
            pf = {"status": "error" if att >= PF_MAX_ATTEMPTS else "pending", "ok": False if att >= PF_MAX_ATTEMPTS else None,
                  "attempts": att, "last_error": repr(e)[:300], "at": P.now()}
        _pf_write(shot_id, stage, n, pf, ctx, path=tpath, expect_file=t.get("file"))
        if _pf_done(pf):          # G3：契约有结果 → 唯一的“设为当前”规则（过契约且非坏图才设；项目方定稿不挤掉）
            from . import auto_current as AC
            AC.on_contract(shot_id, stage, n)
        return pf
    finally:
        with _PF_LOCK:
            _PF_RUNNING.discard(key)


def postflight_async(shot_id: str, stage: str, n: int, final_rel: str, job: dict, host: str | None, meta: dict):
    """出图后契约放到后台线程（CPU 上 CLIP 滑窗 + 原地打分要几分钟，不占出图任务）。先持久化 status=pending，
    算完写 status=done；进程中途死掉 → sweep_once（API 启动时 + 每 10 分钟）接着算。ok=None 不算通过"""
    ctx = {"cast": job.get("cast"), "host": host}
    mark_pending(shot_id, stage, n, ctx)
    c0 = dict(meta.get("contract") or {})
    c0["postflight"] = {"status": "pending", "ok": None}
    meta["contract"] = c0
    _spawn(run_postflight, (shot_id, stage, n), f"postflight-{shot_id}-{stage}-{n}")


def _spawn(fn, args: tuple, name: str):
    """后台跑出图后契约；测试里 tests/conftest.py 把它换成同步执行（线程不许活过用例，ITERAUDIT K-OPS-Q03）"""
    _th.Thread(target=fn, args=args, daemon=True, name=name).start()


def pending_takes() -> list[tuple[str, str, int]]:
    """唯一出图路径（frame_v9_unified）里 postflight 还没结果（缺失 / pending）的版本"""
    import glob
    from . import pipeline as P
    out = []
    for f in glob.glob(str(P.RENDERS / "**" / "takes.json"), recursive=True):
        sid = "-".join(Path(f).parent.parts[-2:])
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for t in d.get("takes") or []:
            c = t.get("contract") or {}
            if (t.get("workflow") == "frame_v9_unified" or "pf_ctx" in c) and not _pf_done(c.get("postflight")):   # G3：编辑链也续算
                out.append((sid, t["stage"], t["n"]))
    return out


def sweep_once() -> list[tuple[str, str, int]]:
    done = []
    for key in pending_takes():
        if run_postflight(*key) is not None:
            done.append(key)
    return done


def start_sweeper(interval_s: float = 600, first_delay_s: float = 20) -> bool:
    """API 启动时调一次：后台线程先等 first_delay_s 再扫一遍，之后每 interval_s 扫一遍（单例）"""
    if _SWEEPER["started"]:
        return False
    _SWEEPER["started"] = True

    def loop():
        time.sleep(first_delay_s)
        while True:
            try:
                sweep_once()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(interval_s)
    _th.Thread(target=loop, daemon=True, name="postflight-sweeper").start()
    return True


def record_params(shot_id: str, stage: str, n: int) -> dict:
    """出图任务收尾（frame_jobs 在 IP.finish 之后调）：把 frame_v9 的后处理与交付尺寸写进 params（image_check 按它比尺寸），
    过坏图检查才设为当前；不过 → 不设当前、记 bad（绝不把坏图设为当前）。返回更新后的 take"""
    from . import image_check as IC
    from . import pipeline as P
    with P.shot_lock(shot_id):
        data = P.load_takes(shot_id)
        t = next(x for x in data["takes"] if x["stage"] == stage and x["n"] == n)
        post = t.get("v9_post")
        params = dict(t.get("params") or {})
        if post:
            params["post"] = post
            eff = dict(params.get("effective") or {})
            eff.update(width=post["size"][0], height=post["size"][1], rendered_size=post.get("rendered_size"),
                       render_scale=round(post["rendered_size"][0] / post["size"][0], 3) if post.get("rendered_size") else 1.0)
            params["effective"] = eff
        params["preset"] = t.get("v9_preset") or params.get("preset")
        t["params"] = params
        why = IC.check_take(t, P.DATA)
        t["bad"] = why
        P._save_takes(shot_id, data)
    # G3（项目方 11-01）：过坏图检查还不够，要等契约（postflight）有结果再由 auto_current.on_contract 决定是否设为当前
    return next(x for x in P.load_takes(shot_id)["takes"] if x["stage"] == stage and x["n"] == n)
