"""
v1 视频（api_v1_contract §D，fredw-92/W3）：定稿首帧 + 定稿尾帧 → gpu3 GPU3 ComfyUI 的 MiniMax H3 FL2VA。

- 图与 gpu3 生产渲染器一致（weita_render.h3_graph + data/run/weita/PIPELINE_LOCKED.json，2026-10-30 核对）：
  DiT minimax_h3_fl2va_pruned_int8_convrot、Turbo LoRA 8step v1.1 768p（强度 1.0）、shift 6/3（采样与调度都过）、
  euler/simple、8 步、768×1344、24 fps、首尾帧 center-crop + lanczos；文本编码器 qwen3vl_32b_minimax_h3_nvfp4_awq、
  视频 VAE int8_convrot、音频 VAE fp32（gpu3 :8191 object_info 均在列）。
- 提示词：请求 prompt_override > 项目方覆盖（edits.json h3_prompt_override，未过期）> 剧本 **H3提示词**（编译器
  resolve_music 已解析配乐）。原样传；只有时长改了时把系统生成的第一行对齐句按新帧数重算（结构行，不是内容）。
- 帧数：秒数 → ns_schema.h3_lint.frames_for_seconds（17k+5，124–362）。
- 本模块不做内容检测 / 成人拦截。
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import replace
from pathlib import Path

from ns_schema.edits import Take
from ns_schema import h3_lint as _L
from ns_schema.h3_lint import ALIGN_PREFIX, alignment_line, frames_for_seconds

from . import pipeline as P
from . import render_paths as RP   # W33
from . import workflows as W
from . import comfy_client as CC
from . import motion_qc as MQ
from .comfy_client import ComfyError

# fredw-92/LS（取代 D1 的 192 帧上限）：项目方“长镜头长度模型决定，不限定时长”——帧数一律按剧本时长
# （frames_for_seconds，最长 362），系统不截短。每条道一次能出多少帧记在 config/render_endpoints.json
# （ns_render.endpoints.max_frames：gpu33 的 3090 + --reserve-vram 1 实测 243 过、277/294 OOM；
# gpu36:2 长镜头道 --novram 实测见 docs/e2e_iteration_log.md）；超过本道上限的镜头在 video_jobs 改派到长镜头道。


def frames_for(seconds: float) -> tuple[int, int]:
    """(出的帧数, 剧本时长要的帧数)——两者相同（不截短；保留二元组是为了调用方兼容）"""
    want = frames_for_seconds(seconds)
    return want, want


def lane_max_frames(gpu) -> int:
    from . import endpoints as EP
    return EP.max_frames(gpu)


def timeout_for(frames: int, steps: int | None = None) -> float:
    """长镜头在 --novram 道上慢（每步约 2.7 min @294 帧）：按帧数放宽等待上限。
    RR 2026-10-31：多步基底（official_base 24 步，3090 每镜 192 帧约 41 min，常排在别人的任务后）按步数再放宽：
    steps > 12 时 × steps / 12（24 步 → ×2）"""
    t = max(TIMEOUT_S, round(TIMEOUT_S * (frames / 192) ** 2))
    if steps and steps > 12:
        t = round(t * steps / 12)
    return float(t)


DEFAULT_STEPS = W.H3Model().steps
DEFAULT_LORA_STRENGTH = dict(W.H3Model().loras)[W.H3_TURBO_8STEP_768P]
TIMEOUT_S = 3600

# fredw-92/VB：请求附加的视频 LoRA（串在 Turbo LoRA 和剧本视频 LoRA 之后）。强度范围、可选清单在 config/render_quality.json
EXTRA_LORA_MIN, EXTRA_LORA_MAX = 0.0, 1.5


def optional_video_loras(path: Path | None = None) -> list[dict]:
    """config/render_quality.json 的 optional_video_loras（画布“附加视频 LoRA”下拉用）；读不到 → []"""
    import json
    try:
        q = json.loads(Path(path or (P.ROOT / "config" / "render_quality.json")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [dict(x) for x in (q.get("optional_video_loras") or []) if isinstance(x, dict) and x.get("name")]


# fredw-92/PF：视频基底（config/render_quality.json video_bases）。读不到配置时用内置的官方现链，保证默认行为不变
DEFAULT_BASE = "official_turbo"
_BUILTIN_BASES = [{"id": DEFAULT_BASE, "label": "官方 8 步（当前）", "short": "官方8步", "lora": W.H3_TURBO_8STEP_768P,
                   "lora_strength": 1.0, "steps": 8, "sampler": "euler", "scheduler": "simple", "sigmas": None,
                   "shift": list(W.SHIFT_768P_TURBO), "shift_node": True, "cfg": 1.0, "guider": "BasicGuider",
                   "steps_adjustable": True}]


def video_bases(path: Path | None = None) -> dict:
    """{"default": id, "bases": [...]}；配置里没有 official_turbo 也补上内置的"""
    import json
    try:
        q = json.loads(Path(path or (P.ROOT / "config" / "render_quality.json")).read_text(encoding="utf-8"))
        vb = q.get("video_bases") or {}
    except (OSError, ValueError):
        vb = {}
    # MOT：lora 为 null 且 no_lora: true 的基底 = 不挂蒸馏 LoRA 的基模（官方 CFG 蒸馏权重，多步）
    bases = [dict(x) for x in (vb.get("bases") or []) if isinstance(x, dict) and x.get("id")
             and (x.get("lora") or x.get("no_lora"))]
    if not any(b["id"] == DEFAULT_BASE for b in bases):
        bases.insert(0, dict(_BUILTIN_BASES[0]))
    default = vb.get("default") if any(b["id"] == vb.get("default") for b in bases) else DEFAULT_BASE
    return {"default": default, "bases": bases}


def base_spec(base_id: str | None = None) -> dict:
    """按 id 取基底；None → 配置的默认。没有这个 id → KeyError"""
    vb = video_bases()
    bid = base_id or vb["default"]
    for b in vb["bases"]:
        if b["id"] == bid:
            return b
    raise KeyError(bid)


def base_model(spec: dict, steps: int | None = None, lora_strength: float | None = None) -> W.H3Model:
    """基底 → H3Model。可调步数 / 强度的基底（官方 Turbo）用请求值，固定的（Pika Flex）一律用配置值"""
    adj = bool(spec.get("steps_adjustable"))
    st = int(steps) if (adj and steps) else int(spec["steps"])
    ls = float(lora_strength) if (adj and lora_strength is not None) else float(spec.get("lora_strength") or 1.0)
    loras = ((spec["lora"], ls),) if spec.get("lora") else ()
    return replace(W.H3Model(), steps=st, loras=loras, sampler=spec.get("sampler") or "euler",
                   scheduler=spec.get("scheduler") or "simple", sigmas=spec.get("sigmas") or None,
                   shift=tuple(float(x) for x in (spec.get("shift") or W.SHIFT_BASE)))


def merge_loras(script_loras, extra) -> list[tuple[str, float]]:
    """剧本视频 LoRA + 请求附加 LoRA；同名时以请求的强度为准（不重复串两次），顺序：剧本在前、附加在后"""
    extra = [(str(n), float(s)) for n, s in (extra or [])]
    names = {n for n, _ in extra}
    return [(n, float(s)) for n, s in script_loras if n not in names] + extra


class MissingVideoLora(ComfyError):
    """FIX（ep02_iter P0，C2/E3）：本镜按在画角色需要的视频 LoRA 不在这条道上 → 不出（以前是编译时悄悄去掉）"""

    def __init__(self, missing: list[str], lane):
        super().__init__(f"本镜需要视频 LoRA {', '.join(missing)}（按在画角色确定），道 {lane} 上没有；"
                         "不在缺 LoRA 的道上出片（不静默去掉）")
        self.missing, self.lane = missing, lane


def required_video_loras(script, shot_id: str) -> list[tuple[str, float]]:
    """本镜视频 LoRA（编译器按剧本触发词 + 在画角色确定，与道无关）"""
    return list(P.compile_with_edits(script, shot_id, set()).video_loras)


def missing_video_loras(required, avail) -> list[str]:
    return [n for n, _s in required or [] if n not in set(avail or ())]


class FramesNotLocked(ValueError):
    """首尾帧没定稿；missing = ["first"] / ["last"] / 两个都有"""

    def __init__(self, missing: list[str], detail: str):
        super().__init__(detail)
        self.missing = missing


_SHA_CACHE: dict[tuple, str] = {}


def file_sha(path: Path) -> str:
    """文件内容 sha256 前 16 位（按 路径 + mtime + 大小 缓存：集摘要每次都要比对）"""
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key not in _SHA_CACHE:
        _SHA_CACHE[key] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return _SHA_CACHE[key]


def locked_n(takes: dict, stage: str) -> int | None:
    """定稿版本号：takes.json 顶层 locked[stage]（W2）或 take.locked 为真（兼容）"""
    lk = takes.get("locked") or {}
    keys = ("image", "first") if stage == "image" else (stage,)
    for k in keys:
        if isinstance(lk, dict) and lk.get(k) is not None:
            return int(lk[k])
    t = next((t for t in reversed(takes.get("takes", [])) if t.get("stage") == stage and t.get("locked")), None)
    return t["n"] if t else None


def locked_frames(shot_id: str) -> dict:
    """{"first": {n, file, sha}, "last": {...}}；缺哪个就抛 FramesNotLocked 写明"""
    takes = P.load_takes(shot_id)
    out, missing, why = {}, [], []
    for key, stage, label in (("first", "image", "首帧"), ("last", "last", "尾帧")):
        n = locked_n(takes, stage)
        t = next((t for t in takes["takes"] if t["stage"] == stage and t["n"] == n), None) if n is not None else None
        if t is None:
            missing.append(key)
            why.append(f"{label}未定稿")
            continue
        p = P.DATA / t["file"]
        if not p.is_file():
            missing.append(key)
            why.append(f"{label}定稿文件不存在（{t['file']}）")
            continue
        out[key] = {"n": n, "file": t["file"], "sha": file_sha(p)}
    if missing:
        raise FramesNotLocked(missing, "；".join(why) + "（出视频要求首帧、尾帧都已定稿）")
    return out


UPLOAD_TIMEOUT_S = 900   # 笔记本上行慢（实测 8–25 KB/s 时 3 MB PNG 超过 ComfyClient 默认 60 s）


def keyframe_file(path: Path, size=None) -> Path:
    """首尾帧预先 center-crop + lanczos 到视频画布（与图里 ImageScale crop=center 相同，图里那步变成原尺寸直通），
    文件小一半，上传快；按内容 sha 缓存在 data/renders/_h3_keyframes/"""
    from PIL import Image
    w, h = size or P.VIDEO_SIZE
    out = P.RENDERS / "_h3_keyframes" / f"{file_sha(path)}_{w}x{h}.png"
    if out.is_file():
        return out
    im = Image.open(path).convert("RGB")
    sw, sh = im.size
    scale = max(w / sw, h / sh)
    rw, rh = max(w, round(sw * scale)), max(h, round(sh * scale))
    im = im.resize((rw, rh), Image.LANCZOS)
    left, top = (rw - w) // 2, (rh - h) // 2
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.png")
    im.crop((left, top, left + w, top + h)).save(tmp, optimize=True)
    tmp.replace(out)
    return out


def upload_once(client, path: Path, subfolder: str = "ns_final", size=None) -> str:
    """首尾帧 → LoadImage 名。真 ComfyClient（fredw-92/F1）：path 是 gpu3 本实例的输出原样 → [output] 引用 +
    图内 center-crop 缩放，不上传；否则上传 keyframe_file（JPEG q95、输入目录已有同内容跳过、≥900 s）。
    假客户端：ComfyUI 输入目录里已有同内容文件（upload_image 按 sha 命名）就不再上传"""
    if hasattr(client, "input_image"):
        return client.input_image(path, subfolder, size=tuple(size or P.VIDEO_SIZE), crop="center",
                                  prepare=lambda: keyframe_file(path, size))
    path = keyframe_file(path, size)
    name = hashlib.sha256(path.read_bytes()).hexdigest()[:16] + path.suffix.lower()
    try:
        client._request(f"/view?filename={name}&subfolder={subfolder}&type=input")
        return f"{subfolder}/{name}"
    except Exception:  # noqa: BLE001  没有 / 查不到 → 上传
        pass
    old = getattr(client, "timeout", None)
    if old is not None:
        client.timeout = max(old, UPLOAD_TIMEOUT_S)
    try:
        return client.upload_image(path, subfolder)
    finally:
        if old is not None:
            client.timeout = old


def resolve_prompt(script, shot_id: str, prompt_override: str | None, loras=frozenset()) -> tuple[str, str, object]:
    """→ (提示词原文, 来源 request|owner_edit|script|laptop, compiled)"""
    c = P.compile_with_edits(script, shot_id, set(loras))
    if prompt_override and prompt_override.strip():
        return prompt_override, "request", c
    edit = P.load_edit(shot_id)
    ov = edit.h3_prompt_override
    if ov and ov.text.strip():
        _, s = P.find_shot(script, shot_id)
        stale = ov.script_sha != (hashlib.sha256(s.h3_prompt.encode("utf-8")).hexdigest()
                                  if getattr(s, "h3_prompt", None) else None)
        if not stale:
            return ov.text, "owner_edit", c
    return c.video_prompt, c.h3_prompt_source or "laptop", c


def realign(prompt: str, frames: int) -> str:
    """只换第一行系统对齐句（秒数随帧数变）；不是 H3 结构就原样返回"""
    head, sep, rest = prompt.partition("\n")
    if not head.startswith(ALIGN_PREFIX):
        return prompt
    return alignment_line(frames) + sep + rest


# fredw-92/QCV：只给首帧（I2VA）时的对齐句——官方提示词指南 docs/h3_prompt_guide_base_en.txt §2.1 原文（逐字，含尖括号 /
# 方括号）。对齐句是系统生成的结构行，必须和实际给的图数一致；正文原样不动。
I2V_ALIGN_LINE = _L.I2V_ALIGN_LINE   # FIX：唯一来源 ns_schema.h3_lint（CHAIN F2）


def align_first_only(prompt: str) -> str:
    """FL2VA 对齐句 → I2VA 对齐句（只换第一行）；第一行不是系统对齐句就原样返回"""
    head, sep, rest = prompt.partition("\n")
    if head.startswith(ALIGN_PREFIX) or head == I2V_ALIGN_LINE:
        return I2V_ALIGN_LINE + sep + rest
    return prompt


# FIX（ep02_iter P0 ③，chain_audit C5）：只给首帧时正文里引用 Picture 2 的句子（“settles into … established by
# Picture 2 at the end of the shot.”）指向一张不存在的图（官方指南 §3.1：I2VA = 首帧锚定 → 动作起始 → 持续发展）。
# 发送前去掉 <d> 外引用 Picture 2 的分句（同一句前面的动作保留）/ 整句；<d> 内的台词一字不动。
_SETTLE_RE = re.compile(r"(?:,?\s*\band\s+|,\s*|(?<![A-Za-z]))(?:(?:she|he|they|it|the subject|the woman|the man)\s+)?"
                        r"settles?\s+into[^.!?\n]*?Picture 2[^.!?\n]*", re.I)
_SENT_RE = re.compile(r"[^.!?\n]*?Picture 2[^.!?\n]*[.!?]?")


def strip_picture2(body: str) -> tuple[str, list[str]]:
    """去掉 <d>…</d> 之外含 “Picture 2” 的句子 → (新正文, 去掉的句子)"""
    removed: list[str] = []
    parts = re.split(r"(<d>.*?</d>)", body, flags=re.S)
    for i in range(0, len(parts), 2):          # 偶数段在 <d> 外
        seg = parts[i]
        if "Picture 2" not in seg:
            continue

        def cut(m):
            removed.append(m.group(0).strip())
            return ""
        # 先只去掉“(, and) <主语> settles into … Picture 2 …”分句，同一句里前面的动作（剧本内容）保留
        seg = _SETTLE_RE.sub(cut, seg)
        seg = re.sub(r"(^|[.!?:]\s+|\n)\s*[.!?]", r"\1", seg)   # 整句被去掉后剩下的孤立句号
        seg = _SENT_RE.sub(cut, seg)                                     # 其他引用 Picture 2 的句子整句去掉
        parts[i] = re.sub(r"[ \t]{2,}", " ", seg)
    out = "".join(parts)
    out = re.sub(r"[ \t]+\n", "\n", out)
    return out, removed


def i2v_prompt(prompt: str) -> tuple[str, list[str]]:
    """只给首帧（I2VA）发送用的提示词：对齐句换 I2VA 句 + 正文去掉 Picture 2 句。不是 H3 结构 → 原样"""
    one = align_first_only(prompt)
    if one == prompt and not prompt.startswith(I2V_ALIGN_LINE):
        return prompt, []
    head, sep, rest = one.partition("\n")
    rest, removed = strip_picture2(rest)
    # CHAIN F1（官方 base-en §3.1 / Case 2）：I2VA 正文里首帧写作 <Picture 1>
    rest = re.sub(r"(?<!<)\bPicture 1\b(?!>)", "<Picture 1>", rest)
    return head + sep + rest, removed


# FIX（CHAIN F3）：提交前按实际模式跑 lint_h3，block 级不出片（以前只在剧本导入时跑）。
# 项目方原话改过的提示词（owner_edit）按 gpu3 S1-W9 豁免，不在这里拦
LINT_EXEMPT_SOURCES = ("owner_edit",)


class PromptLintBlocked(ComfyError):
    def __init__(self, failed: list[str], details: list[str]):
        super().__init__("H3 提示词没过官方规范检查（block）：" + "；".join(f"{r} {d}" for r, d in zip(failed, details)))
        self.failed, self.details = failed, details


def final_prompt(prompt: str, frames: int, use_last: bool) -> tuple[str, list[str]]:
    """实际送进 H3 的提示词：对齐句按帧数重算；只给首帧时换成 I2VA 正文"""
    prompt = realign(prompt, frames)
    return i2v_prompt(prompt) if not use_last else (prompt, [])


def submit_lint(prompt: str, frames: int, use_last: bool) -> tuple[list[str], list[str]]:
    """→ (block 规则 id, 中文原因)；只查 H3 结构的提示词（自由文本 = 旧链路，不查）"""
    head = (prompt or "").split("\n", 1)[0]
    if not (head.startswith(ALIGN_PREFIX) or head == I2V_ALIGN_LINE):
        return [], []
    res = _L.lint_h3(prompt, frames, mode="fl2va" if use_last else "i2va")
    bad = [r for r in res if not r["ok"] and r.get("level") == "block"]
    return [r["rule_id"] for r in bad], [r["detail"] for r in bad]


LAST_FRAME_MODES = ("use", "none", "auto")


def keyframe_precheck(frames_in: dict) -> dict:
    """出片前：定稿首帧 ↔ 定稿尾帧差（motion_qc.keyframe_check）；不拦，只给提示"""
    f, la = (frames_in.get("first") or {}).get("file"), (frames_in.get("last") or {}).get("file")
    if not f or not la:
        return {"warn": False, "error": "首尾帧文件未知，未做首尾帧差检查"}
    return MQ.keyframe_check(P.DATA / f, P.DATA / la)


def default_last_frame_mode(spec: dict | None = None, requested: str | None = None) -> str:
    """请求写了 last_frame_mode 就照请求；没写 → 基底的 default_last_frame_mode（RR 2026-10-31：默认基底
    official_base = "none" 只给首帧；official_turbo = "use"）；基底也没写 → "use"（原行为）"""
    if requested:
        return requested
    spec = spec if spec is not None else base_spec(None)
    return (spec or {}).get("default_last_frame_mode") or "use"


def use_last_frame(mode: str | None, pre: dict | None) -> bool:
    mode = (mode or "use").lower()
    if mode not in LAST_FRAME_MODES:
        raise ValueError(f"last_frame_mode 只能是 {LAST_FRAME_MODES}")
    if mode == "none":
        return False
    if mode == "auto":
        return not (pre or {}).get("warn")
    return True


def shot_seconds(script, shot_id: str) -> float:
    _, s = P.find_shot(script, shot_id)
    return float(getattr(s, "duration", None) or 10)


def build(prompt: str, first: str, last: str, frames: int, seed: int, prefix: str, *, steps: int | None = None,
          lora_strength: float = DEFAULT_LORA_STRENGTH, extra_loras=(), base: str | dict | None = None) -> dict:
    """steps=None → 基底自己的步数（官方 Turbo 8；official_base 25；MOT：原来默认 8 会把可调基底也压成 8 步）"""
    if base is None or base == DEFAULT_BASE:
        m = replace(W.H3Model(), steps=int(steps or DEFAULT_STEPS),
                    loras=((W.H3_TURBO_8STEP_768P, float(lora_strength)),))
    else:   # fredw-92/PF：其他基底（Pika Flex 等）
        spec = base if isinstance(base, dict) else base_spec(base)
        m = base_model(spec, steps, lora_strength)
    return W.h3_fl2va(prompt, first, *P.VIDEO_SIZE, frames, seed, prefix, m=m, loras=list(extra_loras),
                      last_frame=last)


class VramSampler:
    """出视频期间每 2 s 读一次 ComfyUI /system_stats，记显存峰值（GB）"""

    def __init__(self, client, every_s: float = 2.0):
        self.client, self.every, self.peak, self.total = client, every_s, None, None
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            try:
                d = (self.client.system_stats().get("devices") or [{}])[0]
                used = (d["vram_total"] - d["vram_free"]) / 2**30
                self.total = round(d["vram_total"] / 2**30, 1)
                self.peak = round(max(self.peak or 0, used), 2)
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(self.every)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()


def render(client, script, shot_id: str, *, seed: int | None = None, steps: int | None = None,
           seconds: float | None = None, lora_strength: float | None = None, prompt_override: str | None = None,
           parent: int | None = None, job: dict | None = None, gpu: int = 3, timeout_s: float = TIMEOUT_S,
           wait=None, extra_loras=None, base: str | None = None, last_frame_mode: str | None = None) -> Take:
    """出一版视频并存为 take（stage=video，带 params / parent），设为当前。wait(client, job, pid, timeout) 可取消。
    last_frame_mode（fredw-92/QCV）：use 首尾帧都给；none 只给首帧（I2VA 对齐句）；auto 首尾帧几乎一样
    （motion_qc.keyframe_warn）时只给首帧，否则都给。None（RR 2026-10-31）= 基底自己的 default_last_frame_mode
    （默认基底 official_base = none；official_turbo = use）。出片后算运动量写 take.qc.motion"""
    frames_in = locked_frames(shot_id)
    pre = keyframe_precheck(frames_in)
    spec = base_spec(base)
    bid = spec["id"]
    last_frame_mode = default_last_frame_mode(spec, last_frame_mode)
    use_last = use_last_frame(last_frame_mode, pre)
    avail = P.available_loras(client)
    if spec.get("lora") and spec["lora"] not in avail:   # fredw-92/PF：基底 LoRA 必须在这条道上（无 LoRA 基底跳过）
        raise ComfyError(f"基底 {bid} 的 LoRA 不在 GPU{gpu} 的 LoRA 列表里：{spec['lora']}")
    prompt, src, c = resolve_prompt(script, shot_id, prompt_override, avail)
    req_loras = [(str(n), float(st)) for n, st in (extra_loras or [])]
    missing = [n for n, _ in req_loras if n not in avail]
    if missing:   # 提交时校验过；这里防止排队期间道上的 LoRA 列表变了
        raise ComfyError(f"附加 LoRA 不在 GPU{gpu} 的 LoRA 列表里：{', '.join(missing)}")
    miss = missing_video_loras(c.video_loras, avail)   # FIX P0：身份 LoRA 按角色确定，道上缺 → 拒绝，不静默不挂
    if miss:
        raise MissingVideoLora(miss, gpu)
    vloras = merge_loras(c.video_loras, req_loras)
    secs = float(seconds) if seconds else shot_seconds(script, shot_id)
    frames, frames_want = frames_for(secs)
    # RR 2026-10-31：无 LoRA 基底在长镜头道（--novram，294 帧）上 int8_linear 显存不够（ep02-S8 两次 OOM）；
    # 挂一个强度极小的 LoRA 让 ComfyUI 走“LoRA 补丁权重”那条路（和 Turbo 一样，能出），数值上可忽略。配置见基底 long_patch_lora
    lp = spec.get("long_patch_lora") or {}
    if not spec.get("lora") and not vloras and lp.get("name") and frames >= int(lp.get("min_frames") or 10**9) \
            and lp["name"] in avail:
        vloras = [(lp["name"], float(lp.get("strength") or 0.0001))]
    lane_max = lane_max_frames(gpu)
    if frames > lane_max:   # video_jobs 已按帧数改派；到这里说明道配置变了 → 明确报错，不截短
        raise ComfyError(f"{frames} 帧超过道 {gpu} 实测上限 {lane_max} 帧（不截短）；请派到长镜头道"
                         f"（config/render_endpoints.json 的 max_frames）")
    timeout_s = max(float(timeout_s), timeout_for(frames, steps if bid == DEFAULT_BASE else base_model(spec, steps).steps))
    # 只给首帧：对齐句换 I2VA 句；FIX P0 ③：正文里引用 Picture 2 的句子去掉（不存在的图）
    prompt, i2v_removed = final_prompt(prompt, frames, use_last)
    if src not in LINT_EXEMPT_SOURCES:
        failed, why = submit_lint(prompt, frames, use_last)
        if failed:
            raise PromptLintBlocked(failed, why)
    if bid == DEFAULT_BASE:
        steps = int(steps or DEFAULT_STEPS)
        ls = DEFAULT_LORA_STRENGTH if lora_strength is None else float(lora_strength)
    else:
        bm = base_model(spec, steps, lora_strength)
        steps, ls = bm.steps, (dict(bm.loras)[spec["lora"]] if spec.get("lora") else None)
    with P.shot_lock(shot_id):
        n = P._next_n(shot_id, "video")
    s = int(seed) if seed is not None else P.seed_for(shot_id + "/v", n)
    d = P.shot_dir(shot_id)
    t_up = time.monotonic()
    first = upload_once(client, P.DATA / frames_in["first"]["file"])
    last = upload_once(client, P.DATA / frames_in["last"]["file"]) if use_last else None
    upload_s = round(time.monotonic() - t_up, 1)
    wf = build(prompt, first, last, frames, s, f"ns_final/{shot_id}_video", steps=steps, lora_strength=ls,
               extra_loras=vloras, base=None if bid == DEFAULT_BASE else spec)
    problems = client.check(wf)
    if problems:
        raise ComfyError("工作流检查不通过：" + "；".join(problems))
    t0 = time.monotonic()
    with VramSampler(client) as vram:
        pid = client.submit(wf)
        if job is not None:
            job["prompt_id"] = pid
        entry = wait(client, job, pid, timeout_s) if wait else client.wait(pid, timeout_s)
    secs_run = round(time.monotonic() - t0, 1)
    item = next(f for f in client.output_files(entry) if f["filename"].endswith(".mp4"))
    # 文件名带任务 id：两条道同时出同一镜视频时 n 可能相同，不能互相覆盖（F3 审计；_add_take 会顺延 n）
    from .frame_edit import unique_tag
    base = f"{RP.take_rel_dir(d, P.DATA, P.RENDERS)}/video_{n:03d}_{unique_tag(job)}"   # W33
    for i in range(3):   # 隧道抖动时重试下载（prompt 已跑完，不重渲）
        try:
            client.download(item, P.DATA / f"{base}.mp4")
            break
        except ComfyError:
            if i == 2:
                raise
            time.sleep(5)
    lf = P.extract_last_frame(P.DATA / f"{base}.mp4", P.DATA / f"{base}_last.png")
    motion = MQ.video_motion(P.DATA / f"{base}.mp4")   # fredw-92/QCV：出片后运动量质检（失败不影响存版本）
    m = W.H3Model() if bid == DEFAULT_BASE else base_model(spec)
    if bid == DEFAULT_BASE:
        shift = list(W.expected_shift([(W.H3_TURBO_8STEP_768P, ls), *vloras]))
    else:
        shift = [float(x) for x in spec.get("shift") or []]
    base_rec = {"id": bid, "label": spec.get("label"), "lora": spec.get("lora"), "lora_strength": ls, "steps": steps,
                "sampler": m.sampler, "scheduler": None if m.sigmas else m.scheduler, "sigmas": m.sigmas,
                "shift": shift, "shift_node": not m.sigmas, "cfg": spec.get("cfg", 1.0),
                "guider": spec.get("guider", "BasicGuider"), "source_url": spec.get("source_url"),
                "license": spec.get("license"), "sha256": spec.get("sha256"),
                "experimental": bool(spec.get("experimental"))}
    params = {"base": bid, "base_params": base_rec,
              "seed": s, "steps": steps, "seconds": secs, "frames": frames, "lora_strength": ls,
              "frames_requested": frames_want, "frames_capped": frames < frames_want,
              "method": "single", "lane_max_frames": lane_max,
              "prompt_source": src, "prompt_override": prompt_override or None,
              "first": frames_in["first"], "last": frames_in["last"] if use_last else None, "gpu": gpu,
              "last_frame_mode": last_frame_mode, "last_frame_used": use_last,
              "last_unused": None if use_last else frames_in["last"], "keyframe_check": pre,
              "i2v_removed": i2v_removed or None,
              "model": {"unet": m.unet, "clip": m.clip, "video_vae": m.video_vae, "audio_vae": m.audio_vae,
                        "turbo_lora": W.H3_TURBO_8STEP_768P if bid == DEFAULT_BASE else None,
                        "base_lora": spec.get("lora"), "extra_loras": [list(x) for x in vloras],
                        "shift": shift, "sigmas": m.sigmas,
                        "sampler": m.sampler, "scheduler": None if m.sigmas else m.scheduler,
                        "size": list(P.VIDEO_SIZE), "fps": m.fps},
              "loras": [{"name": n, "strength": st} for n, st in req_loras],
              "render_s": secs_run, "upload_s": upload_s, "vram_peak_gb": vram.peak, "vram_total_gb": vram.total}
    take = Take(n=n, stage="video", file=f"{base}.mp4", seed=s, prompt=prompt, seconds=secs_run, frames=frames,
                created_at=P.now(), last_frame=f"{base}_last.png" if lf else None, workflow="h3_fl2va",
                parent=parent, from_first=frames_in["first"]["n"], gpu=gpu,
                job_id=(job or {}).get("id"), params=params, qc={"motion": motion},
                remote=CC.remote_info(client, P.DATA / f"{base}.mp4"))
    P._add_take(shot_id, take)
    P._clear_error(shot_id)
    return take


def stale_reasons(take: dict, takes: dict) -> list[str]:
    """视频版本：渲染时的首尾帧 ≠ 当前定稿（版本号或文件内容变了）→ 过期原因"""
    p = take.get("params") or {}
    out = []
    for key, stage, label in (("first", "image", "定稿首帧已换"), ("last", "last", "定稿尾帧已换")):
        rec = p.get(key)
        if not rec:
            continue
        n = locked_n(takes, stage)
        if n is None:
            out.append(label.replace("已换", "已取消定稿"))
            continue
        if n != rec.get("n"):
            out.append(label)
            continue
        t = next((t for t in takes.get("takes", []) if t["stage"] == stage and t["n"] == n), None)
        path = P.DATA / t["file"] if t else None
        if path and path.is_file() and rec.get("sha") and file_sha(path) != rec["sha"]:
            out.append(label)
    return out
