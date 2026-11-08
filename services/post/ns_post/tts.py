"""Local CPU TTS (Kokoro multi-lang v1.1) and ASR round trip (SenseVoice), adapted from ns-studio
adapters/tts/local_tts (NSFW Drama system).

Speech runs in a separate, isolated interpreter that has only sherpa-onnx installed (NS_TTS_PYTHON);
this repo never imports sherpa-onnx. `speech_worker.py` is spoken to over JSON stdin/stdout. CPU only
(provider="cpu"), audio never leaves the machine. Voices are Kokoro's generic synthetic speakers — no
cloning of anyone. Exception (VOICE 2026-10-31): the narrator voice ("engine": "cosyvoice3_zs") is a fixed
*synthetic* reference (Qwen3-TTS VoiceDesign, text-designed, data/assets/voices/narrator_qag/) re-spoken by
zero-shot cloning on gpu36 (RemoteCloneBackend); RoutedTts sends those lines there, everything else stays local.

Clips are cached by sha256(model, sid, speed, text) under data/cache/tts/, so re-assembling an episode
does not re-synthesize unchanged lines.
"""

import hashlib
import json
import math
import os
import subprocess
import re
import unicodedata
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

ROOT = Path(os.environ.get("NS_PROJECT_ROOT") or Path(__file__).resolve().parents[3])  # T42：子进程可指向 projects/<slug>
WORKER = Path(__file__).with_name("speech_worker.py")
VOICES_JSON = ROOT / "config" / "voices.json"
CACHE_DIR = ROOT / "data" / "cache" / "tts"


class TtsError(RuntimeError):
    pass


class TtsUnavailable(TtsError):
    """No speech interpreter or model on this machine."""


@dataclass(frozen=True)
class Voice:
    speaker: str  # character id (config/voices.json)
    sid: int  # Kokoro speaker id
    name: str = ""  # Kokoro voice name (zf_001 ...)
    speed: float = 1.0
    model: str = "kokoro-multi-lang-v1_1"
    engine: str = "kokoro"  # kokoro = local CPU; other engines (cosyvoice3_zs, qwen3_clone) = RemoteCloneBackend

    @property
    def id(self) -> str:
        return f"{self.engine}:{self.name or self.sid}"


class VoiceRunRequired(TtsError):
    """当前剧是资产包模式（V2），调用方没传 run 就要读音色表——失败关闭，不退回 V1 的 config/voices.json（L-126，和 run_style 同口径）"""


def _current_run() -> str | None:
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "services" / "agent"))
    from ns_agent import run_paths as RP
    return RP.current_run(ROOT)


def load_voice_config(path: Path | None = None, *, run: str | None = None) -> dict:
    """按剧取音色表（OVERSIGHT-F P1-13a / 第 14 轮）：
    - run 给了：资产包模式的剧（V2）只用 config/voices_v2.json 启用的音色（sha 校验，没登记报错），legacy 剧 = config/voices.json；
    - run 没给、读的是本仓库 config/voices.json：当前剧是资产包模式 → VoiceRunRequired（失败关闭）；legacy → 照旧；
    - path 指向别的文件（测试 / 沙箱）：照旧读那个文件。"""
    if run is not None:
        from .run_media import voice_config
        return voice_config(run)
    p = Path(path) if path is not None else VOICES_JSON
    if p.resolve() == VOICES_JSON.resolve():
        from .run_media import is_asset_pack_run
        cur = _current_run()
        if is_asset_pack_run(cur, ROOT):
            raise VoiceRunRequired(f"当前剧 {cur} 是资产包模式：音色必须按剧取（load_voice_config(run=...)），不读 V1 的 config/voices.json")
    return json.loads(p.read_text(encoding="utf-8"))


def voice_for(cfg: dict, speaker: str, *, voiceover: bool = False, speed_factor: float = 1.0) -> list[Voice]:
    """The fixed voice(s) of a speaker: one Voice, or several for a group (chorus)."""
    chars = cfg["characters"]
    model = cfg.get("tts_model", {}).get("id", "kokoro-multi-lang-v1_1")
    members = cfg.get("groups", {}).get(speaker)
    if members:
        return [v for m in members for v in voice_for(cfg, m, speed_factor=speed_factor)]
    c = chars.get(speaker)
    if c is None:
        un = (cfg.get("unvoiced") or {}).get(speaker)
        if un:   # V2 人物表里有、但项目方还没选音色的人：报错，不拿女主或别人顶替（总调度 22:4x）
            raise TtsError(un)
        raise TtsError(f"no voice for speaker {speaker!r}: add it to config/voices.json")
    speed = float(c.get("vo_speed", c.get("speed", 1.0)) if voiceover else c.get("speed", 1.0))
    engine = c.get("engine") or "kokoro"
    if engine != "kokoro":
        # 克隆音色：缓存键里带参考音频 sha，换参考就自动重合成
        sha = str(c.get("ref_sha256") or "")
        if not sha:
            raise TtsError(f"speaker {speaker!r} uses engine {engine!r} but has no ref_sha256 in config/voices.json")
        return [Voice(speaker, -1, c.get("voice", speaker), round(speed * speed_factor, 3), f"{engine}:{sha[:16]}",
                      engine)]
    return [Voice(speaker, int(c["sid"]), c.get("voice", ""), round(speed * speed_factor, 3), model)]


def default_voiceover_speaker(cfg: dict | None = None, *, run: str | None = None) -> str:
    """独白 / 旁白默认说话人（音色表的 voiceover_speaker）。只有 V1 音色表（含 lin_wan）没写时才退回 lin_wan；
    V2（按剧取的音色表里没有 lin_wan）没写或写错 → TtsError，不许退回 V1 的人（OVERSIGHT-F 第 14 轮）"""
    if cfg is None:
        try:
            cfg = load_voice_config(run=run) if run is not None else load_voice_config()
        except (OSError, ValueError):
            cfg = {}
    chars = (cfg or {}).get("characters") or {}
    sp = (cfg or {}).get("voiceover_speaker")
    if sp and sp in chars:
        return sp
    if chars and "lin_wan" not in chars:
        raise TtsError(f"音色表没有可用的 voiceover_speaker（{sp!r}），不退回 lin_wan")
    return "lin_wan"


# ---------------- WAV helpers (stdlib) ----------------

def wav_duration(path: str | Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def write_tone(path: Path, seconds: float, freq: float, sample_rate: int = 24000) -> None:
    n = max(1, round(seconds * sample_rate))
    pcm = array("h", (int(6000 * math.sin(2 * math.pi * freq * i / sample_rate)) for i in range(n)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())


# ---------------- backends ----------------

class TtsBackend(Protocol):
    name: str

    def synthesize(self, items: list[tuple[str, Voice, Path]]) -> None:
        """Write one 16-bit mono WAV per (text, voice, out path)."""
        ...


class AsrBackend(Protocol):
    def transcribe(self, wavs: list[Path], language: str = "zh") -> list[str]: ...


Runner = Callable[[list[str], str, float], subprocess.CompletedProcess]


def run_json(cmd: list[str], stdin: str, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")


def _setting(cfg: dict, section: str, key_env: str, key_default: str) -> str | None:
    sec = cfg.get(section, {})
    env = sec.get(key_env)
    v = os.environ.get(env) if env else None
    if v and Path(v).exists():
        return v
    d = sec.get(key_default)
    if d and Path(d).exists():
        return d
    # VOICE10 11-01（K-EDT-022）：服务器常驻合成（Linux）用 <key>_posix（如 default_posix / default_dir_posix），
    # 不用在 WORKFLOW 的 start.sh 里另设环境变量
    d = sec.get(f"{key_default}_posix") if os.name != "nt" else None
    return d if d and Path(d).exists() else None


class _Worker:
    def __init__(self, cfg: dict, model_section: str, *, python: str | None = None, model_dir: str | None = None,
                 num_threads: int = 4, timeout_s: float = 3600.0, runner: Runner = run_json,
                 require_model: bool = True):
        self.python = python or _setting(cfg, "speech_python", "env", "default")
        self.model_dir = model_dir or _setting(cfg, model_section, "env_dir", "default_dir")
        self.model_section = model_section
        if not self.python:
            raise TtsUnavailable("no speech interpreter: set NS_TTS_PYTHON (python with sherpa-onnx==1.13.8)")
        if not self.model_dir and require_model:
            raise TtsUnavailable(f"no model directory for {model_section}: see config/voices.json")
        self.num_threads, self.timeout_s, self.runner = num_threads, timeout_s, runner

    def call(self, request: dict) -> dict:
        request = {"model_dir": self.model_dir, "num_threads": self.num_threads, **request}
        try:
            proc = self.runner([self.python, "-I", str(WORKER)], json.dumps(request), self.timeout_s)
        except subprocess.TimeoutExpired as e:
            raise TtsError(f"speech worker timed out after {self.timeout_s:.0f} s") from e
        except OSError as e:
            raise TtsUnavailable(f"cannot start {self.python}: {e}") from e
        try:
            resp = json.loads(proc.stdout or "")
        except ValueError:
            raise TtsError(f"speech worker exited {proc.returncode}: {(proc.stderr or '')[-500:]}") from None
        if not resp.get("ok"):
            raise TtsError(f"speech worker failed: {resp.get('error')}")
        return resp


class KokoroBackend(_Worker):
    name = "kokoro"

    def __init__(self, cfg: dict, **kw):
        # VOICE10 11-01（K-EDT-022）：series10 全部角色 + 旁白都是克隆音色（RoutedTts 不会把句子交给 Kokoro），
        # 所以 Kokoro 模型目录到真要合成 Kokoro 句子时才必需；服务器上没下 Kokoro 也能合成成片
        kw.setdefault("require_model", False)
        super().__init__(cfg, "tts_model", **kw)
        self.cfg = cfg          # synthesize_cached 转远程克隆时用同一份（按剧取的）音色表

    def synthesize(self, items: list[tuple[str, Voice, Path]]) -> None:
        if not items:
            return
        if not self.model_dir:
            raise TtsUnavailable("no model directory for tts_model (Kokoro): see config/voices.json")
        req =[{"text": t, "sid": v.sid, "speed": v.speed, "out": str(p)} for t, v, p in items]
        resp = self.call({"op": "tts", "items": req})
        if len(resp.get("items", [])) != len(req):
            raise TtsError("speech worker returned the wrong number of clips")


RemoteRunner = Callable[[list[str], float], subprocess.CompletedProcess]


def run_cmd(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")


REMOTE_SRC = Path(__file__).with_name("remote")
REMOTE_FILES = ("narrator_batch.py", "run_narrator.sh", "spk_sim.py")


def _file_sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def remote_is_local(rt: dict) -> bool:
    """VOICE10 11-01（K-EDT-022）：服务器常驻生产时合成器就跑在 remote_tts.host 本机（gpu36 上的 night.py），
    不能再 `ssh gpu36` 自己连自己。判定：NS_TTS_REMOTE_LOCAL=1，或 OFFLINE_SELF（WORKFLOW 常驻补丁设的本机名）== host"""
    if os.environ.get("NS_TTS_REMOTE_LOCAL") == "1":
        return True
    me = os.environ.get("OFFLINE_SELF")
    # 总调度 11-01 方案 (b)：gpu33 自装一套 CosyVoice3（两台服务器之间刻意不互相登录），local_hosts 里的机器都本机合成
    return bool(me) and (me == str(rt.get("host") or "") or me in (rt.get("local_hosts") or []))


def remote_gpus(rt: dict) -> list:
    """本机合成时按 OFFLINE_SELF 取 gpus_by_host（gpu33 的 GPU2 是坏卡、0/1/3 是出片道 → 配 [] 走 CPU）；否则 gpus"""
    me = os.environ.get("OFFLINE_SELF")
    by = rt.get("gpus_by_host") or {}
    return list(by[me]) if me and me in by and remote_is_local(rt) else list(rt.get("gpus") or [])


class _LocalRunner:
    """把 RemoteCloneBackend 发出的 ssh / scp 命令改成本机执行：ssh HOST CMD → bash -c CMD；scp A B → cp（去掉 HOST: 前缀）"""

    def __init__(self, host: str, runner: "RemoteRunner"):
        self.host, self.runner = host, runner

    def _strip(self, s: str) -> str:
        return s[len(self.host) + 1:] if s.startswith(self.host + ":") else s

    def __call__(self, cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
        if cmd and cmd[0] == "ssh":
            return self.runner(["bash", "-c", cmd[-1]], timeout)
        if cmd and cmd[0] == "scp":
            return self.runner(["cp", self._strip(cmd[-2]), self._strip(cmd[-1])], timeout)
        return self.runner(cmd, timeout)


class RemoteCloneBackend:
    """VOICE 2026-10-31：旁白音色（QAg）= 固定合成参考音频 + 零样本复刻，在 gpu36 的 venv_tts 里批处理。

    一次 synthesize = 每个说话人一次 ssh 批处理：按需上传 remote/ 脚本和参考音频（远端 sha256 不同才传），
    上传请求 JSON → run_narrator.sh 选空闲卡（config remote_tts.gpus 里已用 < max_used_mb 且本卡锁空闲，
    否则 CPU）→ 取回 24 kHz 单声道 16-bit WAV。runner 可注入（测试用 mock，不碰网络和 GPU）。
    """

    name = "remote_clone"

    def __init__(self, cfg: dict, *, runner: RemoteRunner = run_cmd, root: Path | None = None):
        self.cfg = cfg
        self.rt = dict(cfg.get("remote_tts") or {})
        if not self.rt.get("host") or not self.rt.get("base_dir"):
            raise TtsUnavailable("config/voices.json remote_tts.host / base_dir missing")
        self.local = remote_is_local(self.rt)
        self.runner = _LocalRunner(str(self.rt["host"]), runner) if self.local else runner
        self.root = Path(root or ROOT)
        self.timeout = float(self.rt.get("timeout_s", 3600))
        self._synced: set[str] = set()

    # -- transport -------------------------------------------------------------------------------
    def _ssh(self, command: str, timeout: float = 120) -> subprocess.CompletedProcess:
        p = self.runner(["ssh", "-o", "BatchMode=yes", str(self.rt["host"]), command], timeout)
        if p.returncode != 0:
            raise TtsError(f"ssh {self.rt['host']} failed ({p.returncode}): {(p.stderr or p.stdout or '')[-400:]}")
        return p

    def _scp(self, src: str, dst: str, timeout: float = 600) -> None:
        p = self.runner(["scp", "-q", "-o", "BatchMode=yes", src, dst], timeout)
        if p.returncode != 0:
            raise TtsError(f"scp {src} -> {dst} failed: {(p.stderr or '')[-400:]}")

    def _remote(self, rel: str) -> str:
        return f"{str(self.rt['base_dir']).rstrip('/')}/{rel}"

    def _sync(self, local: Path, rel: str) -> str:
        """上传一个文件（远端 sha256 一致就跳过）；返回远端路径"""
        dst = self._remote(rel)
        if dst in self._synced:
            return dst
        sha = _file_sha(local)
        p = self.runner(["ssh", "-o", "BatchMode=yes", str(self.rt["host"]),
                         f"sha256sum {dst} 2>/dev/null || true"], 60)
        if not (p.stdout or "").startswith(sha):
            self._ssh(f"mkdir -p {dst.rsplit('/', 1)[0]}")
            self._scp(str(local), f"{self.rt['host']}:{dst}")
        self._synced.add(dst)
        return dst

    # -- synthesis -------------------------------------------------------------------------------
    def _speaker_cfg(self, speaker: str) -> dict:
        c = (self.cfg.get("characters") or {}).get(speaker) or {}
        ref = self.root / str(c.get("ref_wav") or "")
        if not c.get("ref_wav") or not ref.is_file():
            raise TtsUnavailable(f"reference audio for {speaker!r} not found: {c.get('ref_wav')}")
        if c.get("ref_sha256") and _file_sha(ref) != c["ref_sha256"]:
            raise TtsError(f"reference audio for {speaker!r} does not match ref_sha256 in config/voices.json")
        meta = {}
        if c.get("asset"):
            try:
                meta = json.loads((self.root / c["asset"]).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
        ref_text = c.get("ref_text") or meta.get("ref_text")
        if not ref_text:
            raise TtsError(f"no ref_text for {speaker!r} (voices.json or its asset file)")
        return {"ref": ref, "ref_text": ref_text}

    def synthesize(self, items: list[tuple[str, Voice, Path]]) -> None:
        if not items:
            return
        import tempfile
        import uuid
        for f in REMOTE_FILES:
            self._sync(REMOTE_SRC / f, f)
        groups: dict[tuple[str, str], list[tuple[str, Voice, Path]]] = {}
        for t, v, p in items:
            groups.setdefault((v.speaker, v.engine), []).append((t, v, p))
        for (speaker, engine), group in groups.items():
            sc = self._speaker_cfg(speaker)
            ref_remote = self._sync(sc["ref"], f"refs/{speaker}_{_file_sha(sc['ref'])[:12]}.wav")
            model_dir = (self.rt.get("model_dirs") or {}).get(engine)
            if not model_dir:
                raise TtsUnavailable(f"remote_tts.model_dirs has no entry for engine {engine!r}")
            jdir = self._remote(f"jobs/{uuid.uuid4().hex[:12]}")
            req = {"engine": engine, "model_dir": model_dir, "ref_wav": ref_remote, "ref_text": sc["ref_text"],
                   "seed": int(self.rt.get("seed", 20261031)),
                   "items": [{"text": t, "out": f"{jdir}/{i:03d}.wav", "speed": v.speed}
                             for i, (t, v, _) in enumerate(group)]}
            with tempfile.TemporaryDirectory() as td:
                lreq = Path(td) / "request.json"
                lreq.write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
                self._ssh(f"mkdir -p {jdir}")
                self._scp(str(lreq), f"{self.rt['host']}:{jdir}/request.json")
                gpus = " ".join(str(g) for g in remote_gpus(self.rt))
                p = self._ssh(f"cd {self._remote('')} || exit 1; sh run_narrator.sh {jdir}/request.json "
                              f"'{gpus}' {int(self.rt.get('max_used_mb', 13500))}", timeout=self.timeout)
                try:
                    resp = json.loads((p.stdout or "").strip().splitlines()[-1])
                except (ValueError, IndexError):
                    raise TtsError(f"narrator worker returned no JSON: {(p.stdout or '')[-300:]}") from None
                if not resp.get("ok") or len(resp.get("items") or []) != len(group):
                    raise TtsError(f"narrator worker failed: {resp.get('error') or resp}")
                for i, (_, _, out) in enumerate(group):
                    self._scp(f"{self.rt['host']}:{jdir}/{i:03d}.wav", str(out))
                    if not Path(out).is_file():
                        raise TtsError(f"narrator worker did not return {Path(out).name}")
            self.runner(["ssh", "-o", "BatchMode=yes", str(self.rt["host"]), f"rm -rf {jdir}"], 60)


class RoutedTts:
    """按 Voice.engine 分派：kokoro → 本机后端（默认），其它 → RemoteCloneBackend（第一次用到才建）。
    name 跟随默认后端，所以 Kokoro 句子的缓存键不变。"""

    def __init__(self, default: "TtsBackend", cfg: dict, *, remote: "TtsBackend | None" = None,
                 remote_factory: "Callable[[dict], TtsBackend] | None" = None):
        self.default, self.cfg, self._remote = default, cfg, remote
        self._factory = remote_factory or (lambda c: RemoteCloneBackend(c))
        self.name = getattr(default, "name", "kokoro")

    @property
    def remote(self) -> "TtsBackend":
        if self._remote is None:
            self._remote = self._factory(self.cfg)
        return self._remote

    def synthesize(self, items: list[tuple[str, Voice, Path]]) -> None:
        local = [it for it in items if it[1].engine == "kokoro"]
        other = [it for it in items if it[1].engine != "kokoro"]
        if local:
            self.default.synthesize(local)
        if other:
            self.remote.synthesize(other)


class SenseVoiceAsr(_Worker):
    def __init__(self, cfg: dict, **kw):
        super().__init__(cfg, "asr_model", **kw)

    def transcribe(self, wavs: list[Path], language: str = "zh") -> list[str]:
        if not wavs:
            return []
        resp = self.call({"op": "asr", "language": language, "items": [{"wav": str(p)} for p in wavs]})
        return [str(i.get("text", "")) for i in resp.get("items", [])]

    def transcribe_detail(self, wavs: list[Path], language: str = "zh") -> list[dict]:
        """AUD：text + tokens/timestamps（每字起点秒）+ event（<|BGM|>/<|Speech|>…），供 H3 原声对白核对。"""
        if not wavs:
            return []
        resp = self.call({"op": "asr", "language": language, "items": [{"wav": str(p)} for p in wavs]})
        return [dict(i) for i in resp.get("items", [])]


class FakeTts:
    """Tests: a tone per line, length from the character count (12 chars/s ÷ speed), pitch from the sid."""

    name = "fake"

    def __init__(self, chars_per_second: float = 12.0):
        self.cps = chars_per_second
        self.calls: list[tuple[str, int]] = []

    def synthesize(self, items: list[tuple[str, Voice, Path]]) -> None:
        for text, voice, out in items:
            self.calls.append((text, voice.sid))
            n = sum(ch.isalnum() for ch in text)
            write_tone(out, max(0.3, n / self.cps) / max(voice.speed, 0.1), 150.0 + 3.0 * voice.sid)


class FakeAsr:
    def __init__(self, transcripts: dict[str, str] | None = None, details: dict[str, dict] | None = None):
        self.transcripts = transcripts or {}
        self.details = details or {}

    def transcribe(self, wavs: list[Path], language: str = "zh") -> list[str]:
        return [self.transcripts.get(Path(w).name, "") for w in wavs]

    def transcribe_detail(self, wavs: list[Path], language: str = "zh") -> list[dict]:
        """details: {wav name: {"text", "timestamps", "event", ...}}；没有的退回 transcripts 的纯文本"""
        out = []
        for w in wavs:
            d = dict(getattr(self, "details", {}).get(Path(w).name) or {})
            d.setdefault("text", self.transcripts.get(Path(w).name, ""))
            out.append(d)
        return out


def tts_preflight(cfg: dict | None = None, *, speakers: "list[str] | None" = None,
                  root: Path | None = None, run: str | None = None) -> list[str]:
    """VOICE10 2026-11-01（K-EDT-021）：合成前一次查完本机配音的全部前提，返回缺什么（空 = 齐）。
    night.py tts_missing 调它：缺了就 hold_tts（不耗版本号、不耗自动重试），不再等合成器跑到一半才报错。
    查：speech 解释器、Kokoro / SenseVoice 模型目录、每个要用到的说话人（默认 = 全部 characters）有音色条目，
    克隆音色（engine ≠ kokoro）的参考音频在、sha 对得上、有 ref_text、remote_tts 有该 engine 的模型目录。
    不查网络（gpu36 ssh 断开由隧道看门狗管，失败走 assemble_failed 的自动重试）。"""
    root = Path(root or ROOT)
    if cfg is None:
        try:   # 按剧取：run 给了走 run；没给且当前剧是资产包模式 → 报缺（不退回 V1 的音色表，OVERSIGHT-F 第 14 轮）
            cfg = load_voice_config(run=run) if run is not None else load_voice_config(root / "config" / "voices.json")
        except TtsError as e:
            return [f"音色表读不了：{e}"]
        except (OSError, ValueError, RuntimeError) as e:
            return [f"音色表读不了：{e}"]
    out: list[str] = []
    sp = cfg.get("speech_python") or {}
    if not _setting(cfg, "speech_python", "env", "default"):
        out.append(f"没有配音解释器：{sp.get('env') or 'NS_TTS_PYTHON'} 未设，default {sp.get('default')} 不存在")
    chars = cfg.get("characters") or {}
    rt = cfg.get("remote_tts") or {}
    need = speakers if speakers is not None else list(chars)
    secs = ["asr_model"]
    if any((chars.get(s) or {}).get("engine", "kokoro") == "kokoro" for s in need if s in chars):
        secs.insert(0, "tts_model")   # 只有用 Kokoro 音色的说话人才需要 Kokoro 模型
    for sec in secs:
        if not _setting(cfg, sec, "env_dir", "default_dir"):
            out.append(f"没有 {sec} 模型目录（{(cfg.get(sec) or {}).get('env_dir')} / default_dir）")
    for s in need:
        c = chars.get(s)
        if c is None:
            out.append(f"说话人 {s} 在 config/voices.json 没有音色")
            continue
        eng = c.get("engine") or "kokoro"
        if eng == "kokoro":
            if c.get("sid") is None:
                out.append(f"{s}：Kokoro 音色缺 sid")
            continue
        ref = root / str(c.get("ref_wav") or "")
        if not c.get("ref_wav") or not ref.is_file():
            out.append(f"{s}：参考音频不在 {c.get('ref_wav')}")
        elif c.get("ref_sha256") and _file_sha(ref) != c["ref_sha256"]:
            out.append(f"{s}：参考音频 sha 和 voices.json 不一致")
        meta = {}
        if c.get("asset"):
            try:
                meta = json.loads((root / c["asset"]).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
        if not (c.get("ref_text") or meta.get("ref_text")):
            out.append(f"{s}：缺 ref_text（voices.json 或 {c.get('asset')}）")
        if not (rt.get("host") and rt.get("base_dir") and (rt.get("model_dirs") or {}).get(eng)):
            out.append(f"{s}：remote_tts 缺 host / base_dir / model_dirs.{eng}")
        elif remote_is_local(rt) and os.name != "nt":   # 本机合成：venv_tts 和模型目录必须在本机（gpu33 方案 b）
            vpy = Path(str(rt.get("venv") or "/root/work/fredw/venv_tts")) / "bin" / "python"
            if not vpy.exists():
                out.append(f"{s}：本机合成但没有 {vpy}（tools/voice10/cosy_setup.sh install）")
            if not Path(str(rt["model_dirs"][eng])).is_dir():
                out.append(f"{s}：本机合成但没有模型目录 {rt['model_dirs'][eng]}")
    return out


# ---------------- cached synthesis ----------------

def clip_key(text: str, voice: Voice, backend: str) -> str:
    h = hashlib.sha256(json.dumps([backend, voice.model, voice.sid, voice.speed, text],
                                  ensure_ascii=False).encode("utf-8")).hexdigest()
    return h[:24]


def synthesize_cached(backend: TtsBackend, requests: list[tuple[str, Voice]], cache_dir: Path = CACHE_DIR
                      ) -> list[Path]:
    """One WAV per (text, voice); only lines not in the cache are synthesized (one worker call)."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    if getattr(backend, "name", "") == "kokoro" and not isinstance(backend, RoutedTts) \
            and any(v.engine != "kokoro" for _, v in requests):
        # 旁白克隆音色走远程后端；Kokoro 句子缓存键不变。音色表用建 backend 时那份（按剧取的），不再自己读 V1 的
        backend = RoutedTts(backend, getattr(backend, "cfg", None) or load_voice_config())
    paths, todo, queued = [], [], set()
    for text, voice in requests:
        p = cache_dir / f"{clip_key(text, voice, backend.name)}.wav"
        paths.append(p)
        # VOICE10 2026-11-01（K-EDT-019）：同一集里同一角色说同一句（ep04 S1/S2“今天特意早点回来。”）只合成一次。
        # 旧写法拿 .wav 和 .tmp.wav 比，永远不相等 → 同一个 tmp 进 todo 两次，第一次改名后第二次报
        # “TTS did not write …tmp.wav”（ep04 16:09 合成失败的根因）。
        if not p.exists() and p not in queued:
            queued.add(p)
            todo.append((text, voice, p.with_suffix(".tmp.wav")))
    if todo:
        backend.synthesize(todo)
        for _, _, tmp in todo:
            if not tmp.exists():
                raise TtsError(f"TTS did not write {tmp.name}")
            os.replace(tmp, tmp.with_name(tmp.name.replace(".tmp.wav", ".wav")))
    return paths


# ---------------- ASR round trip ----------------

def normalize(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).casefold()
                   if unicodedata.category(ch)[0] in "LN")


_ZH_DIGIT = "零一二三四五六七八九"


def _int_zh(n: int) -> str:
    """整数 → 中文读法（12 → 十二，105 → 一百零五，20000 → 二万）"""
    if n == 0:
        return "零"

    def under_10k(x: int) -> str:
        out, zero = "", False
        for unit, base in (("千", 1000), ("百", 100), ("十", 10), ("", 1)):
            d, x = divmod(x, base)
            if d:
                if zero and out:
                    out += "零"
                out += _ZH_DIGIT[d] + unit
                zero = False
            elif out:
                zero = True
        return out

    parts, out = [], ""
    for unit in ("", "万", "亿"):
        n, r = divmod(n, 10000)
        parts.append((r, unit))
        if not n:
            break
    for i, (r, unit) in enumerate(reversed(parts)):
        if r:
            if out and r < 1000:
                out += "零"
            out += under_10k(r) + unit
    return out[1:] if out.startswith("一十") else out


def _num_zh(m: "re.Match") -> str:
    s = m.group(1)
    whole, _, frac = s.partition(".")
    z = _int_zh(int(whole)) + ("点" + "".join(_ZH_DIGIT[int(c)] for c in frac) if frac else "")
    return ("百分之" + z) if m.group(2) else z


def canon_numerals(text: str) -> str:
    """CER 比对前把数字写法统一成中文读法：ASR 常把“九 / 百分之十二 / 两个”写成“9 / 12% / 2个”，
    剧本是汉字，不统一就算成错字（VOICE10 2026-11-01：林晚 4 个候选 CER 都是 0.182，全是这个原因）。两 → 二。"""
    t = re.sub(r"(\d+(?:\.\d+)?)\s*([%％])?", _num_zh, text or "")  # \d 含全角数字，int() 能直接解析
    return t.replace("两", "二")


def char_error_rate(reference: str, hypothesis: str) -> float:
    ref, hyp = normalize(canon_numerals(reference)), normalize(canon_numerals(hypothesis))
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return min(1.0, prev[-1] / len(ref))


def asr_limit(text: str) -> float:
    """Short lines are dominated by homophones (有/又): allow more."""
    return 0.5 if len(normalize(text)) <= 6 else 0.2
