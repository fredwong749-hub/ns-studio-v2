"""
API 格式工作流。

- qwen_t2i：Qwen Image 2.1（Turbo 8 步或 Base），节点结构来自 ComfyUI 官方 2.1 示例（旧系统 config/comfy_templates 已验证）。
- qwen_edit：Qwen Image 2.1 单参考编辑（锁脸），结构同旧系统 qwen_image_2_1_turbo_edit.json：参考图作为
  TextEncodeQwenImage21 的 images.image_1（并接 VAE）。参考图先按画布比例居中裁切（ImageScale crop=center），
  因为画布与参考图比例一致时身份保持最好（research v5：眼距波动 ±3.9% vs ±18.9%）。
- h3_fl2va：MiniMax H3 首帧生视频（含同步音频），节点结构来自旧系统 adapters/video/comfy_h3/workflow.py。
LoRA 统一用 LoraLoaderModelOnly 串在 UNET 后面。
"""
from dataclasses import dataclass, field

TURBO_SIGMAS = "1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0"


@dataclass(frozen=True)
class QwenModel:
    unet: str = "qwen_image_2.1_turbo_int8_convrot.safetensors"
    clip: str = "qwen3vl_8b_int8_convrot.safetensors"
    vae: str = "qwen_image_2.1_vae_bf16.safetensors"
    turbo: bool = True          # Turbo 用官方 8 步 sigma；Base 用 steps/cfg
    steps: int = 25
    cfg: float = 2.5
    sampler: str = "euler"


# ---- H3 参数（docs/SOP_parameters.md S4；依据 docs/research/h3_deep/00_summary.md、h3_prompt_and_fl2v.md §2）----
# 与 gpu3 weita_render.py（H3_DIT / TURBO["t8"] / TURBO_SHIFT）保持一致；docs/research_adoption.md 记录来源。
H3_DIT_INT8 = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"   # P-A1：w4a8 + LoRA 时 adaln_proj 补丁静默失效（ComfyUI #16420）
H3_TURBO_8STEP_768P = "minimax_h3_fl2v_turbo_8step_v1.1_768p_comfyui_bf16.safetensors"   # P-A4（与 gpu3 现行一致）
SHIFT_768P_TURBO = (6.0, 3.0)   # P-A3：768p Turbo LoRA 的训练 shift（LightX2V 规格表）
SHIFT_BASE = (12.0, 3.0)        # P-A3：基模 / 544p Turbo LoRA（FL2VA model_index.json sigma_shift_scales）
H3_MIN_TURBO_STEPS = 6          # 社区 ≥8 人：4 步慢动作、蜡化、音频差（02 §2.1）
H3_FRAMES_MIN, H3_FRAMES_MAX = 107, 362   # 4–15 s，17k+5 网格（production_rules_v3 K4：与 ns_schema.h3_lint.MIN_FRAMES 一致；4 s 档画质待实测）
# 有损/会改长相的加速节点：一律不进 H3 图（00_summary 速度/稳定 2；03 §2.3、§5 #1）
H3_BANNED_NODES = ("EasyCache", "LazyCache", "MiniMaxH3Cache", "SpectrumPatch", "Spectrum", "TeaCache",
                   "CacheDiT", "SolAttention", "TorchCompileModel", "ModelPatchTorchSettings")


@dataclass(frozen=True)
class H3Model:
    unet: str = H3_DIT_INT8
    clip: str = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
    video_vae: str = "minimax_h3_video_vae_int8_convrot.safetensors"   # int8 视频 VAE 可用（PSNR 56.7 dB）
    audio_vae: str = "minimax_h3_audio_vae_fp32.safetensors"           # 音频 VAE 必须 fp32
    loras: tuple[tuple[str, float], ...] = ((H3_TURBO_8STEP_768P, 1.0),)   # 强度 1.0（低→暗，高→过曝）
    steps: int = 8
    sampler: str = "euler"      # 不用 ancestral/SDE（音频破音 #15283）；euler 变暗约减半（HF Comfy #67）
    scheduler: str = "simple"
    fps: int = 24
    shift: tuple[float, float] = SHIFT_768P_TURBO   # (video, audio) → MiniMaxH3SigmaShift
    keyframe_crop: bool = True  # P-B2：首尾帧在图里用同一种 center-crop+lanczos 到画布（节点对首帧拉伸、尾帧裁切）
    # fredw-92/PF：手写 sigma（Pika H3 Flex 官方模板 ManualSigmas）。设了就用 ManualSigmas 代替 BasicScheduler，
    # 且不加 MiniMaxH3SigmaShift（与官方模板 video_pika_h3_flex_fl2va.json 相同：shift 12 已算进 sigma 表）
    sigmas: str | None = None


# 升级/重试档：基模 20 步、无 LoRA、shift 12/3（SOP S4 P-A3/P-A5 锁定）
H3_BASE = H3Model(loras=(), steps=20, shift=SHIFT_BASE)


def expected_shift(loras) -> tuple[float, float]:
    """LoRA 与 shift 的配对规则（h3_prompt_and_fl2v.md §2 Turbo 表）：768p Turbo → 6/3；544p Turbo 或无 Turbo → 12/3"""
    turbo = [n for n, _ in loras if "turbo" in n.lower()]
    if not turbo:
        return SHIFT_BASE
    return SHIFT_768P_TURBO if all("768p" in n.lower() for n in turbo) else SHIFT_BASE


def check_h3_model(m: H3Model) -> None:
    """出图前的参数自检（PI-23）：不合规直接报错，不消耗 GPU"""
    errs = []
    turbo = [n for n, _ in m.loras if "turbo" in n.lower()]
    if "w4a8" in m.unet.lower() and m.loras:
        errs.append(f"{m.unet} + LoRA：w4a8 下 LoRA 的 adaln_proj 不生效（ComfyUI #16420），改用 int8_convrot")
    if tuple(m.shift) != expected_shift(m.loras):
        errs.append(f"shift {tuple(m.shift)} 与 LoRA {turbo or '无'} 不配对，应为 {expected_shift(m.loras)}")
    if turbo and m.steps < H3_MIN_TURBO_STEPS:
        errs.append(f"Turbo 步数 {m.steps} < {H3_MIN_TURBO_STEPS}")
    if turbo and any("pika_h3_flex" in n.lower() for n, _ in m.loras):
        errs.append("Pika H3 Flex 已蒸馏，不能再叠 Turbo LoRA（官方：do not stack a turbo or Lightning LoRA）")
    if m.sigmas and len([x for x in m.sigmas.split(",") if x.strip()]) < 7:
        errs.append("手写 sigma 少于 7 个值（Pika Flex 官方：sigma 表少于 7 个值会破坏调度）")
    if any(k in m.sampler.lower() for k in ("ancestral", "sde")):
        errs.append(f"采样器 {m.sampler}：H3 不用 ancestral/SDE")
    if errs:
        raise ValueError("H3 参数不合规：" + "；".join(errs))


def turbo_sigmas(denoise: float) -> str:
    """Turbo 8 步 sigma 的低重绘版（指令改图，W2）：从 sigma = denoise 起步（flow 模型 sigma = 噪声占比），
    接官方表里小于它的各步。denoise 0.35 → "0.35, 0"；0.75 → "0.75, 0.704534, 0.414568, 0"。"""
    d = min(max(float(denoise), 0.05), 1.0)
    vals = [float(x) for x in TURBO_SIGMAS.split(",")]
    if d >= vals[0]:
        return TURBO_SIGMAS
    rest = [v for v in vals if v < d]
    return ", ".join(f"{v:g}" for v in [round(d, 6), *rest])


def _lora_chain(wf: dict, model: list, loras, first_id: int) -> list:
    for i, (name, strength) in enumerate(loras):
        nid = str(first_id + i)
        wf[nid] = {"class_type": "LoraLoaderModelOnly",
                   "inputs": {"model": model, "lora_name": name, "strength_model": float(strength)}}
        model = [nid, 0]
    return model


def effective_cfg(m: QwenModel) -> float:
    """送进 CFGGuider 的实际 cfg（Turbo 一律 1.0）"""
    return 1.0 if m.turbo else float(m.cfg)


def negative_for(m: QwenModel, negative: str) -> str:
    """FIX（ep02_iter P0 ⑥，chain_audit A3）：cfg ≤ 1 时负面分支不参与采样 → 不送、不记录负面词（记了只会误导）"""
    return "" if effective_cfg(m) <= 1.0 else (negative or "")


def _qwen_graph(prompt: str, negative: str, width: int, height: int, seed: int, prefix: str,
                m: QwenModel, loras, reference: str | list[str] | None = None, ref_resolution: int = 1024,
                crop: bool = True, init_image: str | None = None, denoise: float = 1.0) -> dict:
    wf: dict = {
        "10": {"class_type": "UNETLoader", "inputs": {"unet_name": m.unet, "weight_dtype": "default"}},
        "11": {"class_type": "CLIPLoader", "inputs": {"clip_name": m.clip, "type": "qwen_image", "device": "default"}},
        "12": {"class_type": "VAELoader", "inputs": {"vae_name": m.vae}},
    }
    model = _lora_chain(wf, ["10", 0], loras, 60)
    enc = {"clip": ["11", 0], "prompt": prompt, "negative_prompt": negative_for(m, negative), "resolution": 1024}
    refs = [reference] if isinstance(reference, str) else list(reference or [])
    if refs:
        enc.update({"resolution": ref_resolution, "vae": ["12", 0]})
    for i, ref in enumerate(refs):
        load_id = str(101 + 2 * i)
        wf[load_id] = {"class_type": "LoadImage", "inputs": {"image": ref}}
        src = [load_id, 0]
        if crop:
            # 居中裁到画布比例（同时缩放到画布尺寸；编码器内部再按 ref_resolution 缩放）
            scale_id = str(102 + 2 * i)
            wf[scale_id] = {"class_type": "ImageScale", "inputs": {"image": src, "upscale_method": "lanczos",
                                                                   "width": width, "height": height,
                                                                   "crop": "center"}}
            src = [scale_id, 0]
        enc[f"images.image_{i + 1}"] = src
    wf.update({
        "13": {"class_type": "QwenImage21Cache", "inputs": {"model": model, "device": "auto", "dtype": "default"}},
        "20": {"class_type": "TextEncodeQwenImage21", "inputs": enc},
        "34": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "35": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "36": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": m.sampler}},
        "38": {"class_type": "CFGGuider", "inputs": {"model": ["13", 0], "positive": ["20", 0], "negative": ["20", 1],
                                                     "cfg": effective_cfg(m)}},
        "40": {"class_type": "SamplerCustomAdvanced",
               "inputs": {"noise": ["35", 0], "guider": ["38", 0], "sampler": ["36", 0], "sigmas": ["37", 0],
                          "latent_image": ["34", 0]}},
        "41": {"class_type": "VAEDecode", "inputs": {"samples": ["40", 0], "vae": ["12", 0]}},
        "90": {"class_type": "SaveImage", "inputs": {"images": ["41", 0], "filename_prefix": prefix}},
    })
    if m.turbo:
        sig = turbo_sigmas(denoise) if init_image and denoise < 1 else TURBO_SIGMAS
        wf["37"] = {"class_type": "ManualSigmas", "inputs": {"sigmas": sig}}
    else:
        wf["37"] = {"class_type": "BasicScheduler",
                    "inputs": {"model": ["13", 0], "scheduler": "simple", "steps": m.steps, "denoise": denoise}}
    if init_image:
        # 低重绘第二遍（research v8 §4）：从已有图的 latent 出发，Base 少步数 denoise≈0.3
        wf["110"] = {"class_type": "LoadImage", "inputs": {"image": init_image}}
        wf["111"] = {"class_type": "ImageScale", "inputs": {"image": ["110", 0], "upscale_method": "lanczos",
                                                            "width": width, "height": height, "crop": "center"}}
        wf["112"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["111", 0], "vae": ["12", 0]}}
        wf["40"]["inputs"]["latent_image"] = ["112", 0]
        del wf["34"]
    return wf


def qwen_t2i(prompt: str, negative: str, width: int, height: int, seed: int, prefix: str,
             m: QwenModel = QwenModel(), loras: list[tuple[str, float]] = ()) -> dict:
    return _qwen_graph(prompt, negative, width, height, seed, prefix, m, loras)


def qwen_edit(prompt: str, negative: str, reference: str | list[str], width: int, height: int, seed: int,
              prefix: str, m: QwenModel = QwenModel(), loras: list[tuple[str, float]] = (),
              ref_resolution: int = 1024, crop: bool = True, init_image: str | None = None,
              denoise: float = 1.0) -> dict:
    """参考编辑。reference: ComfyUI 输入图片名（upload_image 的返回值），或多张（依次为 图1、图2…）。
    crop=False + ref_resolution=0：参考图已在本地预缩放（research v6 §4.2：脸部底板约 768²），原尺寸送入编码器，
    不再裁成画布比例——画面构图只由 EmptyLatentImage 画布和提示词决定。"""
    return _qwen_graph(prompt, negative, width, height, seed, prefix, m, loras,
                       reference=reference, ref_resolution=ref_resolution, crop=crop, init_image=init_image,
                       denoise=denoise)


def qwen_final(prompt: str, negative: str, reference: str | list[str], init_image: str, seed: int, prefix: str,
               m: QwenModel, loras, *, pass1_size: tuple[int, int], pass2_size: tuple[int, int],
               init_denoise: float, pass2_denoise: float, pass2_steps: int, pass2_seed_offset: int = 1) -> dict:
    """定稿档（fredw-92/Q1；config/render_quality.json）：gpu3 weita_render.realism_graph 的两段式。
    第一遍：Base 真 CFG，≈1.5 MP，以预览版为底图（init_denoise，同 identity_graph）并作 图1 参考；
    第二遍：lanczos 放大到 pass2_size → VAEEncode → 低重绘 pass2_denoise（噪声种子 +offset）。节点号同 WR 50–55。"""
    if m.turbo:
        raise ValueError("定稿档必须用 Base（真 CFG）")
    wf = _qwen_graph(prompt, negative, pass1_size[0], pass1_size[1], seed, prefix, m, loras, reference=reference,
                     ref_resolution=0, crop=False, init_image=init_image, denoise=init_denoise)
    wf.update({
        "50": {"class_type": "ImageScale", "inputs": {"image": ["41", 0], "upscale_method": "lanczos",
                                                      "width": pass2_size[0], "height": pass2_size[1],
                                                      "crop": "disabled"}},
        "51": {"class_type": "VAEEncode", "inputs": {"pixels": ["50", 0], "vae": ["12", 0]}},
        "52": {"class_type": "RandomNoise", "inputs": {"noise_seed": int(seed) + int(pass2_seed_offset)}},
        "53": {"class_type": "BasicScheduler", "inputs": {"model": ["13", 0], "scheduler": "simple",
                                                          "steps": int(pass2_steps), "denoise": float(pass2_denoise)}},
        "54": {"class_type": "SamplerCustomAdvanced",
               "inputs": {"noise": ["52", 0], "guider": ["38", 0], "sampler": ["36", 0], "sigmas": ["53", 0],
                          "latent_image": ["51", 0]}},
        "55": {"class_type": "VAEDecode", "inputs": {"samples": ["54", 0], "vae": ["12", 0]}},
    })
    wf["90"]["inputs"]["images"] = ["55", 0]
    return wf


TEXT_MODEL = "qwen3vl_8b_int8_convrot.safetensors"   # Qwen Image 2.1 的文本编码器本身就是 Qwen3-VL 8B


def text_generate(prompt: str, prefix: str, image: str | None = None, max_length: int = 400,
                  clip: str = TEXT_MODEL) -> dict:
    """本地看图/写作模型：ComfyUI TextGenerate + Qwen3-VL 8B（与出图共用同一个编码器文件，不额外占内存）。
    贪心解码（sampling off），结果由 SaveText 输出（history 里 outputs[\"90\"][\"text\"]）。"""
    wf: dict = {
        "11": {"class_type": "CLIPLoader", "inputs": {"clip_name": clip, "type": "qwen_image", "device": "default"}},
        "20": {"class_type": "TextGenerate", "inputs": {"clip": ["11", 0], "prompt": prompt, "max_length": max_length,
                                                        "sampling_mode": "off"}},
        "90": {"class_type": "SaveText", "inputs": {"text": ["20", 0], "filename_prefix": prefix, "format": "txt"}},
    }
    if image:
        wf["101"] = {"class_type": "LoadImage", "inputs": {"image": image}}
        wf["20"]["inputs"]["image"] = ["101", 0]
    return wf


def h3_fl2va(prompt: str, first_frame: str, width: int, height: int, frames: int, seed: int, prefix: str,
             m: H3Model = H3Model(), loras: list[tuple[str, float]] = (), last_frame: str | None = None) -> dict:
    """first_frame / last_frame: ComfyUI 里的输入图片名（upload_image 的返回值）；last_frame 可选"""
    check_h3_model(m)
    if (frames - 5) % 17 or not H3_FRAMES_MIN <= frames <= H3_FRAMES_MAX:
        raise ValueError(f"H3 帧数 {frames} 不在 17k+5 网格或超出 {H3_FRAMES_MIN}–{H3_FRAMES_MAX}")
    wf: dict = {
        "127": {"class_type": "UNETLoader", "inputs": {"unet_name": m.unet, "weight_dtype": "default"}},
        "128": {"class_type": "CLIPLoader", "inputs": {"clip_name": m.clip, "type": "minimax", "device": "default"}},
        "119": {"class_type": "VAELoader", "inputs": {"vae_name": m.video_vae}},
        "120": {"class_type": "VAELoader", "inputs": {"vae_name": m.audio_vae}},
        "200": {"class_type": "LoadImage", "inputs": {"image": first_frame}},
    }
    all_loras = [*m.loras, *loras]
    model = _lora_chain(wf, ["127", 0], all_loras, 140)
    if not m.sigmas:
        shift = expected_shift(all_loras) if loras else tuple(m.shift)   # 额外 LoRA 也按配对规则
        # 采样模型和调度器都要经过 shift 节点（sigma 由调度器的 model 算出；与 gpu3 h3_graph 相同）
        wf["150"] = {"class_type": "MiniMaxH3SigmaShift",
                     "inputs": {"model": model, "shift_video": float(shift[0]), "shift_audio": float(shift[1])}}
        model = ["150", 0]

    def keyframe(load_id: str, scale_id: str) -> list:
        if not m.keyframe_crop:
            return [load_id, 0]
        wf[scale_id] = {"class_type": "ImageScale", "inputs": {"image": [load_id, 0], "upscale_method": "lanczos",
                                                               "width": width, "height": height, "crop": "center"}}
        return [scale_id, 0]

    wf.update({
        "131": {"class_type": "MiniMaxH3ImageToVideo",
                "inputs": {"clip": ["128", 0], "prompt": prompt, "width": width, "height": height,
                           "length": frames, "vae": ["119", 0], "first_frame": keyframe("200", "202")}},
    })
    if last_frame:
        wf["201"] = {"class_type": "LoadImage", "inputs": {"image": last_frame}}
        wf["131"]["inputs"]["last_frame"] = keyframe("201", "203")
    wf.update({
        "126": {"class_type": "BasicGuider", "inputs": {"model": model, "conditioning": ["131", 0]}},
        "129": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "123": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": m.sampler}},
        "124": ({"class_type": "ManualSigmas", "inputs": {"sigmas": m.sigmas}} if m.sigmas else
                {"class_type": "BasicScheduler",
                 "inputs": {"model": model, "scheduler": m.scheduler, "steps": m.steps, "denoise": 1.0}}),
        "125": {"class_type": "SamplerCustomAdvanced",
                "inputs": {"noise": ["129", 0], "guider": ["126", 0], "sampler": ["123", 0], "sigmas": ["124", 0],
                           "latent_image": ["131", 1]}},
        "122": {"class_type": "VAEDecode", "inputs": {"samples": ["125", 0], "vae": ["119", 0]}},
        "121": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["125", 0], "vae": ["120", 0]}},
        "130": {"class_type": "CreateVideo", "inputs": {"images": ["122", 0], "audio": ["121", 0], "fps": m.fps}},
        "92": {"class_type": "SaveVideo", "inputs": {"video": ["130", 0], "filename_prefix": prefix,
                                                     "format": "mp4", "codec": "auto"}},
    })
    return wf
