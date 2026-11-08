"""ComfyUI API 图（确定性；节点 id 固定）。

- single：MiniMaxH3ImageToVideo（I2VA / FL2VA），节点结构同 ns_render.workflows.h3_fl2va（现生产图）。
- ref2va：MiniMaxH3ReferenceToVideo（项目方工作流 Realistic AI Video MiniMax H3.json）+ 可选 AddGuide frame 0。
- PDD-Acc（sampling.pdd.enabled，VIDEO10 2026-10-31）：UNET（+LoRA）→ MiniMaxH3PDDAccApply → SigmaShift；σ 表取 PDD 节点
  输出 1（8NFE），两段式在 two_stage.split_step（=4，σ .923）切。FL2VA 用 FL2VA-Acc，Ref2VA 用 Ref2VA-Acc。
- ref2va + two_stage：同 tools/lab_ep02/ref2va_run.py（LAB X1–X6 实测）：第一段 pass1 分辨率条件 → 3D 放大 → 第二段同一条件。
- two_stage：结构照 ganloss i2v latent upscale 工作流（data/ab/owner_workflows/…/minimax_h3_i2v_Latent Upscaler2-9.json）：
  低分辨率条件 + 高 σ 段 → LTXVSeparateAVLatent → MinimaxH3LatentUpscaler3D(target dimensions) →
  LTXVConcatAVLatent（音频沿用第一段）→ 目标分辨率条件 + 低 σ 段；第一段取 denoised_output（输出 1）。

图片输入写成占位符 "@first_frame" / "@last_frame" / "@ref:<角色名>"，提交时由提交器换成上传后的文件名。
"""
from __future__ import annotations

PLACEHOLDER_FIRST = "@first_frame"
PLACEHOLDER_LAST = "@last_frame"


def ref_placeholder(name: str) -> str:
    return f"@ref:{name}"


def _loaders(wf: dict, rc: dict) -> list:
    m = rc["model"]
    wf["127"] = {"class_type": "UNETLoader", "inputs": {"unet_name": m["unet"], "weight_dtype": "default"}}
    wf["128"] = {"class_type": "CLIPLoader", "inputs": {"clip_name": m["clip"], "type": m.get("clip_type", "minimax"),
                                                       "device": "default"}}
    wf["119"] = {"class_type": "VAELoader", "inputs": {"vae_name": m["video_vae"]}}
    wf["120"] = {"class_type": "VAELoader", "inputs": {"vae_name": m["audio_vae"]}}
    model = ["127", 0]
    for i, (lid, lo) in enumerate(sorted(enabled_loras(rc).items())):
        nid = str(140 + i)
        wf[nid] = {"class_type": "LoraLoaderModelOnly",
                   "inputs": {"model": model, "lora_name": lo["file"], "strength_model": float(lo["strength"])}}
        model = [nid, 0]
    s = rc["sampling"]
    pdd = pdd_conf(rc)
    if pdd:
        wf["152"] = {"class_type": "MiniMaxH3PDDAccApply",
                     "inputs": {"model": model, "pdd_file": pdd["file"], "nfe": str(pdd.get("nfe", "8")),
                                "lora_strength": float(pdd.get("lora_strength", 1.0)),
                                "head_strength": float(pdd.get("head_strength", 1.0)),
                                "on_off_grid": pdd.get("on_off_grid", "error")}}
        model = ["152", 0]
    if s.get("shift_node") and not s.get("sigmas"):
        wf["150"] = {"class_type": "MiniMaxH3SigmaShift",
                     "inputs": {"model": model, "shift_video": float(s["shift"][0]), "shift_audio": float(s["shift"][1])}}
        model = ["150", 0]
    if s.get("attention") and s["attention"] != "pytorch":
        wf["151"] = {"class_type": "ModelAttentionBackend", "inputs": {"model": model, "attention": s["attention"]}}
        model = ["151", 0]
    return model


def pdd_conf(rc: dict) -> dict | None:
    p = (rc.get("sampling") or {}).get("pdd") or {}
    return p if p.get("enabled") else None


def enabled_loras(rc: dict) -> dict:
    return {k: v for k, v in (rc.get("loras") or {}).items() if v.get("enabled", True)}


def _keyframe(wf: dict, load_id: str, scale_id: str, image: str, w: int, h: int, crop: bool) -> list:
    wf[load_id] = {"class_type": "LoadImage", "inputs": {"image": image}}
    if not crop:
        return [load_id, 0]
    wf[scale_id] = {"class_type": "ImageScale", "inputs": {"image": [load_id, 0], "upscale_method": "lanczos",
                                                          "width": w, "height": h, "crop": "center"}}
    return [scale_id, 0]


def _sampler_nodes(wf: dict, rc: dict, model: list, seed: int) -> list:
    """→ σ 表的连线（PDD 时取 PDD 节点输出 1，否则 124）"""
    s = rc["sampling"]
    wf["129"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": int(seed)}}
    wf["123"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": s["sampler"]}}
    if pdd_conf(rc):
        return ["152", 1]
    if s.get("sigmas"):
        wf["124"] = {"class_type": "ManualSigmas", "inputs": {"sigmas": s["sigmas"]}}
    else:
        wf["124"] = {"class_type": "BasicScheduler",
                     "inputs": {"model": model, "scheduler": s["scheduler"], "steps": int(s["steps"]), "denoise": 1.0}}
    return ["124", 0]


def _outputs(wf: dict, samples: list, prefix: str, fps: int) -> None:
    wf["122"] = {"class_type": "VAEDecode", "inputs": {"samples": samples, "vae": ["119", 0]}}
    wf["121"] = {"class_type": "VAEDecodeAudio", "inputs": {"samples": samples, "vae": ["120", 0]}}
    wf["130"] = {"class_type": "CreateVideo", "inputs": {"images": ["122", 0], "audio": ["121", 0], "fps": fps}}
    wf["92"] = {"class_type": "SaveVideo", "inputs": {"video": ["130", 0], "filename_prefix": prefix,
                                                     "format": "mp4", "codec": "auto"}}


def single(rc: dict, prompt: str, w: int, h: int, frames: int, seed: int, prefix: str, use_last: bool) -> dict:
    wf: dict = {}
    model = _loaders(wf, rc)
    crop = rc["resolution"].get("keyframe_crop") == "center_lanczos"
    i2v = {"clip": ["128", 0], "prompt": prompt, "width": w, "height": h, "length": frames, "vae": ["119", 0],
           "first_frame": _keyframe(wf, "200", "202", PLACEHOLDER_FIRST, w, h, crop)}
    if use_last:
        i2v["last_frame"] = _keyframe(wf, "201", "203", PLACEHOLDER_LAST, w, h, crop)
    wf["131"] = {"class_type": "MiniMaxH3ImageToVideo", "inputs": i2v}
    wf["126"] = {"class_type": "BasicGuider", "inputs": {"model": model, "conditioning": ["131", 0]}}
    sig = _sampler_nodes(wf, rc, model, seed)
    wf["125"] = {"class_type": "SamplerCustomAdvanced",
                 "inputs": {"noise": ["129", 0], "guider": ["126", 0], "sampler": ["123", 0], "sigmas": sig,
                            "latent_image": ["131", 1]}}
    _outputs(wf, ["125", 0], prefix, rc["frames"]["fps"])
    return wf


def two_stage(rc: dict, prompt: str, w: int, h: int, frames: int, seed: int, prefix: str, use_last: bool) -> dict:
    ts = rc["two_stage"]
    w1, h1 = (int(x) for x in ts["pass1"])
    wf: dict = {}
    model = _loaders(wf, rc)
    crop = rc["resolution"].get("keyframe_crop") == "center_lanczos"
    lo = {"clip": ["128", 0], "prompt": prompt, "width": w1, "height": h1, "length": frames, "vae": ["119", 0],
          "first_frame": _keyframe(wf, "200", "202", PLACEHOLDER_FIRST, w1, h1, crop)}
    hi = {"clip": ["128", 0], "prompt": prompt, "width": w, "height": h, "length": frames, "vae": ["119", 0],
          "first_frame": _keyframe(wf, "204", "205", PLACEHOLDER_FIRST, w, h, crop)}
    if use_last:
        lo["last_frame"] = _keyframe(wf, "201", "203", PLACEHOLDER_LAST, w1, h1, crop)
        hi["last_frame"] = _keyframe(wf, "206", "207", PLACEHOLDER_LAST, w, h, crop)
    wf["131"] = {"class_type": "MiniMaxH3ImageToVideo", "inputs": lo}
    wf["132"] = {"class_type": "MiniMaxH3ImageToVideo", "inputs": hi}
    wf["126"] = {"class_type": "BasicGuider", "inputs": {"model": model, "conditioning": ["131", 0]}}
    wf["133"] = {"class_type": "BasicGuider", "inputs": {"model": model, "conditioning": ["132", 0]}}
    sig = _sampler_nodes(wf, rc, model, seed)
    _upscale_tail(wf, ts, sig, ["131", 1], ["133", 0], w, h)
    _outputs(wf, ["164", 0], prefix, rc["frames"]["fps"])
    return wf


def _upscale_tail(wf: dict, ts: dict, sig: list, latent1: list, guider2: list, w: int, h: int) -> None:
    """两段式公共尾巴：SplitSigmas → 第一段（guider 126）→ 拆音视频 → 3D 放大 → 合 → 第二段（guider2）→ 164"""
    wf["160"] = {"class_type": "SplitSigmas", "inputs": {"sigmas": sig, "step": int(ts["split_step"])}}
    wf["125"] = {"class_type": "SamplerCustomAdvanced",
                 "inputs": {"noise": ["129", 0], "guider": ["126", 0], "sampler": ["123", 0], "sigmas": ["160", 0],
                            "latent_image": latent1}}
    wf["161"] = {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["125", 1]}}
    # 输入名以实跑过的 planb_run.py / ref2va_run.py 为准（enable_temporal_chunking）
    wf["162"] = {"class_type": "MinimaxH3LatentUpscaler3D",
                 "inputs": {"latent": ["161", 0], "model_name": ts["upscaler_model"], "mode": "target dimensions",
                            "mode.width": w, "mode.height": h, "align": 32, "device": "cuda",
                            "precision": ts.get("upscaler_precision", "fp16"),
                            "enable_temporal_chunking": bool(ts.get("temporal_chunking", True)),
                            "force_unload": bool(ts.get("force_unload", True))}}
    wf["163"] = {"class_type": "LTXVConcatAVLatent", "inputs": {"video_latent": ["162", 0], "audio_latent": ["161", 1]}}
    wf["164"] = {"class_type": "SamplerCustomAdvanced",
                 "inputs": {"noise": ["129", 0], "guider": guider2, "sampler": ["123", 0], "sigmas": ["160", 1],
                            "latent_image": ["163", 0]}}


def ref2va(rc: dict, prompt: str, w: int, h: int, frames: int, seed: int, prefix: str,
           ref_slots: list[dict]) -> dict:
    """ref_slots 顺序 = ref_image_N（<Picture N> = N+1）。首帧另用 AddGuide frame_idx 0 钉住（Comfy multiframe）"""
    r = rc["ref2va"]
    ts = rc.get("two_stage") or {}
    two = bool(ts.get("enabled"))
    cw, ch = (int(x) for x in ts["pass1"]) if two else (w, h)
    wf: dict = {}
    model = _loaders(wf, rc)
    inputs = {"clip": ["128", 0], "vae": ["119", 0], "audio_vae": ["120", 0], "prompt": prompt, "width": cw,
              "height": ch, "length": frames, "ref_image_size": r.get("ref_image_size", "match")}
    for i, s in enumerate(ref_slots[: int(r.get("max_refs", 9))]):
        lid = str(210 + i)
        img = (PLACEHOLDER_FIRST if s["role"] == "first_frame" else
               f"@look:{s['character']}" if s["role"] == "outfit" else ref_placeholder(s["character"]))
        wf[lid] = {"class_type": "LoadImage", "inputs": {"image": img}}
        inputs[f"ref_images.ref_image_{i}"] = [lid, 0]
    wf["131"] = {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": inputs}
    cond = ["131", 0]
    if r.get("add_guide_first_frame"):
        crop = rc["resolution"].get("keyframe_crop") == "center_lanczos"
        img = _keyframe(wf, "200", "202", PLACEHOLDER_FIRST, cw, ch, crop)
        wf["170"] = {"class_type": "MiniMaxH3AddGuide",
                     "inputs": {"positive": cond, "latent": ["131", 1], "vae": ["119", 0], "image": img, "frame_idx": 0}}
        cond = ["170", 0]
    wf["126"] = {"class_type": "BasicGuider", "inputs": {"model": model, "conditioning": cond}}
    sig = _sampler_nodes(wf, rc, model, seed)
    if two:   # 两段同一条件（ref2va_run.py；参考图条件与画布尺寸无关）
        _upscale_tail(wf, ts, sig, ["131", 1], ["126", 0], w, h)
        _outputs(wf, ["164", 0], prefix, rc["frames"]["fps"])
        return wf
    wf["125"] = {"class_type": "SamplerCustomAdvanced",
                 "inputs": {"noise": ["129", 0], "guider": ["126", 0], "sampler": ["123", 0], "sigmas": sig,
                            "latent_image": ["131", 1]}}
    _outputs(wf, ["125", 0], prefix, rc["frames"]["fps"])
    return wf


def dangling_links(wf: dict) -> list[str]:
    errs = []
    for nid, node in wf.items():
        for k, v in node.get("inputs", {}).items():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and isinstance(v[1], int):
                if v[0] not in wf:
                    errs.append(f"{nid}.{k} → {v[0]} 不存在")
    return errs
