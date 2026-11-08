"""
出图参数（fredw-92/PX，项目方“加模型参数”）：命名预设 + 每次覆盖，作用在画布的 Image 2.1 出图上。

- 预设在 config/render_quality.json：image_presets（内置：预览·Turbo = 现状、定稿·Base 两段式 = Q1 定稿档、
  data/ab/texture_ep02_S4/settings.json 的 A/B 变体 B–F）+ user_presets（画布“存为我的预设”写进来）。
- resolve(preset, overrides) → 完整有效参数（校验不过抛 ParamError，消息是中文，API 返回 422）。
- apply(wf, eff) 在提交 ComfyUI 前改工作流（UNET / VAE / LoRA 链 / 采样器 / 调度器 / 步数 / cfg / shift / 尺寸 /
  第二遍 / 改图幅度）；describe(wf) 从实际提交的工作流读回参数，记进 take.params（任何一张图都能照着复现）。
- 画布 frame-jobs 预览档不传 preset / params = config default_preset（项目方 2026-10-31 选定 C·Turbo 质感）；
  default_preset 设成 preview_turbo 即回到旧行为。定稿档不传参数 = Q1 原样。apply(wf, None) 不改工作流。

Turbo（qwen_image_2.1_turbo）是蒸馏模型：用官方固定 8 步 sigma（workflows.TURBO_SIGMAS）、cfg 1，
步数 / cfg / 调度器 / shift 对它不生效（改图幅度 < 1 时从 sigma = 幅度 起步，接官方表里更小的各步）。
不做内容检测：提示词原样；去掉“系统规格词”只是把系统追加的固定句删掉，不改写作者原文。
"""
from __future__ import annotations

import copy
import json
import re
import threading
from pathlib import Path

from . import pipeline as P
from . import workflows as W

CONFIG = P.ROOT / "config" / "render_quality.json"
FINAL_PRESET = "final_base_2pass"
IDENTITY = "@identity"              # LoRA 行里的占位：本镜自动带的身份 LoRA（如林晚 lin_wan_qi_v1），强度用这一行的
UNETS = {"turbo": "qwen_image_2.1_turbo_int8_convrot.safetensors", "base": "qwen_image_2.1_int8_convrot.safetensors"}
VAES = {"plain": "qwen_image_2.1_vae_bf16.safetensors",
        "texture_fix": "texture_fix_vae_for_qwen_image_2.1_bf16.safetensors"}
SAMPLERS = ("euler", "euler_ancestral", "heun", "dpmpp_2m", "dpmpp_2m_sde", "dpmpp_sde", "res_multistep", "uni_pc",
            "lcm", "ddim")
SCHEDULERS = ("simple", "normal", "karras", "exponential", "sgm_uniform", "beta", "linear_quadratic", "kl_optimal",
              "ddim_uniform")
TURBO_NOTE = "Turbo 是蒸馏模型：用官方固定 8 步 sigma、cfg 1，步数 / cfg / 调度器 / shift 对它不生效"
RANGES = {
    "steps": (1, 100), "cfg": (1.0, 10.0), "shift": (0.5, 20.0), "size": (256, 2048), "size_step": 16,
    "strength": (0.05, 1.0), "lora_strength": (-1.0, 2.0), "lora_max": 8, "grade_strength": (0.0, 1.0),
    "two_stage_denoise": (0.05, 0.8), "two_stage_steps": (1, 60), "seed": (0, 2 ** 53),
    "sharpen_radius": (0.1, 10.0), "sharpen_amount": (0.0, 3.0), "render_scale": (0.5, 2.0),
    "detail_denoise": (0.05, 0.6), "detail_scale": (1.0, 2.0), "scaled_max": 3072,
    "sharpen_threshold": (0, 255), "style_suffix_max": 500,
}
# 旧现状（2026-10-31 前的预览档：Turbo 单遍、画布 1088×1920、LoRA 按剧本自动、调色按现状、带系统规格词）。
# 字段基准；画布默认用的是 config default_preset（项目方选定 C）
DEFAULTS: dict = {
    "unet": "turbo", "steps": 8, "cfg": 1.0, "sampler": "euler", "scheduler": "simple", "shift": None,
    "vae": "plain", "loras": None,
    "two_stage": {"enabled": False, "first_size": [928, 1632], "upscale_size": [1152, 2016], "denoise": 0.3,
                  "steps": 14},
    "width": P.IMAGE_SIZE[0], "height": P.IMAGE_SIZE[1], "seed": None, "strength": None,
    "grade": {"enabled": None, "strength": 0.6}, "spec_words": True,
    # 锐度后处理（项目方 2026-10-31：C 仍有磨皮感，要徕卡式锐利 → 锐度对比轮的候选项）
    # 出图后 USM 锐化（PIL UnsharpMask，amount 1 = 100%；threshold 同 PIL，0 = 全部像素）
    "sharpen": {"enabled": False, "radius": 1.0, "amount": 0.5, "threshold": 0},
    "render_scale": 1.0,                                            # 先按 ×倍尺寸出（= gen_scale），再 lanczos 缩回 宽×高
    "detail_pass": {"enabled": False, "denoise": 0.25, "scale": 1.5},  # 细节重绘：放大 ×scale 低重绘一遍再缩回
    # LCA：风格后缀（项目方选定的写作模型原文）。非空 = 前加“，”原样接在送进 Image 2.1 的提示词末尾，不改一个字
    "style_suffix": "",
}
STYLE_SEP = "，"
FIELD_LABEL = {
    "unet": "模型", "steps": "步数", "cfg": "cfg", "sampler": "采样器", "scheduler": "调度器", "shift": "shift",
    "vae": "VAE", "loras": "LoRA", "two_stage": "两段式", "width": "宽", "height": "高", "seed": "种子",
    "strength": "重绘幅度", "grade": "调色", "spec_words": "系统规格词", "sharpen": "锐化",
    "render_scale": "放大出图再缩回", "detail_pass": "细节重绘", "style_suffix": "风格后缀",
}
DEFAULT_PRESET = "leica_sstar"         # config 里没写 default_preset 时（项目方 2026-10-31 选定 S★）
# 项目方 2026-10-31 选定的后缀 = data/ab/leica_sharp/style_suffix_candidates.md 候选1（写作模型原文，不改字）
LEICA_SUFFIX = "专业电影画面，徕卡M 50mm f/2镜头，焦点锐利，微反差强，皮肤纹理清晰，自然高光，暖调通透，干净真实"
# config 被覆盖丢了 image_presets 时的兜底（画布默认 S★、C、旧 A、定稿档）
BUILTIN_FALLBACK = [
    {"id": "leica_sstar", "label": "徕卡 S★", "tier": "preview",
     "params": {"unet": "turbo", "sampler": "euler", "vae": "texture_fix", "width": 768, "height": 1344,
                "spec_words": True, "loras": [{"name": "krea2_film_texture_qwen21.safetensors", "strength": 0.5},
                                              {"name": "@identity", "strength": 0.6}],
                "render_scale": 1.5, "sharpen": {"enabled": True, "radius": 1.2, "amount": 0.6, "threshold": 2},
                "style_suffix": LEICA_SUFFIX},
     "note": "项目方 2026-10-31 选定（代码兜底）"},
    {"id": "c_turbo_texture", "label": "C·Turbo 质感（项目方选定）", "tier": "preview",
     "params": {"unet": "turbo", "sampler": "euler", "vae": "texture_fix", "width": 768, "height": 1344,
                "spec_words": True, "loras": [{"name": "krea2_film_texture_qwen21.safetensors", "strength": 0.5},
                                              {"name": "@identity", "strength": 0.6}]},
     "note": "项目方 2026-10-31 选定（代码兜底）"},
    {"id": "preview_turbo", "label": "预览·Turbo（旧 A）", "tier": "preview", "params": {}},
    {"id": "final_base_2pass", "label": "定稿·Base 两段式", "tier": "final",
     "params": {"unet": "base", "steps": 35, "cfg": 3.0, "vae": "texture_fix", "strength": 0.5,
                "two_stage": {"enabled": True, "first_size": [928, 1632], "upscale_size": [1152, 2016],
                              "denoise": 0.30, "steps": 14}, "grade": {"enabled": True, "strength": 0.6}}},
]
NOTES = {
    "turbo": TURBO_NOTE + "（sigma：" + W.TURBO_SIGMAS + "）",
    "shift": "shift 只对 Base 有效：加 ModelSamplingAuraFlow 节点；不填 = 不加（现状，BasicScheduler simple 原样）",
    "strength": "重绘幅度：指令改图 / 换参考的底图重绘（不填 = 按 小 0.35 / 中 0.55 / 大 0.75）；定稿档 = 以父版本为底图的重绘（默认 0.5）",
    "grade": "调色：不填 = 现状（首帧不调色、尾帧向定稿首帧 0.7、定稿档 0.6）；开 = Reinhard LAB 向色调参考（尾帧向定稿首帧）",
    "spec_words": "系统规格词 = 编译器自动追加的镜头句（Leica）、风格后缀、纠正器条目；关掉只删这些固定句，作者原文不动",
    "loras": "不填 = 按剧本自动（身份 LoRA）；@identity = 本镜自动带的身份 LoRA，强度用这一行的",
    "two_stage": "两段式：第一遍 first_size 出图 → lanczos 放大到 upscale_size → 低重绘第二遍（噪声种子 +1）",
    "sharpen": "锐化：出图（和调色、缩回）之后做 USM（PIL UnsharpMask，amount 1 = 100%，threshold 同 PIL），另存 *_post.png，原图保留",
    "render_scale": "放大出图再缩回（gen_scale）：按 宽×高×倍数（取 16 的倍数）出图，再 PIL lanczos 缩回 宽×高，放大原图保留。"
                    "带底图的改图（指令改图 / 换参考的底图重绘）不放大：底图就是交付尺寸的父版本，按交付尺寸出（后缀、锐化照做）",
    "style_suffix": "风格后缀：项目方选定的写作模型原文，前加“，”原样接在提示词末尾（首帧 / 尾帧 / 改图 / 资产 / 场景图都接），不改字",
    "detail_pass": "细节重绘：第一遍后 lanczos 放大 ×scale、低重绘 denoise 一遍（步数用两段式的第二遍步数；Turbo 用官方 sigma 截断），再缩回 宽×高；与两段式二选一",
    "default": "画布出图默认 = config default_preset（项目方 2026-10-31 选定 徕卡 S★：C + 1.5 倍出图缩回 + USM 1.2/60/2 + 风格后缀）；定稿档不受影响",
}
_io_lock = threading.Lock()


def _opts(oi: dict, cls: str, key: str) -> list[str]:
    try:
        v = oi[cls]["input"]["required"][key]
    except (KeyError, TypeError):
        return []
    if isinstance(v, list) and v and v[0] == "COMBO" and len(v) > 1 and isinstance(v[1], dict):
        return list(v[1].get("options") or [])
    return list(v[0]) if isinstance(v, list) and v and isinstance(v[0], list) else []


def allowed_from_object_info(oi: dict) -> dict:
    """ComfyUI /object_info → 面板可选值；连不上时（oi = {}）给内置值，LoRA 列表为空（不按它校验）"""
    oi = oi or {}
    unets, vaes = _opts(oi, "UNETLoader", "unet_name"), _opts(oi, "VAELoader", "vae_name")
    return {
        "loras": sorted(_opts(oi, "LoraLoaderModelOnly", "lora_name")),
        "samplers": _opts(oi, "KSamplerSelect", "sampler_name") or list(SAMPLERS),
        "schedulers": _opts(oi, "BasicScheduler", "scheduler") or list(SCHEDULERS),
        "unets": {k: {"file": f, "available": (f in unets) if unets else None} for k, f in UNETS.items()},
        "vaes": {k: {"file": f, "available": (f in vaes) if vaes else None} for k, f in VAES.items()},
        "shift_node": ("ModelSamplingAuraFlow" in oi) if oi else None,
    }


class ParamError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("；".join(errors))
        self.errors = errors


# ------------------------------------------------------------------ 预设

def quality(path: Path | None = None) -> dict:
    return json.loads(Path(path or CONFIG).read_text(encoding="utf-8"))


def presets(path: Path | None = None) -> list[dict]:
    q = quality(path)
    out = []
    for p in q.get("image_presets") or BUILTIN_FALLBACK:
        out.append({**p, "builtin": True})
    for p in q.get("user_presets") or []:
        out.append({**p, "builtin": False})
    return out


def default_preset(path: Path | None = None) -> str:
    """画布 frame-jobs 预览档不传 preset / params 时用的预设（项目方 2026-10-31 选定 C）"""
    try:
        pid = quality(path).get("default_preset") or DEFAULT_PRESET
    except (OSError, ValueError):
        pid = DEFAULT_PRESET
    return pid if find_preset(pid, path) else "preview_turbo"


def find_preset(pid: str | None, path: Path | None = None) -> dict | None:
    if not pid:
        return None
    return next((p for p in presets(path) if p.get("id") == pid), None)


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def save_user_preset(label: str, params: dict, *, tier: str = "preview", note: str = "", base: str | None = None,
                     path: Path | None = None) -> dict:
    """“存为我的预设”：校验后追加到 render_quality.json user_presets（同名覆盖）"""
    label = (label or "").strip()
    if not label or len(label) > 40:
        raise ParamError(["预设名不能为空，最多 40 个字"])
    if tier not in ("preview", "final"):
        raise ParamError(["tier 只能是 preview | final"])
    resolve(None, params, tier=tier)            # 校验
    cfgp = Path(path or CONFIG)
    with _io_lock:
        q = json.loads(cfgp.read_text(encoding="utf-8"))
        if any(p.get("label") == label for p in q.get("image_presets") or []):
            raise ParamError([f"“{label}”是内置预设的名字，换一个"])
        users = [p for p in q.get("user_presets") or [] if p.get("label") != label]
        pid = "user_" + (re.sub(r"[^0-9A-Za-z]+", "_", label).strip("_").lower() or "")
        pid = pid if pid != "user_" and not any(p.get("id") == pid for p in users) else f"user_{P.now().replace(':', '').replace('-', '')}"
        rec = {"id": pid, "label": label, "tier": tier, "params": params, "note": note, "base": base,
               "created_at": P.now()}
        users.append(rec)
        q["user_presets"] = users
        tmp = cfgp.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(q, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(cfgp)
    return {**rec, "builtin": False}


def delete_user_preset(pid: str, path: Path | None = None) -> bool:
    cfgp = Path(path or CONFIG)
    with _io_lock:
        q = json.loads(cfgp.read_text(encoding="utf-8"))
        users = q.get("user_presets") or []
        keep = [p for p in users if p.get("id") != pid]
        if len(keep) == len(users):
            return False
        q["user_presets"] = keep
        tmp = cfgp.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(q, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(cfgp)
    return True


# ------------------------------------------------------------------ 校验

def _num(errs, name, v, lo, hi, *, integer=False):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        errs.append(f"{name} 要是数字")
        return None
    if integer and int(v) != v:
        errs.append(f"{name} 要是整数")
        return None
    if not lo <= v <= hi:
        errs.append(f"{name} 要在 {lo}–{hi} 之间（现在 {v}）")
        return None
    return int(v) if integer else float(v)


def _size(errs, name, v):
    lo, hi = RANGES["size"]
    st = RANGES["size_step"]
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi or v % st:
        errs.append(f"{name} 要是 {lo}–{hi} 之间 {st} 的倍数（现在 {v}）")
        return None
    return v


def _pair(errs, name, v):
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        errs.append(f"{name} 要写成 [宽, 高]")
        return None
    w, h = _size(errs, f"{name} 宽", v[0]), _size(errs, f"{name} 高", v[1])
    return [w, h] if w and h else None


def resolve(preset: str | None, overrides: dict | None, *, tier: str | None = None, mode: str = "regen",
            allowed: dict | None = None, path: Path | None = None) -> dict:
    """预设 + 覆盖 → 完整有效参数（含 preset / tier）。allowed：GET image-presets 的 allowed（ComfyUI 实时列表），
    给了就按它核对采样器 / 调度器 / LoRA 名。校验不过 → ParamError（中文，全部问题一次列出）"""
    errs: list[str] = []
    q = quality(path)
    pr = None
    if preset:
        pr = find_preset(preset, path)
        if pr is None:
            raise ParamError([f"没有预设 {preset}"])
    elif tier == "final":
        pr = find_preset(FINAL_PRESET, path)      # 定稿档不选预设 = 定稿档配置
    over = overrides or {}
    if not isinstance(over, dict):
        raise ParamError(["params 要是对象"])
    unknown = [k for k in over if k not in DEFAULTS]
    if unknown:
        errs.append("不认识的参数：" + "、".join(unknown))
    eff = _merge(_merge(DEFAULTS, (pr or {}).get("params") or {}), {k: v for k, v in over.items() if k in DEFAULTS})
    eff_tier = tier or (pr or {}).get("tier") or "preview"
    if eff_tier not in ("preview", "final"):
        errs.append("tier 只能是 preview | final")
    al = allowed or {}

    if eff["unet"] not in UNETS:
        errs.append("模型 unet 只能是 turbo | base")
    turbo = eff["unet"] == "turbo"
    if eff["vae"] not in VAES:
        errs.append("VAE 只能是 plain（普通）| texture_fix（纹理修复）")
    steps = _num(errs, "步数 steps", eff["steps"], *RANGES["steps"], integer=True)
    cfg = _num(errs, "cfg", eff["cfg"], *RANGES["cfg"])
    samplers = al.get("samplers") or SAMPLERS
    if eff["sampler"] not in samplers:
        errs.append(f"采样器 {eff['sampler']} 不在 ComfyUI 的列表里")
    schedulers = al.get("schedulers") or SCHEDULERS
    if eff["scheduler"] not in schedulers:
        errs.append(f"调度器 {eff['scheduler']} 不在 ComfyUI 的列表里")
    if eff["shift"] is not None:
        if turbo and "shift" in over:
            errs.append("Turbo 用官方固定 8 步 sigma，不能设 shift（只有 Base 能设）")
        _num(errs, "shift", eff["shift"], *RANGES["shift"])
    w = _size(errs, "宽 width", eff["width"])
    h = _size(errs, "高 height", eff["height"])
    if eff["seed"] is not None:
        eff["seed"] = _num(errs, "种子 seed", eff["seed"], *RANGES["seed"], integer=True)
    if eff["strength"] is not None:
        eff["strength"] = _num(errs, "重绘幅度 strength", eff["strength"], *RANGES["strength"])
    if not isinstance(eff["spec_words"], bool):
        errs.append("系统规格词 spec_words 只能是 true / false")

    g = eff["grade"]
    if not isinstance(g, dict):
        errs.append("调色 grade 要写成 {enabled, strength}")
    else:
        if g.get("enabled") not in (None, True, False):
            errs.append("调色 grade.enabled 只能是 true / false / null（按现状）")
        _num(errs, "调色强度 grade.strength", g.get("strength", 0.6), *RANGES["grade_strength"])

    ts = eff["two_stage"]
    if not isinstance(ts, dict):
        errs.append("两段式 two_stage 要写成 {enabled, first_size, upscale_size, denoise, steps}")
    else:
        if not isinstance(ts.get("enabled"), bool):
            errs.append("two_stage.enabled 只能是 true / false")
        fs = _pair(errs, "第一遍尺寸 first_size", ts.get("first_size"))
        us = _pair(errs, "放大尺寸 upscale_size", ts.get("upscale_size"))
        _num(errs, "第二遍重绘 two_stage.denoise", ts.get("denoise"), *RANGES["two_stage_denoise"])
        _num(errs, "第二遍步数 two_stage.steps", ts.get("steps"), *RANGES["two_stage_steps"], integer=True)
        if fs and us and ts.get("enabled") and (us[0] < fs[0] or us[1] < fs[1]):
            errs.append("放大尺寸要不小于第一遍尺寸")

    sh = eff["sharpen"]
    if not isinstance(sh, dict) or not isinstance(sh.get("enabled"), bool):
        errs.append("锐化 sharpen 要写成 {enabled: true/false, radius, amount}")
    else:
        _num(errs, "锐化半径 sharpen.radius", sh.get("radius"), *RANGES["sharpen_radius"])
        _num(errs, "锐化强度 sharpen.amount", sh.get("amount"), *RANGES["sharpen_amount"])
        _num(errs, "锐化阈值 sharpen.threshold", sh.get("threshold", 0), *RANGES["sharpen_threshold"], integer=True)
    suf = eff["style_suffix"]
    if suf is None:
        eff["style_suffix"] = ""
    elif not isinstance(suf, str):
        errs.append("风格后缀 style_suffix 要是文字（空 = 不加）")
    elif len(suf) > RANGES["style_suffix_max"]:
        errs.append(f"风格后缀最多 {RANGES['style_suffix_max']} 个字")
    rs =_num(errs, "放大出图倍数 render_scale", eff["render_scale"], *RANGES["render_scale"])
    dp = eff["detail_pass"]
    if not isinstance(dp, dict) or not isinstance(dp.get("enabled"), bool):
        errs.append("细节重绘 detail_pass 要写成 {enabled: true/false, denoise, scale}")
        dp = {}
    else:
        _num(errs, "细节重绘幅度 detail_pass.denoise", dp.get("denoise"), *RANGES["detail_denoise"])
        _num(errs, "细节重绘放大 detail_pass.scale", dp.get("scale"), *RANGES["detail_scale"])
    two_on = isinstance(ts, dict) and ts.get("enabled") is True
    if dp.get("enabled") and two_on:
        errs.append("细节重绘和两段式都是“放大再低重绘一遍”，只能开一个")
    if two_on and rs and rs != 1.0:
        errs.append("两段式时不用 render_scale（尺寸由 first_size / upscale_size 决定）")
    if w and h and rs:
        k = rs * (float(dp.get("scale") or 1.0) if dp.get("enabled") else 1.0)
        if max(w, h) * k > RANGES["scaled_max"]:
            errs.append(f"放大后长边 {round(max(w, h) * k)} 超过 {RANGES['scaled_max']}，调小倍数或宽高")

    loras = eff["loras"]
    if loras is not None:
        banned = set(((q.get("tiers") or {}).get("final") or {}).get("banned_loras") or [])
        avail = set(al.get("loras") or [])
        if not isinstance(loras, list):
            errs.append("LoRA 要写成 [{name, strength}] 列表（null = 按剧本自动）")
            loras = []
        if len(loras) > RANGES["lora_max"]:
            errs.append(f"LoRA 最多 {RANGES['lora_max']} 个")
        out, seen = [], set()
        for i, x in enumerate(loras):
            if not isinstance(x, dict) or not isinstance(x.get("name"), str):
                errs.append(f"第 {i + 1} 个 LoRA 要写成 {{name, strength}}")
                continue
            name = x["name"].strip()
            st = _num(errs, f"LoRA {name} 强度", x.get("strength", 1.0), *RANGES["lora_strength"])
            if name != IDENTITY:
                if not name.endswith(".safetensors") or "\\" in name or ".." in name.split("/") or name.startswith("/"):
                    errs.append(f"LoRA 名不合法：{name}")
                    continue
                if name in banned:
                    errs.append(f"{name} 是禁用 LoRA（config/visual_bible.toml [realism].banned_loras）")
                if avail and name not in avail:
                    errs.append(f"ComfyUI 上没有这个 LoRA：{name}")
            if name in seen:
                errs.append(f"LoRA {name} 重复了")
            seen.add(name)
            if st is not None:
                out.append({"name": name, "strength": st})
        eff["loras"] = out

    if eff_tier == "final":
        if mode != "regen":
            errs.append("定稿档只能整张出（在父版本上出定稿版），不能指令改图 / 换参考")
        if turbo:
            errs.append("定稿档两段式必须用 Base（真 CFG），不能选 Turbo")
        if isinstance(ts, dict) and ts.get("enabled") is False:
            errs.append("定稿档就是两段式，不能关第二遍（要单遍请用预览档 + Base）")
        if (rs and rs != 1.0) or dp.get("enabled"):
            errs.append("定稿档不支持 render_scale / 细节重绘（它本身就是放大两段式）")
    if errs:
        raise ParamError(errs)
    eff["width"], eff["height"] = w, h
    eff["steps"], eff["cfg"] = steps, cfg
    if turbo:   # 蒸馏模型：固定值（只记录真实生效的）
        eff.update(steps=8, cfg=1.0, scheduler="simple", shift=None)
    eff["preset"] = (pr or {}).get("id")
    eff["tier"] = eff_tier
    return eff


def final_cfg_for(eff: dict, base: dict | None = None) -> dict:
    """定稿档：有效参数 → final_tier 的 cfg（尺寸 / 步数 / 两段式 / 底图重绘 / 调色）；LoRA 覆盖由 apply 改图"""
    from . import final_tier as FT
    cfg = copy.deepcopy(base or FT.final_cfg())
    ts = eff["two_stage"]
    cfg.update(unet=UNETS[eff["unet"]], vae=VAES[eff["vae"]], steps=int(eff["steps"]), cfg=float(eff["cfg"]),
               sampler=eff["sampler"], pass1_size=list(ts["first_size"]), pass2_size=list(ts["upscale_size"]),
               pass2_denoise=float(ts["denoise"]), pass2_steps=int(ts["steps"]))
    if eff.get("strength") is not None:
        cfg["init_denoise"] = float(eff["strength"])
    g = eff.get("grade") or {}
    if g.get("enabled") is False:
        cfg["grade"] = {}
    elif g.get("enabled") is True:
        cfg["grade"] = {**(cfg.get("grade") or {"method": "reinhard_lab"}), "strength": float(g.get("strength", 0.6))}
    return cfg


# ------------------------------------------------------------------ 工作流

def is_image_graph(wf: dict) -> bool:
    return (wf.get("10") or {}).get("class_type") == "UNETLoader" and \
        (wf.get("20") or {}).get("class_type") == "TextEncodeQwenImage21"


def _chain(wf: dict) -> list[tuple[str, str, float]]:
    """从 QwenImage21Cache 往回走到 UNETLoader：[(节点号, lora_name, strength)]，按加载顺序"""
    out, ref, seen = [], (wf.get("13") or {}).get("inputs", {}).get("model"), set()
    while isinstance(ref, list) and ref and ref[0] != "10" and ref[0] not in seen:
        seen.add(ref[0])
        n = wf.get(ref[0]) or {}
        if n.get("class_type") == "LoraLoaderModelOnly":
            out.append((ref[0], n["inputs"]["lora_name"], float(n["inputs"]["strength_model"])))
        ref = (n.get("inputs") or {}).get("model")
    return list(reversed(out))


def resolve_loras(eff_loras, current: list[tuple[str, float]], identity_files: set[str]) -> list[tuple[str, float]]:
    """覆盖的 LoRA 列表 → 实际加载的 [(文件, 强度)]；@identity 换成本镜自动带的身份 LoRA（没有就不加）"""
    out = []
    for x in eff_loras:
        if x["name"] == IDENTITY:
            for f, _s in current:
                if f in identity_files and f not in [o[0] for o in out]:
                    out.append((f, float(x["strength"])))
        elif x["name"] not in [o[0] for o in out]:
            out.append((x["name"], float(x["strength"])))
    return out


def _r16(v: float) -> int:
    return max(16, int(round(float(v) / 16)) * 16)


def target_size(eff: dict | None) -> tuple[int, int] | None:
    """需要缩回的最终尺寸（render_scale ≠ 1 或开了细节重绘）；不需要 → None"""
    if not eff or eff.get("tier") == "final" or (eff.get("two_stage") or {}).get("enabled"):
        return None
    if float(eff.get("render_scale") or 1.0) != 1.0 or (eff.get("detail_pass") or {}).get("enabled"):
        return int(eff["width"]), int(eff["height"])
    return None


def _sigma_node(turbo: bool, eff: dict, steps: int, denoise: float) -> dict:
    if turbo:
        return {"class_type": "ManualSigmas",
                "inputs": {"sigmas": W.turbo_sigmas(denoise) if denoise < 1 else W.TURBO_SIGMAS}}
    return {"class_type": "BasicScheduler", "inputs": {"model": ["13", 0], "scheduler": eff["scheduler"],
                                                       "steps": int(steps), "denoise": float(denoise)}}


def _denoise_of(node: dict) -> float:
    if node.get("class_type") == "ManualSigmas":
        try:
            return min(1.0, float(str(node["inputs"]["sigmas"]).split(",")[0]))
        except (ValueError, KeyError):
            return 1.0
    return float((node.get("inputs") or {}).get("denoise", 1.0))


def append_style_suffix(wf: dict, suffix: str | None) -> bool:
    """LCA：风格后缀原样接在 TextEncodeQwenImage21 提示词末尾（前加“，”，同 tools/ab/leica_sharp.py variant_prompt）。
    已经以它结尾（同一张图重试）就不再接。返回是否接上"""
    enc = (wf.get("20") or {}).get("inputs") or {}
    p = enc.get("prompt")
    if not suffix or not isinstance(p, str):
        return False
    if p.endswith(STYLE_SEP + suffix) or p == suffix:
        return True
    enc["prompt"] = p + STYLE_SEP + suffix if p else suffix
    return True


def scale_for(wf: dict, eff: dict) -> float:
    """实际生效的放大出图倍数：带底图（节点 112，改图底图重绘）的图不放大——底图是交付尺寸的父版本，
    放大后低重绘只会更软、更慢；按交付尺寸出（后缀、锐化照做）"""
    if "112" in wf:
        return 1.0
    return float(eff.get("render_scale") or 1.0)


def apply(wf: dict, eff: dict | None, identity_files: set[str] | None = None) -> dict:
    """按有效参数改工作流（就地改并返回）。eff=None 或不是 Image 2.1 出图图 → 原样。
    定稿档图（有 50–55 第二遍、tier=final）只改 UNET / VAE / LoRA / 采样器 / 调度器 / shift / cfg，
    尺寸、步数、两段式由 final_tier 按 cfg 覆盖出图。"""
    if not eff or not is_image_graph(wf):
        return wf
    append_style_suffix(wf, eff.get("style_suffix"))
    final = eff.get("tier") == "final" and "53" in wf
    turbo = eff["unet"] == "turbo"
    wf["10"]["inputs"]["unet_name"] = UNETS[eff["unet"]]
    if (wf.get("12") or {}).get("class_type") == "VAELoader":
        wf["12"]["inputs"]["vae_name"] = VAES[eff["vae"]]
    # LoRA 链
    cur = _chain(wf)
    if eff.get("loras") is not None:
        for nid, _f, _s in cur:
            wf.pop(nid, None)
        want = resolve_loras(eff["loras"], [(f, s) for _n, f, s in cur], identity_files or set())
        end = W._lora_chain(wf, ["10", 0], want, 60)
    else:
        end = wf["13"]["inputs"]["model"]
        if end == ["14", 0]:
            end = wf["14"]["inputs"]["model"]
    wf.pop("14", None)
    if eff.get("shift") is not None and not turbo:
        wf["14"] = {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": end, "shift": float(eff["shift"])}}
        end = ["14", 0]
    wf["13"]["inputs"]["model"] = end
    if "36" in wf:
        wf["36"]["inputs"]["sampler_name"] = eff["sampler"]
    if "38" in wf:
        wf["38"]["inputs"]["cfg"] = 1.0 if turbo else float(eff["cfg"])
    if final:
        for nid in ("37", "53"):
            if (wf.get(nid) or {}).get("class_type") == "BasicScheduler":
                wf[nid]["inputs"]["scheduler"] = eff["scheduler"]
        return wf
    # 主采样 sigma：保留原图的重绘幅度（改图 / 底图），给了 strength 且有底图时改用它
    denoise = _denoise_of(wf.get("37") or {})
    if eff.get("strength") is not None and "112" in wf:
        denoise = float(eff["strength"])
    wf["37"] = _sigma_node(turbo, eff, eff["steps"], denoise)
    # 尺寸（两段式时第一遍用 first_size）
    ts = eff["two_stage"]
    rs = scale_for(wf, eff)
    w, h = (ts["first_size"] if ts["enabled"] else (_r16(eff["width"] * rs), _r16(eff["height"] * rs)))
    dp = eff.get("detail_pass") or {}
    if dp.get("enabled") and not ts["enabled"]:   # 细节重绘 = 放大 ×scale 的低重绘第二遍（finish 再缩回宽×高）
        ts = {"enabled": True, "upscale_size": [_r16(w * float(dp["scale"])), _r16(h * float(dp["scale"]))],
              "denoise": float(dp["denoise"]), "steps": eff["two_stage"]["steps"]}
    for nid, node in wf.items():
        if node.get("class_type") == "EmptyLatentImage":
            node["inputs"].update(width=int(w), height=int(h))
        elif node.get("class_type") == "ImageScale" and node["inputs"].get("crop") == "center" and nid not in ("50",):
            node["inputs"].update(width=int(w), height=int(h))
    if ts["enabled"]:
        uw, uh = ts["upscale_size"]
        wf.update({
            "50": {"class_type": "ImageScale", "inputs": {"image": ["41", 0], "upscale_method": "lanczos",
                                                          "width": int(uw), "height": int(uh), "crop": "disabled"}},
            "51": {"class_type": "VAEEncode", "inputs": {"pixels": ["50", 0], "vae": ["12", 0]}},
            "52": {"class_type": "RandomNoise",
                   "inputs": {"noise_seed": int(wf["35"]["inputs"]["noise_seed"]) + 1}},
            "53": _sigma_node(turbo, eff, ts["steps"], float(ts["denoise"])),
            "54": {"class_type": "SamplerCustomAdvanced",
                   "inputs": {"noise": ["52", 0], "guider": ["38", 0], "sampler": ["36", 0], "sigmas": ["53", 0],
                              "latent_image": ["51", 0]}},
            "55": {"class_type": "VAEDecode", "inputs": {"samples": ["54", 0], "vae": ["12", 0]}},
        })
        wf["90"]["inputs"]["images"] = ["55", 0]
    return wf


def describe(wf: dict | None) -> dict | None:
    """从实际提交的工作流读回参数（与 resolve 的字段同名）"""
    if not wf or not is_image_graph(wf):
        return None
    unet = wf["10"]["inputs"]["unet_name"]
    vae = (wf.get("12") or {}).get("inputs", {}).get("vae_name")
    turbo = "turbo" in unet.lower()
    s = wf.get("37") or {}
    out: dict = {
        "unet": "turbo" if turbo else "base", "unet_file": unet,
        "vae": next((k for k, v in VAES.items() if v == vae), vae), "vae_file": vae,
        "sampler": (wf.get("36") or {}).get("inputs", {}).get("sampler_name"),
        "cfg": float((wf.get("38") or {}).get("inputs", {}).get("cfg", 1.0)),
        "loras": [{"name": f, "strength": st} for _n, f, st in _chain(wf)],
        "shift": (wf.get("14") or {}).get("inputs", {}).get("shift"),
        "denoise": round(_denoise_of(s), 4),
    }
    if s.get("class_type") == "ManualSigmas":
        sig = str(s["inputs"]["sigmas"])
        out.update(sigmas=sig, steps=len(sig.split(",")) - 1, scheduler=None, sigmas_note=TURBO_NOTE if turbo else None)
    else:
        out.update(steps=int(s.get("inputs", {}).get("steps", 0)), scheduler=s.get("inputs", {}).get("scheduler"),
                   sigmas=None)
    lat = next((n for n in wf.values() if n.get("class_type") == "EmptyLatentImage"), None)
    sz = lat["inputs"] if lat else (wf.get("111") or {}).get("inputs") or {}
    out["width"], out["height"] = sz.get("width"), sz.get("height")
    out["init_image"] = "112" in wf
    if "50" in wf and "53" in wf:
        s2 = wf["53"]
        out["two_stage"] = {"enabled": True, "first_size": [out["width"], out["height"]],
                            "upscale_size": [wf["50"]["inputs"]["width"], wf["50"]["inputs"]["height"]],
                            "denoise": round(_denoise_of(s2), 4),
                            "steps": int(s2["inputs"]["steps"]) if s2.get("class_type") == "BasicScheduler"
                            else len(str(s2["inputs"]["sigmas"]).split(",")) - 1}
        out["width"], out["height"] = out["two_stage"]["upscale_size"]
    else:
        out["two_stage"] = {"enabled": False}
    out["refs"] = sum(1 for k in (wf.get("20") or {}).get("inputs", {}) if k.startswith("images.image_"))
    out["prompt"] = (wf.get("20") or {}).get("inputs", {}).get("prompt")   # 实际送进编码器的提示词（含风格后缀）
    return out


# ------------------------------------------------------------------ 系统规格词

def spec_terms(script, c, stage: str) -> list[str]:
    """编译器在作者原文后面自动追加的固定句：镜头句（Leica）、风格后缀、纠正器条目（visual_bible [[shot_rules]]、
    非成人镜头的安全句）。材质条目（指甲 / 鞋等造型细节）不算，保留"""
    from ns_agent.bible import load_bible
    from ns_agent.compiler import corrector_terms, lens_term
    b = load_bible()
    g = script.globals
    style = g.image_suffix or b.scrub(b.image_style_fallback)
    body = (c.image_body or "") + (("，" + (c.end_state or "")) if stage == "last" else "")
    terms = [lens_term(style, b), style, *corrector_terms(body, adult=bool(c.adult), bible=b)]
    return sorted({t.strip("，, ") for t in terms if t and t.strip("，, ")}, key=len, reverse=True)


def strip_spec(text: str, terms: list[str]) -> str:
    out = text
    for t in terms:
        out = out.replace(t, "")
    out = re.sub(r"[，,]\s*(?=[，,])", "", out)
    return re.sub(r"[，,]{2,}", "，", out).strip("，, ")


# ------------------------------------------------------------------ 记录

def _stamp_preset(t: dict, pid: str | None) -> None:
    """UX5-B P0：本版所用预设内容的 rev 记进 take.deps（预设内容之后被改 → “依赖已更新”）；信息用，失败静默"""
    try:
        from ns_agent.asset_revs import preset_rev
        deps = {k: v for k, v in (t.get("deps") or {}).items() if not k.startswith("preset:")}
        rev = preset_rev(P.ROOT, pid) if pid else None
        if rev:
            deps[f"preset:{pid}"] = rev
        t["deps"] = deps or None
    except Exception:  # noqa: BLE001
        pass


def summary(eff: dict | None, graph: dict | None, take: dict) -> dict:
    """take.params.effective：实际生效的参数（以提交的工作流为准；调色、种子以这一版为准）"""
    g = dict(graph or {})
    gr = take.get("grade")
    out = {
        "tier": (eff or {}).get("tier") or ("final" if take.get("mode") == "final" else "preview"),
        **{k: g.get(k) for k in ("unet", "unet_file", "vae", "vae_file", "steps", "cfg", "sampler", "scheduler",
                                 "sigmas", "shift", "loras", "width", "height", "two_stage", "denoise", "refs")},
        "seed": take.get("seed"),
        "strength": g.get("denoise") if g.get("init_image") else None,
        "grade": {"enabled": bool(gr), "strength": (gr or {}).get("strength")},
        "spec_words": (eff or {}).get("spec_words", True),
        "sharpen": {**DEFAULTS["sharpen"], **((eff or {}).get("sharpen") or {})},
        "render_scale": (eff or {}).get("render_scale", 1.0),
        "detail_pass": (eff or {}).get("detail_pass") or DEFAULTS["detail_pass"],
    }
    # LCA：放大出图在带底图的改图上不生效（scale_for）；记实际值
    if g.get("init_image") and float(out["render_scale"] or 1.0) != 1.0 and out["tier"] != "final":
        out["render_scale_requested"] = out["render_scale"]
        out["render_scale"] = 1.0
        out["render_scale_note"] = "带底图的改图按交付尺寸出，不放大（底图就是交付尺寸的父版本）"
    suf = (eff or {}).get("style_suffix") or ""
    sent = g.get("prompt")
    out["style_suffix"] = suf or None
    out["style_suffix_applied"] = bool(suf and isinstance(sent, str) and sent.endswith(suf))
    if (eff or {}).get("detail_pass", {}).get("enabled") and g.get("two_stage", {}).get("enabled"):
        out["two_stage"] = {"enabled": False, "as_detail_pass": g["two_stage"]}
    if g.get("sigmas_note"):
        out["note"] = g["sigmas_note"]
    return out


def _dark(shot_id: str) -> bool:
    try:
        return bool(json.loads((P.shot_dir(shot_id) / "compiled.json").read_text(encoding="utf-8")).get("dark"))
    except (OSError, ValueError):
        return False


def postprocess(rel: str, eff: dict | None) -> dict | None:
    """出图后（调色之后）：缩回目标尺寸（render_scale / 细节重绘，lanczos）→ USM 锐化。另存 *_post.png，原图保留"""
    post = post_file(P.DATA / rel, eff)
    if not post:
        return None
    stem = Path(rel).with_suffix("").as_posix()
    return {**{k: v for k, v in post.items() if k != "path"}, "file": f"{stem}_post.png", "from": rel}


def post_effective(eff: dict | None, graph: dict | None, post: dict | None) -> dict:
    """资产 / 场景图生成（asset_edit.run_gen）的 effective 补充：锐化、放大出图、风格后缀的实际值（字段同 summary）"""
    s = summary(eff, graph, {})
    out = {k: s[k] for k in ("sharpen", "render_scale", "style_suffix", "style_suffix_applied") if k in s}
    for k in ("render_scale_requested", "render_scale_note"):
        if k in s:
            out[k] = s[k]
    if post:
        out.update(width=post["size"][0], height=post["size"][1], rendered_size=post["rendered_size"],
                   post_steps=post["steps"])
    return out


def post_film_cfg(path: Path | None = None) -> dict | None:
    """H2a 胶片后期档配置（config/render_quality.json post_film）；没开 → None"""
    try:
        c = quality(path).get("post_film") or {}
    except (OSError, ValueError):
        return None
    return c if c.get("enabled") else None


def post_file(src: Path, eff: dict | None, film_cfg: dict | None = None, dst: Path | None = None) -> dict | None:
    """缩回 + 锐化（同 tools/ab/leica_sharp.py down / usm：PIL lanczos → PIL UnsharpMask），写 <stem>_post.png。
    H2a：post_film.enabled=true 时 缩回 →（usm=true 才 USM）→ 胶片调色 →（LUT）；false 时与以前逐字节一致。
    film_cfg：显式传入则用它（None = 读配置文件）。dst：另存路径（重后期用；缺省 <stem>_post.png）。
    不需要后处理 → None。返回 {path, steps, rendered_size, size, unsharp[, film]}"""
    if film_cfg is None:
        film_cfg = post_film_cfg()
    elif not film_cfg.get("enabled"):
        film_cfg = None
    size = target_size(eff)
    sh = (eff or {}).get("sharpen") or {}
    if film_cfg:
        sh = sh if film_cfg.get("usm") else {}
    if not size and not sh.get("enabled") and not film_cfg:
        return None
    from PIL import Image, ImageFilter
    src = Path(src)
    im = Image.open(src).convert("RGB")
    steps, before = [], list(im.size)
    if size and tuple(im.size) != size:
        im = im.resize(size, Image.LANCZOS)
        steps.append(f"lanczos {before[0]}×{before[1]} → {size[0]}×{size[1]}")
    usm = None
    if sh.get("enabled"):
        usm = {"radius": float(sh["radius"]), "percent": int(round(float(sh["amount"]) * 100)),
               "threshold": int(sh.get("threshold") or 0)}
        im = im.filter(ImageFilter.UnsharpMask(**usm))
        steps.append(f"USM radius {usm['radius']} percent {usm['percent']} threshold {usm['threshold']}")
    film = None
    if film_cfg:
        from . import film_look as FL
        lut = film_cfg.get("lut") or {}
        lut_path = None
        if lut.get("apply"):
            lut_path = Path(lut["file"]) if lut.get("file") else None
            if lut_path and not lut_path.is_absolute():
                lut_path = P.ROOT / lut_path
        im, film = FL.grade_image(im, film_cfg.get("film") or {}, lut, lut_path)
        film["usm"] = bool(usm)
        steps.append("film " + FL.VERSION + (f" + LUT {film['lut']['file']}" if film.get("lut") else ""))
    dst = Path(dst) if dst else src.with_name(src.stem + "_post.png")
    im.save(dst)
    out = {"path": str(dst), "steps": steps, "rendered_size": before, "size": list(im.size), "unsharp": usm}
    if film:
        out["film"] = film
    return out


def finish(shot_id: str, stage: str, n: int, eff: dict | None, wf: dict | None, *, overrides: dict | None = None,
           spec_removed: list[str] | None = None) -> dict:
    """出图任务完成后（预览档 / 改图）：按参数调色（开 / 关 / 强度），把有效参数记进 take.params。
    定稿档的调色由 final_tier 按 cfg 做，这里只记录。返回更新后的 take dict"""
    from . import grade as G
    data = P.load_takes(shot_id)
    t = next(x for x in data["takes"] if x["stage"] == stage and x["n"] == n)
    g = (eff or {}).get("grade") or {}
    changed: dict = {}
    if eff and eff.get("tier") != "final" and g.get("enabled") is not None:
        cur = t.get("grade")
        if g["enabled"] is False and cur and t.get("raw_file"):
            changed = {"file": t["raw_file"], "raw_file": None, "grade": None}
        elif g["enabled"] and not (cur and abs(float(cur.get("strength", -1)) - float(g["strength"])) < 1e-6):
            src_rel = t.get("raw_file") or t["file"]
            stem = Path(src_rel).with_suffix("").as_posix()
            stem = stem[:-4] if stem.endswith("_raw") else stem
            dst_rel = f"{stem}_g{round(float(g['strength']) * 100)}.png"
            ref = P.shot_dir(shot_id) / "frame.png" if stage == "last" else None
            gr = G.grade_file(P.DATA / src_rel, P.DATA / dst_rel, reference=ref if ref and ref.exists() else None,
                              strength=float(g["strength"]), dark=_dark(shot_id))
            changed = {"file": dst_rel, "raw_file": src_rel, "grade": gr}
    post = postprocess(changed.get("file") or t["file"], eff)
    if post:
        changed["file"] = post["file"]
    graph = describe(wf)
    with P.shot_lock(shot_id):
        data = P.load_takes(shot_id)
        t = next(x for x in data["takes"] if x["stage"] == stage and x["n"] == n)
        t.update(changed)
        params = dict(t.get("params") or {})
        if post:
            params["post"] = post
        params["preset"] = (eff or {}).get("preset")
        params["overrides"] = overrides or None
        params["requested"] = eff
        params["effective"] = summary(eff, graph, t)
        if post:
            params["effective"].update(width=post["size"][0], height=post["size"][1],
                                       rendered_size=post["rendered_size"])
        if spec_removed:
            params["spec_removed"] = spec_removed
        if (graph or {}).get("prompt") is not None and params["effective"].get("style_suffix"):
            params["prompt_sent"] = graph["prompt"]       # LCA：送进 Image 2.1 的提示词（作者原文 + “，” + 风格后缀）
        t["params"] = params
        _stamp_preset(t, params.get("preset"))
        P._save_takes(shot_id, data)
        if changed and (data.get("active") or {}).get(stage) == n:
            P.set_active(shot_id, stage, n)
        return next(x for x in P.load_takes(shot_id)["takes"] if x["stage"] == stage and x["n"] == n)
