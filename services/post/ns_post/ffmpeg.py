"""FFmpeg/ffprobe discovery and a thin runner (adapted from ns-studio packages/media/media/ffmpeg.py).

Discovery: NS_FFMPEG (ffmpeg(.exe) or its bin folder) → PATH → winget Gyan.FFmpeg under %LOCALAPPDATA%.
Commands are argument lists, never shell strings. CPU only: no hardware encoders/decoders are used.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

EXE = ".exe" if sys.platform == "win32" else ""
WINGET_GLOB = "Microsoft/WinGet/Packages/Gyan.FFmpeg*/ffmpeg-*/bin"


class FFmpegError(RuntimeError):
    pass


# ACTING 11-01（项目方 R11-8 原话：“字幕不是要求不加么?你忘记了?”；此前多次要求不加字幕）：成片永不加字幕 / 烧字。
# 硬默认，不给开关：合成链路里的 ffmpeg 调用都经过 FFmpeg.run，这里拦下字幕滤镜、drawtext、字幕流（tests/test_acting_no_subtitles.py）
NO_SUBTITLES = True
SUBTITLE_ARG_RE = re.compile(r"(?:^|[,;\[\]\s])(?:subtitles|ass|drawtext)\s*[=,;]|(?:^|[,;\]\s])drawtext$|\.(?:srt|ass|vtt|ssa)$|"
                             r"^mov_text$|^-c:s$|^-scodec$", re.I)


def assert_no_subtitles(args) -> None:
    if not NO_SUBTITLES:
        return
    for a in args:
        if SUBTITLE_ARG_RE.search(str(a)):
            raise FFmpegError(f"成片不加字幕（项目方硬规则，R11-8）：ffmpeg 参数里出现字幕 / 文字：{str(a)[:120]}")


def find_tools() -> tuple[str, str]:
    dirs: list[Path] = []
    override = os.environ.get("NS_FFMPEG")
    if override:
        p = Path(override)
        dirs.append(p if p.is_dir() else p.parent)
    found = shutil.which("ffmpeg")
    if found:
        dirs.append(Path(found).parent)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.extend(sorted(Path(local).glob(WINGET_GLOB), reverse=True))
    for d in dirs:
        ff, fp = d / f"ffmpeg{EXE}", d / f"ffprobe{EXE}"
        if ff.is_file() and fp.is_file():
            return str(ff), str(fp)
    raise FFmpegError("ffmpeg/ffprobe not found: set NS_FFMPEG or install Gyan.FFmpeg (winget)")


@dataclass
class FFmpeg:
    ffmpeg: str
    ffprobe: str
    timeout_s: float = 3600.0

    @classmethod
    def discover(cls) -> "FFmpeg":
        return cls(*find_tools())

    def run(self, args: list[str]) -> subprocess.CompletedProcess:
        assert_no_subtitles(args)
        cmd = [self.ffmpeg, "-hide_banner", "-nostdin", "-y", *[str(a) for a in args]]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=self.timeout_s, stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            raise FFmpegError(f"ffmpeg exited {proc.returncode}: {(proc.stderr or '').strip()[-1500:]}")
        return proc

    def probe(self, path: str | Path) -> dict:
        proc = subprocess.run([self.ffprobe, "-v", "error", "-of", "json", "-show_format", "-show_streams",
                               str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=120, stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            raise FFmpegError(f"ffprobe failed on {path}: {proc.stderr.strip()[-500:]}")
        return json.loads(proc.stdout or "{}")

    def info(self, path: str | Path) -> dict:
        """{duration, has_video, has_audio, width, height, fps}"""
        d = self.probe(path)
        streams = d.get("streams", [])
        v = next((s for s in streams if s.get("codec_type") == "video"), None)
        a = next((s for s in streams if s.get("codec_type") == "audio"), None)
        fps = None
        if v and v.get("r_frame_rate") and v["r_frame_rate"] != "0/0":
            n, _, den = v["r_frame_rate"].partition("/")
            fps = float(n) / float(den or 1)
        return {"duration": float(d.get("format", {}).get("duration") or 0.0), "has_video": v is not None,
                "has_audio": a is not None, "width": v.get("width") if v else None,
                "height": v.get("height") if v else None, "fps": fps,
                "video_codec": v.get("codec_name") if v else None, "audio_codec": a.get("codec_name") if a else None}

    def count_frames(self, path: str | Path) -> int:
        proc = subprocess.run([self.ffprobe, "-v", "error", "-select_streams", "v:0", "-count_packets",
                               "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)],
                              capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
        return int((proc.stdout or "0").strip().split(",")[0] or 0)


_JSON_RE = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


def parse_loudnorm(stderr: str) -> dict:
    blocks = _JSON_RE.findall(stderr or "")
    if not blocks:
        raise FFmpegError("loudnorm printed no measurement")
    return json.loads(blocks[-1])
