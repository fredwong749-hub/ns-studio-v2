"""
定稿档（fredw-92/Q1）：首帧 / 尾帧的预览版（Image 2.1 Turbo 单遍）定稿后，用 gpu3 生产渲染器 weita_render.py 的
同一组值再出一版“定稿版”，挂在被定稿的预览版下面（parent），同种子、以预览版为底图 + 图1，完成后接替它成为定稿。

所有数值只在 config/render_quality.json（每个值带 docs/research_adoption.md 行号 + gpu3 出处）：
Base qwen_image_2.1_int8_convrot、35 步 cfg 3.0（D1）；两段式 928×1632 → lanczos 1152×2016、denoise 0.30（D2）；
texture-fix VAE + krea2_film_texture 0.5、禁 glamour_realism（D3）；身份 LoRA lin_wan_qi_v1 0.8、LoRA 合计上限 1.6（D4）；
Reinhard LAB → 色调参考 0.6、暗场减半（B5）；不加颗粒（D7）。

不做内容检测、不评分（同画布出图）；提示词原样：剧本编译提示词（预览版根上用过一次性整段替换时，用那一版的原样提示词），
前面只加一句系统用途说明（图1 = 被定稿的预览版）。
"""
from __future__ import annotations

import json
from pathlib import Path

from . import pipeline as P
from . import render_paths as RP   # W33
from . import workflows as W
from .comfy_client import ComfyError, remote_info

CONFIG = P.ROOT / "config" / "render_quality.json"
TIERS = ("preview", "final")
FINAL_MODE = "final"
FINAL_WORKFLOW = "qwen_final_base_2pass"
FE_EDIT_MODES = ("instruct", "refs")      # 同 frame_edit.EDIT_MODES
# 系统用途说明（与 pipeline.COMPOSE_ROLE 首句同一写法），不含剧情内容
FINAL_ROLE = "图1是本镜头已定稿的预览版。保持图1的构图、机位、景别、人物姿势与朝向、表情、服装、背景和光线不变。"


def quality(path: Path | None = None) -> dict:
    return json.loads(Path(path or CONFIG).read_text(encoding="utf-8"))


def final_cfg(path: Path | None = None) -> dict:
    return quality(path)["tiers"]["final"]


def auto_final_on_lock(path: Path | None = None, run: str | None = None) -> bool:
    """定稿后是否自动排定稿档。L-147（项目方 11-03 05:0x）：按剧覆盖——render_quality.json auto_final_on_lock_runs.<剧>
    优先于全局 auto_final_on_lock（V2 = true，V1 仍 false）。run 不给 = 当前剧（NS_RUN / runs.current_run）"""
    try:
        q = quality(path)
    except (OSError, ValueError):
        return False
    per = q.get("auto_final_on_lock_runs") or {}
    r = run or _current_run()
    if r and r in per:
        return bool(per[r])
    return bool(q.get("auto_final_on_lock"))


ACCEPT_FN = None   # 测试注入：(file_rel, job, host, shot, run) → postflight


def final_accept(script, shot_id: str, stage: str, file_rel: str) -> dict | None:
    """定稿档版本过与预览版同一份共用验收（ns_agent.gen_contract.accept：postflight + Q9 + 合并看图 + 三态）。
    不是资产包剧（V1）→ None（口径不变，按原逻辑接替定稿）。验收出错 → ok=False（失败关闭：不接替定稿）"""
    run = _current_run()
    try:
        from ns_agent.gen_contract import accept as ACC
        if ACCEPT_FN is None and not ACC.enabled(run):
            return None
        from ns_agent import frame_job as FJ
        ep, shot = P.find_shot(script, shot_id)
        job = FJ.build_first(run, ep, shot, script.globals)
        fn = ACCEPT_FN or (lambda f, j, h, sh, r: ACC.accept(f, j, h, sh, r))
        pf = fn(file_rel, job, None, shot, run)
        if pf.get("accept_retry"):
            pf["ok"] = False
        return pf
    except Exception as e:  # noqa: BLE001  # arch-ok: 验收判不了 = 不接替定稿（失败关闭），原因写进版本
        return {"ok": False, "error": f"共用验收出错：{type(e).__name__}: {e}"[:300], "items": []}


def _current_run() -> str | None:
    import os
    if os.environ.get("NS_RUN"):
        return os.environ["NS_RUN"]
    try:
        import importlib.util as U
        sp = U.spec_from_file_location("runs_ft", P.ROOT / "tools" / "autopilot" / "runs.py")
        m = U.module_from_spec(sp)
        sp.loader.exec_module(m)
        return m.current_run(P.ROOT)
    except Exception:  # noqa: BLE001 — 读不到当前剧 = 按全局开关
        return None


def final_model(cfg: dict) -> W.QwenModel:
    return W.QwenModel(unet=cfg["unet"], clip=cfg["clip"], vae=cfg["vae"], turbo=False, steps=int(cfg["steps"]),
                       cfg=float(cfg["cfg"]), sampler=cfg.get("sampler", "euler"))


def pass2_steps(cfg: dict) -> int:
    """WR realism_graph：max(8, int(steps × 0.4))；画布出图参数（fredw-92/PX）给了 pass2_steps 就用它"""
    if cfg.get("pass2_steps"):
        return int(cfg["pass2_steps"])
    return max(int(cfg["pass2_min_steps"]), int(int(cfg["steps"]) * float(cfg["pass2_steps_ratio"])))


def cap_loras(loras, cap: float) -> list[tuple[str, float]]:
    """同 WR cap_loras：合计超过上限时按比例缩小，保留比例"""
    loras = list(loras)
    tot = sum(st for _, st in loras)
    if not loras or tot <= cap:
        return loras
    k = cap / tot
    return [(f, round(st * k, 3)) for f, st in loras]


def final_loras(shot_loras, available: set[str], cfg: dict) -> list[tuple[str, float]]:
    """定稿档 LoRA：去掉禁用的；身份 LoRA 用配置强度；写实以外的合计 ≤ lora_cap；写实 LoRA（ComfyUI 有才加）放最前。
    顺序与 WR 相同：gen_image_route 先 cap，realism_graph 再前置 krea2。"""
    banned = set(cfg.get("banned_loras") or [])
    realism = [(f, float(st)) for f, st in cfg.get("realism_loras") or [] if f in available and f not in banned]
    rnames = {f for f, _ in realism}
    ident = cfg.get("identity_loras") or {}
    rest, seen = [], set()
    for f, st in shot_loras or []:
        if f in banned or f in rnames or f in seen:
            continue
        seen.add(f)
        rest.append((f, float(ident.get(f, st))))
    return realism + cap_loras(rest, float(cfg["lora_cap"]))


def _root(data: dict, take: dict) -> dict:
    """沿修改链往上，找到最初出图的那一版（不是指令改图 / 换参考 / 定稿档）"""
    seen, t = set(), take
    while t is not None and (t["stage"], t["n"]) not in seen:
        seen.add((t["stage"], t["n"]))
        if t.get("mode") not in (*FE_EDIT_MODES, FINAL_MODE) or t.get("parent") is None:
            return t
        t = next((x for x in data["takes"] if x["stage"] == t["stage"] and x["n"] == t["parent"]), None)
    return take



def source_prompt(data: dict, take: dict, compiled: str) -> tuple[str, str]:
    """(提示词, 来源)：根版本用过一次性整段替换（basis_prompt 有值且不等于其 prompt）→ 那一版的原样提示词；否则剧本编译提示词"""
    root = _root(data, take)
    if root.get("mode") == "regen" and root.get("basis_prompt") and root.get("prompt") \
            and root["prompt"] != root["basis_prompt"]:
        return root["prompt"], "override"
    return compiled, "script"


def pick_parent(shot_id: str, stage: str, parent: int | None = None) -> dict:
    """定稿档的父版本：给了就用；否则定稿版；没有定稿用当前版"""
    data = P.load_takes(shot_id)
    n = parent if parent is not None else ((data.get("locked") or {}).get(stage)
                                           if (data.get("locked") or {}).get(stage) is not None
                                           else (data.get("active") or {}).get(stage))
    if n is None:
        raise ComfyError(f"{shot_id} 还没有{'首帧' if stage == 'image' else '尾帧'}，不能出定稿版")
    t = next((t for t in data["takes"] if t["stage"] == stage and t["n"] == n), None)
    if t is None:
        raise KeyError(f"没有 {stage} v{n}")
    return t


def render_final(client, script, shot_id: str, stage: str, *, parent: int | None = None, seed: int | None = None,
                 job: dict | None = None, cfg: dict | None = None, lock: bool | None = None,
                 spec_words: bool = True):
    """出定稿版：1 张，挂在父版本下（mode="final"），存原图 + 调色图。lock=None：父版本是定稿版时接替定稿。返回 take dict。
    cfg：画布出图参数覆盖后的定稿档配置（fredw-92/PX，image_params.final_cfg_for）；spec_words=False 去掉系统规格词"""
    from . import frame_edit as FE
    from . import grade as G
    if stage not in ("image", "last"):
        raise ValueError("定稿档只用于首帧 / 尾帧")
    cfg = cfg or final_cfg()
    src_take = pick_parent(shot_id, stage, parent)
    if src_take.get("mode") == FINAL_MODE:
        raise ValueError(f"{stage} v{src_take['n']} 已经是定稿档版本")
    data = P.load_takes(shot_id)
    avail = P.available_loras(client)
    c = P.compile_with_edits(script, shot_id, avail)
    compiled = c.image_prompt if stage == "image" else c.last_prompt
    body, prompt_src = source_prompt(data, src_take, compiled or "")
    if not spec_words:
        from . import image_params as IP
        body = IP.strip_spec(body, IP.spec_terms(script, c, stage))
    prompt = FINAL_ROLE + body
    negative = P.image_negative(c, final_model(cfg))   # FIX P0 ⑥：cfg ≤ 1 不送 / 不记负面词
    loras = final_loras(c.image_loras, avail, cfg)
    src = P.DATA / (src_take.get("raw_file") or src_take["file"])
    ref = FE._input(client, src, FE.EDIT_REF_SIZE)
    init = FE._input(client, src, tuple(cfg["pass1_size"]))
    s_ = int(seed if seed is not None else src_take.get("seed") or P.seed_for(f"{shot_id}/{stage}/final", 0))
    with P.shot_lock(shot_id):
        n = P._next_n(shot_id, stage)
    wf = W.qwen_final(prompt, negative, [ref], init, s_, f"ns_final/{shot_id}_{stage}_final", final_model(cfg), loras,
                      pass1_size=tuple(cfg["pass1_size"]), pass2_size=tuple(cfg["pass2_size"]),
                      init_denoise=float(cfg["init_denoise"]), pass2_denoise=float(cfg["pass2_denoise"]),
                      pass2_steps=pass2_steps(cfg), pass2_seed_offset=int(cfg.get("pass2_seed_offset", 1)))
    entry, secs = client.run(wf, timeout_s=float(cfg.get("timeout_s", 1800)))
    item = next(f for f in client.output_files(entry) if f["filename"].endswith(".png"))
    rel_dir = RP.shot_take_rel_dir(shot_id)   # W33：带剧名前缀
    tag = FE.unique_tag(job)
    raw, rel = f"{rel_dir}/{stage}_{n:03d}_{tag}_final_raw.png", f"{rel_dir}/{stage}_{n:03d}_{tag}_final.png"
    client.download(item, P.DATA / raw)
    gcfg = cfg.get("grade") or {}
    gr = None
    if gcfg:
        ref_img = P.ROOT / gcfg["reference"] if gcfg.get("reference") else None
        gr = G.grade_file(P.DATA / raw, P.DATA / rel, reference=ref_img, strength=float(gcfg.get("strength", 0.6)),
                          dark=bool(getattr(c, "dark", False)))
    else:
        rel = raw
    params = {"tier": "final", "parent_tier": (src_take.get("params") or {}).get("tier", "preview"),
              "unet": cfg["unet"], "vae": cfg["vae"], "steps": int(cfg["steps"]), "cfg": float(cfg["cfg"]),
              "pass1_size": list(cfg["pass1_size"]), "pass2_size": list(cfg["pass2_size"]),
              "init_denoise": float(cfg["init_denoise"]), "pass2_denoise": float(cfg["pass2_denoise"]),
              "pass2_steps": pass2_steps(cfg), "loras": [list(x) for x in loras], "lora_cap": float(cfg["lora_cap"]),
              "prompt_source": prompt_src, "config": "config/render_quality.json"}
    take = P.Take(n=n, stage=stage, file=rel, raw_file=raw if gr else None, seed=s_, prompt=prompt, negative=negative,
                  seconds=round(secs, 1), created_at=P.now(), workflow=FINAL_WORKFLOW, grade=gr,
                  reference=f"data/{src_take['file']}",
                  references=[f"data/{src_take['file']} ({stage} v{src_take['n']})"],
                  parent=src_take["n"], mode=FINAL_MODE, params=params,
                  from_first=src_take.get("from_first") if stage == "last" else None,
                  gpu=(job or {}).get("gpu"), job_id=(job or {}).get("id"),
                  source="gen" if stage == "last" else None, remote=remote_info(client, P.DATA / raw),
                  basis_prompt=FE.chain_basis(data, src_take), look_hash=src_take.get("look_hash"),
                  inputs_sha=src_take.get("inputs_sha"), bible_version=src_take.get("bible_version"),
                  deps={k: v for k, v in (src_take.get("deps") or {}).items() if not k.startswith("preset:")} or None,
                  scene_plate=src_take.get("scene_plate") if stage == "image" else None)
    P._add_take(shot_id, take, activate=False)
    pf = final_accept(script, shot_id, stage, rel)        # PRODMERGE-O（L-147）：定稿档挂在共用验收之后
    if pf is not None:
        FE.annotate(shot_id, stage, take.n, postflight=pf)
        if not pf.get("ok"):                              # 没过（或 retry 没拿到结论）：只留版本，不接替定稿、不设为当前
            P._clear_error(shot_id)
            return next(t for t in P.load_takes(shot_id)["takes"] if t["stage"] == stage and t["n"] == take.n)
    was_locked = (P.load_takes(shot_id).get("locked") or {}).get(stage) == src_take["n"]
    if lock if lock is not None else was_locked:
        FE.lock(shot_id, stage, take.n)
    else:
        P.set_active(shot_id, stage, take.n)
    if stage == "last":
        FE.annotate(shot_id, stage, take.n)
    P._clear_error(shot_id)
    return next(t for t in P.load_takes(shot_id)["takes"] if t["stage"] == stage and t["n"] == take.n)
