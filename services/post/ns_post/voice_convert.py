"""VOX（fredw-92，2026-10-31）：只把某个角色那句对白的时间段换成参考音色，其余原样，嘴型 / 时间不动。

项目方原话：“男生的声音有点生硬”，修法选了“下载变声模型保留嘴型”。

做法（不分离、不改时长）：
  1. 本镜 H3 原声 → SenseVoice 逐字时间戳（和合成器 h3 模式同一个模型）→ 该句台词的 [t0, t1]；
  2. 只取 [t0 - pad_pre, t1 + pad_post] 这一段 → Seed-VC 零样本变声（length_adjust = 1.0，时长不变）；
  3. 换回原位置：长度对齐到原段（误差补零 / 截掉），响度按原段 RMS，两头各 xfade 秒等功率交叉淡化；
     段外的样本一个不动 → 画面、嘴型、配乐时间都不变。段内的配乐会跟着人声一起过变声模型（没有分轨，已知代价）。

Seed-VC（Plachtaa/seed-vc，GPL-3.0；权重 Plachta/Seed-VC GPL-3.0）装在独立目录 / venv（默认 D:\\ns_vc），
本仓库只经子进程调用它的 inference.py，不 import、不进主 venv。
参考音色必须由项目方给；没给之前只能用演示参考（文件名带 demo），不代表选择。
不做内容检测；台词只用于字符比对和定位。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

SEEDVC_DIR = Path(os.environ.get("NS_SEEDVC_DIR", r"D:\ns_vc\seed-vc"))
SEEDVC_PY = Path(os.environ.get("NS_SEEDVC_PYTHON", r"D:\ns_vc\venv\Scripts\python.exe"))
SR = 48000


class VcUnavailable(RuntimeError):
    pass


def available() -> bool:
    return (SEEDVC_DIR / "inference.py").exists() and SEEDVC_PY.exists()


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, timeout=300)   # K-OPS-S01


def read_wav(p: Path) -> tuple[np.ndarray, int]:
    """→ (samples × channels float32, sr)"""
    with wave.open(str(p), "rb") as w:
        sr, n, ch = w.getframerate(), w.getnframes(), w.getnchannels()
        x = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
    return x.reshape(-1, ch), sr


def write_wav(p: Path, x: np.ndarray, sr: int) -> None:
    x = np.clip(x, -1.0, 1.0)
    with wave.open(str(p), "wb") as w:
        w.setnchannels(x.shape[1])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((x * 32767.0).astype("<i2").tobytes())


def seed_vc(src: Path, ref: Path, out_dir: Path, *, steps: int = 30, cfg_rate: float = 0.7) -> Path:
    """Seed-VC v1 inference.py（语音模型 22.05 kHz，f0_condition 关，length_adjust 1.0）→ 输出 wav 路径"""
    if not available():
        raise VcUnavailable(f"Seed-VC 未安装：{SEEDVC_DIR}（见 docs/QC_ep02_20261031.md §6）")
    out_dir = Path(out_dir).resolve()
    src, ref = Path(src).resolve(), Path(ref).resolve()     # 子进程 cwd 是 Seed-VC 目录
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(SEEDVC_PY), "inference.py", "--source", str(src), "--target", str(ref), "--output", str(out_dir),
           "--diffusion-steps", str(steps), "--length-adjust", "1.0", "--inference-cfg-rate", str(cfg_rate),
           "--f0-condition", "False", "--fp16", "True"]
    p = subprocess.run(cmd, cwd=str(SEEDVC_DIR), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=1800)   # K-OPS-S01：Seed-VC 单条推理远小于 30 分钟，超了就是挂住
    outs = sorted(out_dir.glob("vc_*.wav"), key=lambda q: q.stat().st_mtime)
    if p.returncode != 0 or not outs:
        raise RuntimeError(f"Seed-VC 失败（{p.returncode}）：{(p.stderr or p.stdout)[-800:]}")
    return outs[-1]


def splice(orig: np.ndarray, conv_mono: np.ndarray, a: int, b: int, xfade: int) -> np.ndarray:
    """orig[a:b] 换成 conv（长度已对齐到 b-a），两头 xfade 个样本等功率淡化；段外原样"""
    out = orig.copy()
    seg = np.repeat(conv_mono[:, None], orig.shape[1], axis=1)
    w = np.ones(b - a, dtype=np.float32)
    k = min(xfade, (b - a) // 2)
    if k > 0:
        ramp = np.sin(np.linspace(0, np.pi / 2, k, dtype=np.float32))
        w[:k], w[-k:] = ramp, ramp[::-1]
    g_new = w[:, None]
    g_old = np.sqrt(np.clip(1 - w ** 2, 0, 1))[:, None]
    out[a:b] = orig[a:b] * g_old + seg * g_new
    return out


def convert_span(audio_wav: Path, t0: float, t1: float, reference: Path, out_wav: Path, *,
                 pad_pre: float = 0.12, pad_post: float = 0.25, xfade_s: float = 0.04, steps: int = 30,
                 work: Path | None = None) -> dict:
    """audio_wav（48 kHz 16-bit）里 [t0-pad_pre, t1+pad_post] 换成参考音色 → out_wav（同长度、同采样率）"""
    x, sr = read_wav(audio_wav)
    if sr != SR:
        raise ValueError(f"需要 {SR} Hz 的 wav：{audio_wav}")
    a = max(0, int((t0 - pad_pre) * sr))
    b = min(len(x), int((t1 + pad_post) * sr))
    if b - a < int(0.2 * sr):
        raise ValueError(f"台词时间段太短：{t0}–{t1}")
    tmp = Path(work) if work else Path(tempfile.mkdtemp(prefix="ns_vc_"))
    tmp.mkdir(parents=True, exist_ok=True)
    seg_src = tmp / "seg_src.wav"
    write_wav(seg_src, x[a:b].mean(axis=1, keepdims=True), sr)
    seg22 = tmp / "seg_src22.wav"
    _ff("-i", str(seg_src), "-ar", "22050", "-ac", "1", str(seg22))
    vc_out = seed_vc(seg22, reference, tmp / "vc", steps=steps)
    vc48 = tmp / "seg_vc48.wav"
    _ff("-i", str(vc_out), "-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", str(vc48))
    conv, _ = read_wav(vc48)
    conv = conv[:, 0]
    n = b - a
    len_err = len(conv) - n
    conv = conv[:n] if len(conv) >= n else np.pad(conv, (0, n - len(conv)))
    rms_o = float(np.sqrt(np.mean(x[a:b] ** 2)) + 1e-9)
    rms_c = float(np.sqrt(np.mean(conv ** 2)) + 1e-9)
    conv = conv * (rms_o / rms_c)
    out = splice(x, conv, a, b, int(xfade_s * sr))
    write_wav(out_wav, out, sr)
    if not work:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"span_s": [round(a / sr, 3), round(b / sr, 3)], "t0": t0, "t1": t1, "len_err_samples": int(len_err),
            "gain_db": round(20 * np.log10(rms_o / rms_c), 2), "xfade_s": xfade_s, "steps": steps,
            "reference": str(reference), "model": "Seed-VC v1 (DiT_seed_v2_uvit_whisper_small_wavenet_bigvgan_pruned)",
            "untouched_outside_span": bool(np.array_equal(out[:a], x[:a]) and np.array_equal(out[b:], x[b:]))}


def extract_audio(video: Path, out_wav: Path) -> Path:
    _ff("-i", str(video), "-vn", "-ac", "2", "-ar", str(SR), "-c:a", "pcm_s16le", str(out_wav))
    return out_wav


def mux(video: Path, audio_wav: Path, out_mp4: Path) -> Path:
    """画面流原样复制，只换音轨"""
    _ff("-i", str(video), "-i", str(audio_wav), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-shortest", str(out_mp4))
    return out_mp4


def config_hook(post_cfg: dict) -> dict | None:
    """config/render_quality.json post.voice_conversion：{enabled: false, speaker, reference, ...}；默认关 → None"""
    vc = (post_cfg or {}).get("voice_conversion") or {}
    if not vc.get("enabled") or not vc.get("speaker") or not vc.get("reference"):
        return None
    return vc


def load_hook(config: Path) -> dict | None:
    try:
        return config_hook(json.loads(Path(config).read_text(encoding="utf-8")).get("post") or {})
    except (OSError, ValueError):
        return None


def assembler_audio(video: Path, spans: list[tuple[float, float]], vc: dict, work: Path) -> tuple[Path | None, str | None]:
    """合成器 hook：本镜 H3 原声里 spans（镜内秒，来自 h3 回听时间戳）逐段换成 vc["reference"] 的音色。
    → (换好的 48 kHz wav, None) 或 (None, 不换的原因)。失败不拦合成，调用方记 warning 用原声。"""
    if not spans:
        return None, None
    if not available():
        return None, f"Seed-VC 未安装（{SEEDVC_DIR}）"
    ref = Path(vc["reference"])
    if not ref.exists():
        return None, f"参考音色文件不存在：{ref}"
    work.mkdir(parents=True, exist_ok=True)
    cur = extract_audio(video, work / "h3_orig.wav")
    try:
        for k, (t0, t1) in enumerate(spans):
            nxt = work / f"h3_vc{k}.wav"
            convert_span(cur, t0, t1, ref, nxt, pad_pre=float(vc.get("pad_pre_s", 0.12)),
                         pad_post=float(vc.get("pad_post_s", 0.25)), xfade_s=float(vc.get("xfade_s", 0.04)),
                         steps=int(vc.get("steps", 30)), work=work / f"seg{k}")
            cur = nxt
    except Exception as e:  # noqa: BLE001
        return None, f"变声失败：{e}"
    return cur, None


def dump(rec: dict, p: Path) -> None:
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
