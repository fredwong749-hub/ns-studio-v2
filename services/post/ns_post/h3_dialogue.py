"""AUD（2026-10-31）：对白来源 = H3 原声（dialogue_source="h3"）。

H3 FL2VA 画面和声音一起生成，它自己那条对白和嘴型是同步的。"h3" 模式下合成器不再把 H3 轨在对白处压 40 dB、
也不再叠 TTS；只在这两种情况用 TTS：
  (a) 内心独白 / 画外音（画面上没有嘴）；
  (b) 兜底：本镜 H3 轨回听（SenseVoice，CPU）和剧本台词对不上（CER 超阈值）或根本没听到人声。
每次兜底都记一条点名镜号的 warning。

这里只放纯函数（抽 16 kHz 单声道 WAV、按字时间戳把识别结果切回每句台词、判定兜底、配乐报告），
assemble.py 只在声音相关几处调用。不读、不判断剧本/提示词内容本身，只做字符比对。
"""

from __future__ import annotations

from pathlib import Path

from .tts import asr_limit, char_error_rate, normalize

ASR_SR = 16000
TOKEN_TAIL_S = 0.25  # 最后一个字的时间戳是字起点；没有 durations 时按这个估字长


def extract_asr_wav(ff, video: str | Path, seconds: float, out: Path, start_s: float = 0.0) -> Path:
    """视频的音轨从 start_s 起 `seconds` 秒 → 16 kHz / 单声道 / 16-bit WAV（SenseVoice 输入）。
    start_s：FIX（合成剪掉开头定住段）后，时间戳相对剪后的镜头起点"""
    ss = ["-ss", f"{start_s:.4f}"] if start_s > 1e-6 else []
    ff.run([*ss, "-i", str(video), "-vn", "-t", f"{seconds:.3f}", "-ac", "1", "-ar", str(ASR_SR),
            "-c:a", "pcm_s16le", str(out)])
    return out


def _tokens(detail: dict) -> list[tuple[str, float, float | None]]:
    toks = detail.get("tokens") or []
    ts = detail.get("timestamps") or []
    du = detail.get("durations") or []
    out = []
    for k, tok in enumerate(toks):
        if k >= len(ts):
            break
        out.append((str(tok), float(ts[k]), float(du[k]) if k < len(du) and du[k] else None))
    return out


def split_lines(detail: dict, texts: list[str]) -> list[dict]:
    """把一镜的识别结果按台词顺序切成每句：{heard, t0, t1}（t 为镜内秒，无时间戳时为 None）。
    一句台词 → 整段；多句 → 按剧本每句的字数比例分配识别出的字（只用于定位，不改任何文字）。"""
    toks = [(t, a, d) for t, a, d in _tokens(detail) if normalize(t)]
    text = str(detail.get("text") or "")
    if len(texts) <= 1 or not toks:
        rows = [{"heard": text if i == 0 else "", "t0": None, "t1": None} for i in range(max(1, len(texts)))]
        if toks:
            rows[0]["t0"] = round(toks[0][1], 3)
            rows[0]["t1"] = round(toks[-1][1] + (toks[-1][2] or TOKEN_TAIL_S), 3)
        return rows[: len(texts)] if texts else rows
    ref = [max(1, len(normalize(t))) for t in texts]
    total_ref, total_heard = sum(ref), sum(len(normalize(t)) for t, _, _ in toks)
    rows, k, cum_ref, cum_heard = [], 0, 0, 0
    for i, n in enumerate(ref):
        cum_ref += n
        goal = total_heard if i == len(ref) - 1 else round(total_heard * cum_ref / total_ref)
        part = []
        while k < len(toks) and (cum_heard < goal or i == len(ref) - 1):
            part.append(toks[k])
            cum_heard += len(normalize(toks[k][0]))
            k += 1
        rows.append({"heard": "".join(t for t, _, _ in part),
                     "t0": round(part[0][1], 3) if part else None,
                     "t1": round(part[-1][1] + (part[-1][2] or TOKEN_TAIL_S), 3) if part else None})
    return rows


def check_shot(detail: dict | None, texts: list[str], max_cer: float) -> tuple[bool, list[dict], str | None]:
    """本镜 H3 原声能不能用：(ok, 每句 {heard, cer, limit, ok, t0, t1}, 不能用的原因)。
    任一句没听到人声或 CER 超过 max(max_cer, asr_limit(句子)) → 整镜兜底（避免 H3 句和 TTS 句互相重叠）。"""
    if detail is None:
        return False, [], "no H3 audio"
    rows = split_lines(detail, texts)
    reason = None
    for text, r in zip(texts, rows):
        limit = max(max_cer, asr_limit(text))
        r["cer"] = round(char_error_rate(text, r["heard"]), 3)
        r["limit"] = limit
        if not normalize(r["heard"]):
            r["ok"] = False
            reason = reason or "no speech detected"
        else:
            r["ok"] = r["cer"] <= limit
            if not r["ok"]:
                reason = reason or f"CER {r['cer']:.2f} > {limit:.2f}"
    return reason is None, rows, reason


def event_label(detail: dict | None) -> str | None:
    """SenseVoice 的音频事件标签：'<|BGM|>' → 'BGM'。"""
    ev = (detail or {}).get("event")
    return str(ev).strip("<|>") or None if ev else None


def music_row(detail: dict | None, prompt_source: str | None) -> dict:
    """配乐报告：提示词是否写了配乐（h3_music_shots 的来源）+ SenseVoice 是否把这段标成 BGM。"""
    ev = event_label(detail)
    return {"prompt_music": prompt_source, "asr_event": ev, "music_detected": ev == "BGM"}


def apply_h3_dialogue(ff, asr, found: list, shots: list[dict], durations: list[float], placements: list,
                      s, tmp_dir: Path, music_sources: dict[int, str] | None = None) -> tuple[list[dict], list[str]]:
    """dialogue_source="h3"：逐镜回听 H3 原声，合格的对白句改为 H3 原声（source="h3"、fixed、起止取字时间戳），
    不合格的整镜保持 TTS（兜底，记 warning 点名镜号）。返回 (逐镜报告, warnings)。"""
    warnings: list[str] = []
    tmp_dir = Path(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    starts = [sum(durations[:i]) for i in range(len(durations))]
    wav_of: dict[int, Path] = {}
    for i, shot in enumerate(shots):
        src, info = found[i]
        if src.get("kind") == "video" and info and info.get("has_audio"):
            wav_of[i] = extract_asr_wav(ff, src["path"], durations[i], tmp_dir / f"S{int(shot['index'])}.wav",
                                        start_s=float(src.get("trim_head_s") or 0.0))
    details: dict[int, dict] = {}
    if asr is None:
        if any(p.line.kind == "dialogue" for p in placements):
            warnings.append("dialogue_source=h3 but no ASR: H3 dialogue used unverified (no fallback check)")
    elif wav_of:
        keys = sorted(wav_of)
        if hasattr(asr, "transcribe_detail"):
            got = asr.transcribe_detail([wav_of[k] for k in keys], "zh")
        else:
            got = [{"text": t} for t in asr.transcribe([wav_of[k] for k in keys], "zh")]
        details = dict(zip(keys, got))
    report = []
    for i, shot in enumerate(shots):
        idx = int(shot["index"])
        det = details.get(i)
        dl = [p for p in placements if p.line.kind == "dialogue" and p.line.shot == i + 1]
        row = {"shot": idx, "h3_audio": i in wav_of, "heard": (det or {}).get("text"),
               "event": event_label(det), "music": music_row(det, (music_sources or {}).get(idx)),
               "dialogue": None, "lines": []}
        if dl:
            texts = [p.line.text for p in dl]
            if i in wav_of and asr is None:
                ok, rows, reason = True, [{"heard": None, "cer": None, "ok": None, "t0": None, "t1": None}
                                          for _ in dl], None
            else:
                ok, rows, reason = check_shot(det if i in wav_of else None, texts, s.h3_max_cer)
            if ok:
                for p, r in zip(dl, rows):
                    t0 = r["t0"] if r.get("t0") is not None else s.dialogue_lead_s
                    t1 = min(durations[i], r["t1"]) if r.get("t1") is not None else durations[i] - s.min_lead_s
                    p.source, p.fixed, p.tempo = "h3", True, 1.0
                    p.start_s, p.clip_s = round(starts[i] + t0, 3), round(max(0.05, t1 - t0), 3)
                row["dialogue"] = "h3"
            else:
                row["dialogue"], row["fallback_reason"] = "tts_fallback", reason
                warnings.append(f"S{idx}: H3 原声对白回听不合格（{reason}）→ 本镜对白用 TTS 兜底")
            for p, r in zip(dl, rows or [{}] * len(dl)):
                row["lines"].append({"key": p.line.key, "text": p.line.text, "heard": r.get("heard"),
                                     "cer": r.get("cer"), "limit": r.get("limit"), "ok": r.get("ok"),
                                     "t0": r.get("t0"), "t1": r.get("t1"), "source": p.source})
        report.append(row)
    return report, warnings
