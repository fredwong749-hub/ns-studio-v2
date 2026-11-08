"""YuNet + SFace (OpenCV zoo, same pinned files as gpu3 ns_launcher VISION_WEIGHTS) — detection and identity cosine.

Model lookup order: $NS_QC_VISION_DIR, <root>/models_cache/vision, <root>/models/vision, <root>/ns-studio/models/vision.
gpu3 keeps them in ns-studio/models/vision; the laptop copy in models_cache/vision was verified against the pinned
sha256 values below. Models missing → callers report the face gates as `unchecked` (never as pass).
"""
from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import numpy as np

YUNET = "face_detection_yunet_2023mar.onnx"
SFACE = "face_recognition_sface_2021dec.onnx"
PINNED = {YUNET: "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
          SFACE: "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"}
MIN_FACE_PX = 80  # gpu3 _clip_checks: faces shorter than 80 px are not scored


def find_models(root: Path) -> tuple[Path, Path] | None:
    dirs = [os.environ.get("NS_QC_VISION_DIR"), root / "models_cache" / "vision", root / "models" / "vision",
            root / "ns-studio" / "models" / "vision"]
    for d in dirs:
        if d and (Path(d) / YUNET).is_file() and (Path(d) / SFACE).is_file():
            return Path(d) / YUNET, Path(d) / SFACE
    return None


def verify_pins(paths: tuple[Path, Path]) -> dict:
    out = {}
    for p in paths:
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        out[p.name] = h == PINNED.get(p.name)
    return out


class FaceModels:
    """Thread-safe wrapper (OpenCV DNN objects are not thread-safe — PI-26)."""

    def __init__(self, yunet: Path, sface: Path, score: float = 0.6):
        import cv2
        self._lock = threading.Lock()
        self.det = cv2.FaceDetectorYN.create(str(yunet), "", (320, 320), score, 0.3, 50)
        self.rec = cv2.FaceRecognizerSF.create(str(sface), "")

    def faces(self, bgr: np.ndarray) -> list[np.ndarray]:
        with self._lock:
            self.det.setInputSize((bgr.shape[1], bgr.shape[0]))
            _, f = self.det.detect(bgr)
        return [] if f is None else [row for row in f]

    def embed(self, bgr: np.ndarray, face: np.ndarray) -> np.ndarray:
        with self._lock:
            return self.rec.feature(self.rec.alignCrop(bgr, face)).reshape(-1).astype(np.float64)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    d = float(np.linalg.norm(a) * np.linalg.norm(b))
    return 0.0 if d == 0 else float(np.dot(a, b) / d)


def largest(faces: list[np.ndarray]) -> np.ndarray | None:
    return max(faces, key=lambda f: float(f[2]) * float(f[3])) if faces else None


# ---------------- hair (port of gpu3 weita_series.hair_colour, S3-F3) ----------------

def hair_band(bgr: np.ndarray, face) -> tuple[int, int, int, int] | None:
    x, y, fw, fh = (int(v) for v in face[:4])
    H, W = bgr.shape[:2]
    y0, y1 = max(0, y - int(0.45 * fh)), max(1, y + int(0.05 * fh))
    x0, x1 = max(0, x - int(0.2 * fw)), min(W, x + fw + int(0.2 * fw))
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


def hair_colour(bgr: np.ndarray, face) -> dict | None:
    """Same rule as gpu3 (S3-F3): blue hue 85–125 (OpenCV), S ≥30 must be ≥15 % of the band above/beside the face,
    and its median V ≥ max(70, 0.6·face V). Also returns the band's blue-pixel mean LAB for ΔE tracking."""
    import cv2
    if face is None or float(face[3]) < 60:
        return None
    box = hair_band(bgr, face)
    if box is None:
        return None
    x0, y0, x1, y1 = box
    band = bgr[y0:y1, x0:x1]
    hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(float)
    mask = (hsv[:, 0] >= 85) & (hsv[:, 0] <= 125) & (hsv[:, 1] >= 30)
    blue = hsv[mask]
    share = len(blue) / max(1, len(hsv))
    lab = cv2.cvtColor(band.astype(np.float32) / 255.0, cv2.COLOR_BGR2LAB).reshape(-1, 3)
    lab_mean = lab[mask].mean(0) if mask.any() else lab.mean(0)
    if share < 0.15:
        return {"ok": False, "why": "hair band is not blue", "blue_share": round(share, 2),
                "lab": [round(float(v), 2) for v in lab_mean]}
    x, y, fw, fh = (int(v) for v in face[:4])
    v = float(np.median(blue[:, 2]))
    fv = float(np.median(cv2.cvtColor(bgr[max(0, y):y + fh, max(0, x):x + fw], cv2.COLOR_BGR2HSV)[:, :, 2]))
    need = max(70.0, 0.6 * fv)
    return {"ok": v >= need, "value": round(v), "face_value": round(fv), "hue": round(float(np.median(blue[:, 0]))),
            "blue_share": round(share, 2), "lab": [round(float(v), 2) for v in lab_mean],
            "why": "" if v >= need else "navy / dark blue hair (owner: light ice-blue)"}


# ---------------- torso box (clothing proxy) ----------------

def torso_box(shape, face) -> tuple[int, int, int, int] | None:
    """Box below the face: x ∈ [x−0.6w, x+1.6w], y ∈ [y+1.15h, y+3.0h], clipped; None if < 40×40 px."""
    H, W = shape[:2]
    x, y, fw, fh = (float(v) for v in face[:4])
    x0, x1 = int(max(0, x - 0.6 * fw)), int(min(W, x + 1.6 * fw))
    y0, y1 = int(max(0, y + 1.15 * fh)), int(min(H, y + 3.0 * fh))
    return (x0, y0, x1, y1) if (x1 - x0) >= 40 and (y1 - y0) >= 40 else None


def centre_box(shape) -> tuple[int, int, int, int]:
    H, W = shape[:2]
    return int(0.25 * W), int(0.35 * H), int(0.75 * W), int(0.85 * H)


def ab_hist(rgb: np.ndarray, box) -> np.ndarray:
    import cv2
    x0, y0, x1, y1 = box
    lab = cv2.cvtColor(rgb[y0:y1, x0:x1].astype(np.float32) / 255.0, cv2.COLOR_RGB2LAB)
    h = cv2.calcHist([lab], [1, 2], None, [32, 32], [-80, 80, -80, 80])
    s = float(h.sum())
    return h / s if s else h


def bhattacharyya(h1: np.ndarray, h2: np.ndarray) -> float:
    import cv2
    return float(cv2.compareHist(h1.astype(np.float32), h2.astype(np.float32), cv2.HISTCMP_BHATTACHARYYA))


def dominant_lab(rgb: np.ndarray, box, k: int = 3) -> list[float]:
    """Centre of the largest k-means cluster (LAB) inside the box."""
    import cv2
    x0, y0, x1, y1 = box
    crop = rgb[y0:y1, x0:x1]
    step = max(1, int(np.sqrt(crop.shape[0] * crop.shape[1] / 4000)))
    lab = cv2.cvtColor(crop[::step, ::step].astype(np.float32) / 255.0, cv2.COLOR_RGB2LAB).reshape(-1, 3)
    k = min(k, len(lab))
    if k < 1:
        return [0.0, 0.0, 0.0]
    cv2.setRNGSeed(7)
    _, labels, centres = cv2.kmeans(lab.astype(np.float32), k, None,
                                    (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.5), 3,
                                    cv2.KMEANS_PP_CENTERS)
    big = int(np.bincount(labels.reshape(-1), minlength=k).argmax())
    return [round(float(v), 2) for v in centres[big]]
