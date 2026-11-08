"""Per-shot colour correction of MiniMax H3 FL2V clips against OUR graded keyframes (post-production, CPU).

Why (docs/research/h3_deep/00_summary.md §颜色, 03_comfy_community.md §2): ≥6 users report H3 output drifting
from its anchor keyframes — hotter/duller, contrast up, darker (768×1344 is in the HF#67 dimming zone), lilac
skin — and nobody fixed it model-side. The community workaround is a per-shot correction against a FIXED
reference (our first/last keyframes, never the neighbouring shots) with per-channel gain+bias or region LAB
statistics (one project dE 4.2→1.9); no LUTs, no hard histogram matching.

Method
  measure  decode the clip at a small analysis size (ffmpeg → rgb24), CIELAB per frame, region-weighted stats
           (default "centre": a soft Gaussian around the upper centre where the person usually is; "full" = flat).
           Per frame: mean L*, a*, b*, chroma, contrast (std L*) and dE76 of the mean colour vs the keyframes.
  plan     start map  = gain+bias per LAB channel from the first `edge_frames` frames vs the first keyframe;
           end map    = last frames vs the last keyframe. A keyframe only counts when its structure (normalised
                        luma correlation, colour-invariant) matches that end of the clip (`match_min`) — so a
                        stale/other take is never used as reference. No usable last keyframe: the end is matched
                        to the first keyframe when the last frame still shows the same picture, otherwise the
                        start map is held.
           Clamps: strength (default 0.8), gain ∈ [gain_min, gain_max], mean shift ≤ max_l_shift / max_ab_shift.
           Night/dark scenes (keyframe mean L* < dark_l): L* gain ≤ 1 and a frame is never brightened past the
           keyframe's mean L*.
  apply    per frame t: map = lerp(start, end, t/(n-1)); out = gain·LAB + bias. Identity maps leave frames
           byte-identical; when both maps are identity the output is a stream copy of the original.
  write    <clip>_cc.mp4 next to the original (never overwrites it; audio stream-copied) + <clip>_cc.json with
           the maps, references and before/after dE (after = measured on the written file).

CPU only (numpy + OpenCV + x264); no GPU, no generation.
"""

import hashlib
import json
import os
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .ffmpeg import FFmpeg, FFmpegError

ROOT = Path(os.environ.get("NS_PROJECT_ROOT") or Path(__file__).resolve().parents[3])
DATA = ROOT / "data"
CC_SUFFIX = "_cc"
METHOD = "lab_gain_bias_lerp_v1"


@dataclass(frozen=True)
class CcSettings:
    strength: float = 0.8
    region: str = "centre"  # "centre" (person-weighted proxy) | "full"
    centre_sigma: float = 0.33  # Gaussian sigma as a fraction of width/height
    centre_y: float = 0.45  # weight centre (fraction of height from the top)
    gain_min: float = 0.8
    gain_max: float = 1.25
    max_l_shift: float = 12.0
    max_ab_shift: float = 8.0
    dark_l: float = 38.0
    deadband_gain: float = 0.01  # |gain-1| and |mean shift| below these on every channel → identity map
    deadband_shift: float = 0.3  # (decode noise; dE < 0.5 is invisible)
    edge_frames: int = 3
    match_min: float = 0.85  # keyframe must BE the clip's anchor (true anchors score ~0.97+)
    end_static_min: float = 0.7  # last frame still the first keyframe's picture (static shot)
    analysis_width: int = 192
    crf: int = 14
    preset: str = "medium"
    threads: int = 8

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------- colour maths (pure) ----------------

def to_lab(rgb: np.ndarray) -> np.ndarray:
    import cv2
    return cv2.cvtColor(rgb.astype(np.float32) / 255.0, cv2.COLOR_RGB2LAB)


def to_rgb(lab: np.ndarray) -> np.ndarray:
    import cv2
    out = cv2.cvtColor(lab.astype(np.float32), cv2.COLOR_LAB2RGB)
    return np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8)


_WCACHE: dict[tuple, np.ndarray] = {}


def region_weights(h: int, w: int, s: CcSettings = CcSettings()) -> np.ndarray:
    key = (h, w, s.region, s.centre_sigma, s.centre_y)
    if key not in _WCACHE:
        if s.region == "full":
            wt = np.ones((h, w), np.float64)
        else:
            y = ((np.arange(h) + 0.5) / h - s.centre_y) / s.centre_sigma
            x = ((np.arange(w) + 0.5) / w - 0.5) / s.centre_sigma
            wt = np.exp(-0.5 * (y[:, None] ** 2 + x[None, :] ** 2))
        _WCACHE[key] = (wt / wt.sum()).astype(np.float64)
    return _WCACHE[key]


@dataclass
class LabStats:
    mean: np.ndarray  # L*, a*, b*
    std: np.ndarray
    chroma: float

    @property
    def contrast(self) -> float:
        return float(self.std[0])

    def row(self) -> dict:
        return {"L": round(float(self.mean[0]), 2), "a": round(float(self.mean[1]), 2),
                "b": round(float(self.mean[2]), 2), "chroma": round(self.chroma, 2),
                "contrast": round(self.contrast, 2)}


def lab_stats(lab: np.ndarray, s: CcSettings = CcSettings()) -> LabStats:
    wt = region_weights(lab.shape[0], lab.shape[1], s)
    x = lab.astype(np.float64)
    mu = np.einsum("hw,hwc->c", wt, x)
    var = np.einsum("hw,hwc->c", wt, (x - mu) ** 2)
    chroma = float(np.einsum("hw,hw->", wt, np.hypot(x[..., 1], x[..., 2])))
    return LabStats(mu, np.sqrt(var) + 1e-6, chroma)


def average_stats(rows: list[LabStats]) -> LabStats:
    return LabStats(np.mean([r.mean for r in rows], 0), np.mean([r.std for r in rows], 0),
                    float(np.mean([r.chroma for r in rows])))


def lerp_stats(a: LabStats, b: LabStats, t: float) -> LabStats:
    return LabStats(a.mean + t * (b.mean - a.mean), a.std + t * (b.std - a.std), a.chroma + t * (b.chroma - a.chroma))


def delta_e(a: LabStats, b: LabStats) -> float:
    """CIE76 distance between the (region-weighted) mean colours."""
    return float(np.linalg.norm(a.mean - b.mean))


def drift(frame: LabStats, ref: LabStats) -> dict:
    return {"dE": round(delta_e(frame, ref), 2), "dL": round(float(frame.mean[0] - ref.mean[0]), 2),
            "da": round(float(frame.mean[1] - ref.mean[1]), 2), "db": round(float(frame.mean[2] - ref.mean[2]), 2),
            "dChroma": round(frame.chroma - ref.chroma, 2), "dContrast": round(frame.contrast - ref.contrast, 2)}


def structure_match(lab_a: np.ndarray, lab_b: np.ndarray) -> float:
    """Colour-invariant structural similarity: Pearson correlation of blurred, downscaled L* (gain/bias drift
    leaves it unchanged; a different picture scores low)."""
    import cv2
    size = (24, 42)
    za = cv2.resize(lab_a[..., 0], size, interpolation=cv2.INTER_AREA).astype(np.float64)
    zb = cv2.resize(lab_b[..., 0], size, interpolation=cv2.INTER_AREA).astype(np.float64)
    za, zb = za - za.mean(), zb - zb.mean()
    den = np.sqrt((za ** 2).sum() * (zb ** 2).sum())
    return float((za * zb).sum() / den) if den > 1e-9 else 0.0


@dataclass
class LabMap:
    gain: np.ndarray = field(default_factory=lambda: np.ones(3))
    bias: np.ndarray = field(default_factory=lambda: np.zeros(3))

    def is_identity(self, tol_gain: float = 1e-3, tol_bias: float = 0.05) -> bool:
        return bool(np.all(np.abs(self.gain - 1) < tol_gain) and np.all(np.abs(self.bias) < tol_bias))

    @staticmethod
    def lerp(a: "LabMap", b: "LabMap", t: float) -> "LabMap":
        return LabMap(a.gain + t * (b.gain - a.gain), a.bias + t * (b.bias - a.bias))

    def to_dict(self) -> dict:
        return {"gain": [round(float(x), 4) for x in self.gain], "bias": [round(float(x), 3) for x in self.bias]}


def estimate_map(src: LabStats, ref: LabStats, s: CcSettings = CcSettings(), *, dark: bool = False) -> LabMap:
    """Per-channel gain+bias moving src's (mean, std) toward ref's, with strength and clamps."""
    g = np.clip(ref.std / src.std, s.gain_min, s.gain_max)
    if dark:
        g[0] = min(g[0], 1.0)  # never expand night-scene contrast (lifts highlights)
    k = float(np.clip(s.strength, 0.0, 1.0))
    gain = 1 + k * (g - 1)
    bias = k * (ref.mean - g * src.mean)
    shift = (gain - 1) * src.mean + bias  # resulting change of the mean
    lim = np.array([s.max_l_shift, s.max_ab_shift, s.max_ab_shift])
    bias = bias + (np.clip(shift, -lim, lim) - shift)
    shift = (gain - 1) * src.mean + bias
    if np.all(np.abs(gain - 1) < s.deadband_gain) and np.all(np.abs(shift) < s.deadband_shift):
        return LabMap()
    return LabMap(gain, bias)


def apply_map(rgb: np.ndarray, m: LabMap, s: CcSettings = CcSettings(), *, l_cap: float | None = None) -> np.ndarray:
    """Apply a LAB gain+bias map to one RGB uint8 frame. l_cap (dark scenes): the frame's weighted mean L* is
    not pushed above max(l_cap, its own mean) — never brightened past the keyframe."""
    if m.is_identity():
        return rgb
    lab = to_lab(rgb)
    bias = m.bias.copy()
    if l_cap is not None:
        mu_l = float(np.einsum("hw,hw->", region_weights(*lab.shape[:2], s), lab[..., 0]))
        out_l = m.gain[0] * mu_l + bias[0]
        ceiling = max(l_cap, mu_l)
        if out_l > ceiling:
            bias[0] -= out_l - ceiling
    out = lab * m.gain.astype(np.float32) + bias.astype(np.float32)
    out[..., 0] = np.clip(out[..., 0], 0, 100)
    return to_rgb(out)


# ---------------- plan (pure, on analysis frames) ----------------

@dataclass
class Plan:
    start: LabMap
    end: LabMap
    n: int
    first_ref: LabStats
    end_ref: LabStats | None  # stats the end was matched to (None = start map held)
    end_ref_kind: str  # "last_keyframe" | "first_keyframe" | "hold"
    dark: bool
    l_cap_start: float | None
    l_cap_end: float | None
    match_first: float
    match_last: float | None
    notes: list[str] = field(default_factory=list)
    first_lab: np.ndarray | None = None  # first keyframe at analysis size (for the comparable-window check)

    def map_at(self, i: int) -> LabMap:
        return LabMap.lerp(self.start, self.end, i / (self.n - 1) if self.n > 1 else 0.0)

    def l_cap_at(self, i: int) -> float | None:
        if self.l_cap_start is None:
            return None
        t = i / (self.n - 1) if self.n > 1 else 0.0
        return self.l_cap_start + t * ((self.l_cap_end if self.l_cap_end is not None else self.l_cap_start)
                                       - self.l_cap_start)

    def ref_at(self, i: int) -> LabStats:
        t = i / (self.n - 1) if self.n > 1 else 0.0
        return lerp_stats(self.first_ref, self.end_ref, t) if self.end_ref is not None else self.first_ref

    def summary(self) -> dict:
        return {"start": self.start.to_dict(), "end": self.end.to_dict(), "end_reference": self.end_ref_kind,
                "dark": self.dark, "match_first": round(self.match_first, 3),
                "match_last": None if self.match_last is None else round(self.match_last, 3),
                "identity": self.start.is_identity() and self.end.is_identity(), "notes": self.notes}


def make_plan(frames: list[np.ndarray], first_kf: np.ndarray, last_kf: np.ndarray | None,
              s: CcSettings = CcSettings()) -> Plan:
    """frames / keyframes: RGB uint8 at the same analysis size."""
    labs = [to_lab(f) for f in frames]
    st = [lab_stats(x, s) for x in labs]
    first_lab = to_lab(first_kf)
    first = lab_stats(first_lab, s)
    k = max(1, min(s.edge_frames, len(st)))
    notes: list[str] = []
    m_first = structure_match(labs[0], first_lab)
    dark_first = first.mean[0] < s.dark_l
    if m_first >= s.match_min:
        start = estimate_map(average_stats(st[:k]), first, s, dark=dark_first)
    else:
        start = LabMap()
        notes.append(f"frame 0 does not match the first keyframe (r={m_first:.2f}); start left uncorrected")
    end_ref, kind, m_last = None, "hold", None
    if last_kf is not None:
        last_lab = to_lab(last_kf)
        m_last = structure_match(labs[-1], last_lab)
        if m_last >= s.match_min:
            end_ref, kind = lab_stats(last_lab, s), "last_keyframe"
        else:
            notes.append(f"last frame does not match the last keyframe (r={m_last:.2f})")
    if end_ref is None:
        m_lf = structure_match(labs[-1], first_lab)
        if m_lf >= s.end_static_min:
            end_ref, kind = first, "first_keyframe"
            notes.append(f"end matched to the first keyframe (no usable last keyframe; r={m_lf:.2f})")
        else:
            notes.append(f"no reference for the end (picture changed, r={m_lf:.2f}); start map held")
    dark_end = end_ref is not None and end_ref.mean[0] < s.dark_l
    end = estimate_map(average_stats(st[-k:]), end_ref, s, dark=dark_end) if end_ref is not None else start
    dark = bool(dark_first or dark_end)
    l_cap_start = float(first.mean[0]) if dark else None
    l_cap_end = float(end_ref.mean[0]) if (dark and end_ref is not None) else l_cap_start
    return Plan(start, end, len(frames), first, end_ref, kind, dark, l_cap_start, l_cap_end, m_first, m_last, notes,
                first_lab)


def drift_report(frames: list[np.ndarray], plan: Plan, s: CcSettings = CcSettings(),
                 last_ref: LabStats | None = None) -> dict:
    """Per-frame drift of `frames` (RGB, analysis size) vs the plan's references + summary at 0 / mid / end."""
    labs = [to_lab(f) for f in frames]
    st = [lab_stats(x, s) for x in labs]
    n = len(st)
    per = {"dE_first": [round(delta_e(x, plan.first_ref), 2) for x in st],
           "dE_ref": [round(delta_e(x, plan.ref_at(i)), 2) for i, x in enumerate(st)],
           "L": [round(float(x.mean[0]), 2) for x in st], "a": [round(float(x.mean[1]), 2) for x in st],
           "b": [round(float(x.mean[2]), 2) for x in st], "chroma": [round(x.chroma, 2) for x in st],
           "contrast": [round(x.contrast, 2) for x in st]}
    if last_ref is not None:
        per["dE_last"] = [round(delta_e(x, last_ref), 2) for x in st]
    pts = {"frame0": 0, "mid": n // 2, "end": n - 1}
    summary = {k: {"frame": i, "vs_first": drift(st[i], plan.first_ref), "vs_ref": drift(st[i], plan.ref_at(i)),
                   **({"vs_last": drift(st[i], last_ref)} if last_ref is not None else {})}
               for k, i in pts.items()}
    summary["mean_dE_ref"] = round(float(np.mean(per["dE_ref"])), 2)
    if plan.first_lab is not None:
        # drift vs the first keyframe is only colour drift while the picture is still the keyframe's picture:
        # the comparable window = frames 0..k with structure match >= end_static_min (camera moves end it)
        per["match_first"] = [round(structure_match(x, plan.first_lab), 3) for x in labs]
        k = 0
        while k + 1 < n and per["match_first"][k + 1] >= s.end_static_min:
            k += 1
        win = per["dE_first"][:k + 1]
        summary["comparable"] = {"until_frame": k, "dE_at_until": win[-1],
                                 "mean_dE": round(float(np.mean(win)), 2), "max_dE": round(float(np.max(win)), 2),
                                 "at_until": drift(st[k], plan.first_ref)}
    summary["max_dE_ref"] = round(float(np.max(per["dE_ref"])), 2)
    return {"summary": summary, "per_frame": per}


def correct_frames(frames: list[np.ndarray], plan: Plan, s: CcSettings = CcSettings()) -> list[np.ndarray]:
    return [apply_map(f, plan.map_at(i), s, l_cap=plan.l_cap_at(i)) for i, f in enumerate(frames)]


# ---------------- media I/O ----------------

def _video_stream(ff: FFmpeg, path: Path) -> dict:
    v = next((x for x in ff.probe(path).get("streams", []) if x.get("codec_type") == "video"), None)
    if v is None:
        raise FFmpegError(f"{path}: no video stream")
    return v


def _matrix(v: dict) -> str:
    return "bt709" if v.get("color_space") == "bt709" else "bt601"


def iter_frames(ff: FFmpeg, path: Path, width: int | None = None):
    """Yield RGB uint8 frames (optionally downscaled to `width`, area filter)."""
    v = _video_stream(ff, path)
    w, h = int(v["width"]), int(v["height"])
    if width and width < w:
        ow, oh = width, max(2, round(h * width / w / 2) * 2)
        vf = f"scale={ow}:{oh}:flags=area:in_color_matrix={_matrix(v)}"
    else:
        ow, oh = w, h
        vf = f"scale=in_color_matrix={_matrix(v)}"
    cmd = [ff.ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-map", "0:v:0", "-vf", vf,
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL)
        size = ow * oh * 3
        try:
            while True:
                buf = proc.stdout.read(size)
                if len(buf) < size:
                    break
                yield np.frombuffer(buf, np.uint8).reshape(oh, ow, 3)
        finally:
            proc.stdout.close()
            rc = proc.wait()
        if rc != 0:
            err.seek(0)
            raise FFmpegError(f"decode {path} failed: {err.read().decode('utf-8', 'replace')[-800:]}")


def load_image(path: Path, size: tuple[int, int]) -> np.ndarray:
    """RGB uint8 resized to cover `size` (w, h) and centre-cropped (keyframes may be 1088×1920 vs 768×1344)."""
    import cv2
    from PIL import Image
    rgb = np.asarray(Image.open(path).convert("RGB"))
    w, h = size
    sc = max(w / rgb.shape[1], h / rgb.shape[0])
    rw, rh = max(w, round(rgb.shape[1] * sc)), max(h, round(rgb.shape[0] * sc))
    r = cv2.resize(rgb, (rw, rh), interpolation=cv2.INTER_AREA)
    x0, y0 = (rw - w) // 2, (rh - h) // 2
    return np.ascontiguousarray(r[y0:y0 + h, x0:x0 + w])


def cc_path(clip: Path) -> Path:
    return clip.with_name(clip.stem + CC_SUFFIX + clip.suffix)


# ---------------- keyframe discovery ----------------

def _local(p: str | None, data: Path, shot_dir: Path) -> Path | None:
    if not p:
        return None
    pp = Path(p)
    cands = [pp] if pp.is_absolute() else [data / pp, shot_dir / pp]
    posix = pp.as_posix()
    if "/data/" in posix:  # gpu3 absolute path (…/nsfs_mirror/data/renders_gpu3/…) → local mirror
        cands.append(data / posix.split("/data/", 1)[1])
    for c in cands:
        try:
            if c.is_file():
                return c
        except OSError:
            pass
    return None


def keyframe_candidates(clip: Path, data: Path = DATA) -> dict:
    """Ordered candidate first/last keyframes for a clip inside data/renders*/epXX/SN[/takes]/.
    Never the video's own extracted last frame (video_NNN_last.png / video_last.png)."""
    clip, data = Path(clip).resolve(), Path(data).resolve()
    shot_dir = clip.parent.parent if clip.parent.name == "takes" else clip.parent
    takes = {}
    if (shot_dir / "takes.json").is_file():
        try:
            takes = json.loads((shot_dir / "takes.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            takes = {}
    active = takes.get("active") or {}
    rows = takes.get("takes") or []
    stem = clip.stem.removesuffix("_4k")
    take = None
    if stem == "video":
        take = next((t for t in rows if t.get("stage") == "video" and t.get("n") == active.get("video")), None)
    else:
        take = next((t for t in rows if t.get("stage") == "video" and Path(t.get("file") or "").stem == stem), None)
    first: list[tuple[str, Path]] = []
    last: list[tuple[str, Path]] = []

    def add(lst, label, p):
        if p is not None and p.is_file() and all(p.resolve() != q.resolve() for _, q in lst):
            lst.append((label, p))

    if take:
        add(first, "take.reference", _local(take.get("reference"), data, shot_dir))
        add(last, "take.last_target", _local(take.get("last_target"), data, shot_dir))
    # fredw-92/Q1: the shot's locked keyframes (takes.json locked.image / locked.last) before the active fallbacks
    locked = takes.get("locked") or {}
    for stage, lst in (("image", first), ("last", last)):
        lt = next((t for t in rows if t.get("stage") == stage and t.get("n") == locked.get(stage)), None)
        if lt:
            add(lst, f"locked {stage} take {lt.get('n')}", _local(lt.get("file"), data, shot_dir))
    add(first, "frame.png", shot_dir / "frame.png")
    img = next((t for t in rows if t.get("stage") == "image" and t.get("n") == active.get("image")), None)
    if img:
        add(first, "active image take", _local(img.get("file"), data, shot_dir))
    if "last" in active:  # an independent last keyframe (stage "last"); otherwise last.png is the video's frame
        add(last, "last.png", shot_dir / "last.png")
    for t in sorted((t for t in rows if t.get("stage") == "last"), key=lambda t: -int(t.get("n") or 0)):
        add(last, f"last take {t.get('n')}", _local(t.get("file"), data, shot_dir))
    # older image takes: the clip may have been made from an earlier first frame (gated by structure)
    for t in sorted((t for t in rows if t.get("stage") == "image"), key=lambda t: -int(t.get("n") or 0)):
        add(first, f"image take {t.get('n')}", _local(t.get("file"), data, shot_dir))
    # the other render tree's keyframe of the same shot (only used if it structurally matches the clip)
    rel = shot_dir.relative_to(data) if shot_dir.is_relative_to(data) else None
    if rel is not None and rel.parts and rel.parts[0] in ("renders", "renders_gpu3"):
        other = data / ("renders" if rel.parts[0] == "renders_gpu3" else "renders_gpu3") / Path(*rel.parts[1:])
        add(first, f"{other.relative_to(data).as_posix()}/frame.png", other / "frame.png")
        for f in sorted((other / "takes").glob("image_*.png"), reverse=True):
            if not f.stem.endswith("_raw"):
                add(first, f"{other.relative_to(data).as_posix()}/takes/{f.name}", f)
    # V2: data/pipeline/runs/<run>/video/clips/epNN/<shot>__<tag>.mp4 -> <run>/video/jobs.json (first_frame_src, first_frame)
    vdir = clip.parent.parent.parent
    if clip.parent.parent.name == "clips" and vdir.name == "video" and "__" in clip.stem:
        shot = clip.stem.split("__", 1)[0]
        try:
            jobs = json.loads((vdir / "jobs.json").read_text(encoding="utf-8"))
            rows2 = jobs.get("shots") if isinstance(jobs, dict) else jobs
            job = next((j for j in rows2 or [] if isinstance(j, dict) and j.get("shot") == shot), None)
        except (OSError, ValueError, AttributeError):
            job = None
        if job:
            for key in ("first_frame_src", "first_frame"):
                v = job.get(key)
                if isinstance(v, str) and v:
                    pp = Path(v)
                    add(first, f"jobs.{key}", pp if pp.is_absolute() else data.parent / pp)
    return {"shot_dir": shot_dir, "take": take, "first": first, "last": last}


def pick_keyframe(cands: list[tuple[str, Path]], frame: np.ndarray, size: tuple[int, int],
                  s: CcSettings) -> tuple[str | None, Path | None, np.ndarray | None, list[dict]]:
    """First candidate whose structure matches `frame` (analysis RGB). Returns (label, path, image, tried)."""
    tried = []
    flab = to_lab(frame)
    for label, p in cands:
        img = load_image(p, size)
        r = structure_match(flab, to_lab(img))
        tried.append({"source": label, "file": _rel(p), "match": round(r, 3)})
        if r >= s.match_min:
            return label, p, img, tried
    return None, None, None, tried


def _rel(p) -> str | None:
    if p is None:
        return None
    p = Path(p)
    try:
        return p.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(p)


# ---------------- clip-level API ----------------

def measure_clip(clip: Path, first: Path | None = None, last: Path | None = None, *, ff: FFmpeg | None = None,
                 s: CcSettings = CcSettings(), data: Path = DATA) -> dict:
    """Measure drift and plan the correction (no files written). first/last override keyframe discovery.
    Returns a report dict; report["plan"] is None when no first keyframe matches the clip."""
    ff = ff or FFmpeg.discover()
    clip = Path(clip)
    frames = list(iter_frames(ff, clip, s.analysis_width))
    if not frames:
        raise FFmpegError(f"{clip}: no frames decoded")
    size = (frames[0].shape[1], frames[0].shape[0])
    kc = keyframe_candidates(clip, data)
    fc = [("--first", Path(first))] if first else kc["first"]
    lc = [("--last", Path(last))] if last else kc["last"]
    report: dict = {"clip": _rel(clip), "frames": len(frames), "analysis_size": list(size), "method": METHOD,
                    "settings": s.to_dict()}
    f_label, f_path, f_img, f_tried = pick_keyframe(fc, frames[0], size, s)
    l_label, l_path, l_img, l_tried = pick_keyframe(lc, frames[-1], size, s)
    report["keyframes"] = {"first": {"source": f_label, "file": _rel(f_path), "tried": f_tried},
                           "last": {"source": l_label, "file": _rel(l_path), "tried": l_tried}}
    if f_img is None:
        report.update(plan=None, before=None, predicted_after=None,
                      error="no first keyframe matching frame 0 found locally (pass --first)")
        return report
    plan = make_plan(frames, f_img, l_img, s)
    last_ref = lab_stats(to_lab(l_img), s) if l_img is not None else None
    report["plan"] = plan.summary()
    report["before"] = drift_report(frames, plan, s, last_ref)
    report["predicted_after"] = drift_report(correct_frames(frames, plan, s), plan, s, last_ref)
    report["_plan"] = plan  # in-memory only (stripped before writing json)
    report["_last_ref"] = last_ref
    return report


def correct_clip(clip: Path, first: Path | None = None, last: Path | None = None, *, ff: FFmpeg | None = None,
                 s: CcSettings = CcSettings(), data: Path = DATA, out: Path | None = None, log=print) -> dict:
    """Write <clip>_cc.mp4 (+ _cc.json) next to the clip. Returns the report (report['output'] = path or None)."""
    ff = ff or FFmpeg.discover()
    clip = Path(clip)
    if clip.stem.endswith(CC_SUFFIX):
        raise ValueError(f"{clip} is already a colour-corrected clip")
    out = Path(out) if out else cc_path(clip)
    if out.resolve() == clip.resolve():
        raise ValueError("refusing to overwrite the original clip")
    t0 = time.perf_counter()
    src_sha = file_sha256(clip)          # before reading frames: the _cc is bound to exactly this source
    rep = measure_clip(clip, first, last, ff=ff, s=s, data=data)
    plan: Plan | None = rep.pop("_plan", None)
    last_ref = rep.pop("_last_ref", None)
    rep["output"] = None
    if plan is None:
        log(f"  {clip.name}: skipped — {rep.get('error')}")
        return rep
    tmp = out.with_name(out.stem + ".tmp" + out.suffix)
    if rep["plan"]["identity"]:
        ff.run(["-i", clip, "-map", "0", "-c", "copy", tmp])
        rep["encode"] = "stream copy (identity correction)"
    else:
        _encode(ff, clip, plan, s, tmp)
        rep["encode"] = f"libx264 crf {s.crf} {s.preset}; audio stream copy"
    os.replace(tmp, out)
    after_frames = list(iter_frames(ff, out, s.analysis_width))
    rep["after"] = drift_report(after_frames, plan, s, last_ref)
    rep["output"] = _rel(out)
    rep["seconds"] = round(time.perf_counter() - t0, 1)
    rep["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rep["source_mtime"] = clip.stat().st_mtime
    rep["source_sha256"] = src_sha
    out.with_suffix(".json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    return rep


def _encode(ff: FFmpeg, clip: Path, plan: Plan, s: CcSettings, out: Path) -> None:
    v = _video_stream(ff, clip)
    w, h = int(v["width"]), int(v["height"])
    rate = v.get("r_frame_rate") if v.get("r_frame_rate") not in (None, "0/0") else v.get("avg_frame_rate", "24/1")
    mtx = _matrix(v)
    tags = []
    for opt, key in (("-colorspace", "color_space"), ("-color_primaries", "color_primaries"),
                     ("-color_trc", "color_transfer"), ("-color_range", "color_range")):
        if v.get(key) and v[key] != "unknown":
            tags += [opt, v[key]]
    cmd = [ff.ffmpeg, "-hide_banner", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
           "-r", rate, "-i", "-", "-i", str(clip), "-map", "0:v:0", "-map", "1:a?", "-c:a", "copy",
           "-vf", f"scale=out_color_matrix={mtx}:out_range=tv,format=yuv420p", "-c:v", "libx264",
           "-preset", s.preset, "-crf", str(s.crf), "-threads", str(s.threads), *tags,
           "-movflags", "+faststart", str(out)]
    with tempfile.TemporaryFile() as err:
        enc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err)
        try:
            for i, f in enumerate(iter_frames(ff, clip)):
                enc.stdin.write(apply_map(f, plan.map_at(i), s, l_cap=plan.l_cap_at(i)).tobytes())
        finally:
            enc.stdin.close()
            rc = enc.wait()
        if rc != 0:
            err.seek(0)
            out.unlink(missing_ok=True)
            raise FFmpegError(f"encode {out} failed: {err.read().decode('utf-8', 'replace')[-800:]}")


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_cc(clip: Path, *, ff: FFmpeg | None = None, s: CcSettings = CcSettings(), data: Path = DATA,
              log=print) -> tuple[Path | None, dict]:
    """For assembly: reuse <clip>_cc.mp4 only when its _cc.json records the same settings and the sha256 of the
    current source clip (mtime alone is not proof — a re-rendered clip can keep it), else make a new one.
    Returns (corrected path or None when skipped, report)."""
    clip = Path(clip)
    out = cc_path(clip)
    js = out.with_suffix(".json")
    if out.is_file() and js.is_file():
        try:
            rep = json.loads(js.read_text(encoding="utf-8"))
            if (rep.get("settings") == s.to_dict() and rep.get("source_sha256")
                    and rep.get("source_sha256") == file_sha256(clip)):
                return out, rep
        except (OSError, ValueError):
            pass
    rep = correct_clip(clip, ff=ff, s=s, data=data, log=log)
    return (out if rep.get("output") else None), rep


def short_summary(rep: dict, key: str) -> dict | None:
    d = rep.get(key)
    if not d:
        return None
    sm = d["summary"]
    return {"dE0": sm["frame0"]["vs_ref"]["dE"], "dE_mid": sm["mid"]["vs_ref"]["dE"],
            "dE_end": sm["end"]["vs_ref"]["dE"], "mean": sm["mean_dE_ref"], "max": sm["max_dE_ref"],
            **({"window_until": sm["comparable"]["until_frame"], "window_dE_end": sm["comparable"]["dE_at_until"],
                "window_mean": sm["comparable"]["mean_dE"]} if "comparable" in sm else {})}
