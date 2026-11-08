"""ACTING 第 2 轮（总调度 11-01 批）：MediaPipe FaceLandmarker 虹膜关键点估视线（替换 gaze.py 里“最暗像素”眼球估计）。

模型：models_cache/vision/face_landmarker.task（官方 storage.googleapis.com/mediapipe-models/face_landmarker，float16；
sha256 64184e22…c9ff）。478 点：468–472 / 473–477 为两眼虹膜；另给 52 个 blendshape（eyeLookIn/Out/Up/Down 左右）。

特征（全部按“图像右 / 下为正”）：
- yaw_n：鼻尖(1) 在左右脸缘(234 / 454) 之间的位置，−1…1（正脸 ≈ 0）
- pitch_n：鼻尖在额顶(10)—下巴(152) 之间的位置减 0.5（低头为正）
- eye_x / eye_y：两眼虹膜中心在眼角连线 / 上下眼睑之间的位置，−1…1（眼球朝右 / 朝下为正）
- bs_x / bs_y：blendshape 的眼球方向（右为正、下为正）
- gaze_x = A × yaw_n + eye_x；|gaze_x| ≤ T 且 |pitch_n| ≤ P → 疑似看镜头。A、T、P 暂定，标定（calibrate_gaze.py --score）后再定
只在项目 .venv 里有 mediapipe（不进共享环境）；没装 / 没模型 → 返回 None，调用方记“未测”。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

A, T, P = 1.0, 0.30, 0.12          # 暂定，--score 扫描后改（改动写 docs/acting/CALIBRATION.md）
MODEL = Path(__file__).resolve().parents[3] / "models_cache" / "vision" / "face_landmarker.task"
_LM = None


def landmarker():
    global _LM
    if _LM is None:
        try:
            import mediapipe as mp
            from mediapipe.tasks.python import BaseOptions
            from mediapipe.tasks.python import vision as V
        except Exception:  # noqa: BLE001
            return None
        if not MODEL.is_file():
            return None
        opt = V.FaceLandmarkerOptions(base_options=BaseOptions(model_asset_path=str(MODEL)), num_faces=4,
                                      output_face_blendshapes=True, min_face_detection_confidence=0.4)
        _LM = (mp, V.FaceLandmarker.create_from_options(opt))
    return _LM


def _ratio(p, a, b) -> float:
    """p 在 a→b 线段上的投影位置，映射到 −1…1"""
    v = b - a
    d = float(np.dot(v, v))
    return float(np.clip(2 * np.dot(p - a, v) / d - 1, -1.5, 1.5)) if d > 1e-9 else 0.0


def faces(bgr: np.ndarray) -> list[dict] | None:
    lm = landmarker()
    if lm is None:
        return None
    mp, det = lm
    H, W = bgr.shape[:2]
    rgb = np.ascontiguousarray(bgr[:, :, ::-1])
    res = det.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
    out = []
    for i, pts in enumerate(res.face_landmarks or []):
        P_ = np.array([[p.x * W, p.y * H] for p in pts])
        if len(P_) < 478:
            continue
        xs, ys = P_[:, 0], P_[:, 1]
        box = [float(xs.min()), float(ys.min()), float(xs.max() - xs.min()), float(ys.max() - ys.min())]
        # 图像左边的眼（人物右眼）：眼角 33 / 133，眼睑 159 / 145，虹膜 468；图像右边的眼：362 / 263，386 / 374，虹膜 473
        ex, ey = [], []
        for c1, c2, up, dn, ir in ((33, 133, 159, 145, 468), (362, 263, 386, 374, 473)):
            a, b = (P_[c1], P_[c2]) if P_[c1][0] < P_[c2][0] else (P_[c2], P_[c1])
            ex.append(_ratio(P_[ir], a, b))
            ey.append(_ratio(P_[ir], P_[up], P_[dn]))
        yaw_n = _ratio(P_[1], P_[234], P_[454]) if P_[234][0] < P_[454][0] else _ratio(P_[1], P_[454], P_[234])
        pitch_n = float((P_[1][1] - P_[10][1]) / max(1e-6, P_[152][1] - P_[10][1]) - 0.5)
        bs = {c.category_name: c.score for c in (res.face_blendshapes[i] if res.face_blendshapes else [])}
        # blendshape 左右：eyeLookOutLeft = 人物左眼往外（人物左 = 图像右）；统一成图像右为正
        bs_x = ((bs.get("eyeLookOutLeft", 0) - bs.get("eyeLookInLeft", 0)) - (bs.get("eyeLookOutRight", 0) - bs.get("eyeLookInRight", 0))) / 2
        bs_y = (bs.get("eyeLookDownLeft", 0) + bs.get("eyeLookDownRight", 0) - bs.get("eyeLookUpLeft", 0) - bs.get("eyeLookUpRight", 0)) / 2
        iod = float(np.linalg.norm(P_[468] - P_[473]))
        eye_x, eye_y = float(np.mean(ex)), float(np.mean(ey))
        gx = A * yaw_n + eye_x
        cam = abs(gx) <= T and abs(pitch_n - 0.0) <= P + 0.5 and abs(eye_y) <= 0.6
        out.append({"box": [round(v, 1) for v in box], "iod": round(iod, 1), "yaw_n": round(yaw_n, 3), "pitch_n": round(pitch_n, 3),
                    "eye_x": round(eye_x, 3), "eye_y": round(eye_y, 3), "bs_x": round(bs_x, 3), "bs_y": round(bs_y, 3),
                    "blink": round((bs.get("eyeBlinkLeft", 0) + bs.get("eyeBlinkRight", 0)) / 2, 3),
                    "gaze_x": round(gx, 3), "camera_gaze": bool(cam), "status": "疑似看镜头" if cam else "视线离轴"})
    return out


def face_in_box(bgr: np.ndarray, box, pad: float = 0.8, size: int = 384) -> dict | None:
    """小脸（全景 / 中景）整图跑 MediaPipe 常漏检：按 YuNet 框外扩 pad 裁出来放大到 size 再跑，取离中心最近的那张"""
    import cv2
    H, W = bgr.shape[:2]
    x, y, w, h = box
    x0, y0 = max(0, int(x - pad * w)), max(0, int(y - pad * h))
    x1, y1 = min(W, int(x + w + pad * w)), min(H, int(y + h + pad * h))
    crop = bgr[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    s = size / max(crop.shape[:2])
    crop = cv2.resize(crop, (max(1, int(crop.shape[1] * s)), max(1, int(crop.shape[0] * s))), interpolation=cv2.INTER_CUBIC)
    fs = faces(crop)
    if not fs:
        return fs if fs is None else None
    nb = [(x - x0) * s, (y - y0) * s, w * s, h * s]
    return match(fs, nb)


def match(mp_faces: list[dict], box) -> dict | None:
    """按中心点距离把 MediaPipe 的脸对到 YuNet 框（box = x, y, w, h）上"""
    if not mp_faces:
        return None
    cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
    best = min(mp_faces, key=lambda f: (f["box"][0] + f["box"][2] / 2 - cx) ** 2 + (f["box"][1] + f["box"][3] / 2 - cy) ** 2)
    bx, by = best["box"][0] + best["box"][2] / 2, best["box"][1] + best["box"][3] / 2
    return best if abs(bx - cx) < 0.6 * box[2] and abs(by - cy) < 0.6 * box[3] else None
