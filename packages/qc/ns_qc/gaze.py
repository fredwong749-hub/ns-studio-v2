"""ACTING（2026-11-01，R11-2 / R11-6 / R11-8 / R11-9）：看镜头 / 首秒定格 / 字幕带——纯像素检测，只记录不阻断（标定前）。

项目方原话：“演员的眼睛不要看镜头”；“每个镜头第一帧,演员都看着镜头,而且停顿1秒多,这个是不合理的”；“字幕不是要求不加么?”。

方法（OpenCV zoo YuNet 2023mar 五点：右眼、左眼、鼻尖、右嘴角、左嘴角；与 ns_qc.face 同一模型）：
- 头部朝向：yaw_n = 鼻尖到两眼中点的水平偏移 / 眼距（正脸 ≈ 0，侧脸 |yaw_n| ≥ 0.25）；
  pitch_n = 鼻尖到眼线的垂直距离 / 眼线到嘴线距离（正视 ≈ 0.5–0.65，低头变大、抬头变小）
- 眼球：每只眼按眼距取小框（宽 0.42 眼距、高 0.24 眼距），灰度最暗 18% 像素的质心 → 相对框中心的水平偏移 iris_dx（−1…1）
- 视线水平分量 gaze_x = YAW_K × yaw_n + iris_dx（两者都是“朝画面右为正”）；|gaze_x| 小且 pitch 正常 = 疑似看镜头
- 眼距 < MIN_IOD 像素（脸太小）或没检出脸 → 状态“未测”，不算通过也不算不通过（ITERATION_PROTOCOL H3：未测 ≠ 通过）

阈值是“暂定”（PROVISIONAL），只用于记录；标定（tools/acting/calibrate_gaze.py，总调度标注）之后才改 BLOCK=True。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

# ---- 暂定阈值（标定前只记录；改动须同时更新 docs/acting/CALIBRATION.md）
PROVISIONAL = True
BLOCK = False                 # 标定能把好 / 坏分开后由总调度批准再改 True
MIN_IOD = 22.0                # 眼距像素下限（再小眼球框不足 9 px，测不准 → 未测）
YAW_K = 1.6
CAM_GAZE_X = 0.22             # |gaze_x| ≤ 此值
CAM_PITCH = (0.38, 0.80)      # 且 pitch_n 在此区间（不是明显低头 / 抬头）
FIRST_S = 1.0                 # “首秒”窗口
FREEZE_RATIO = 0.35           # 首秒帧差 < 0.35 × 后段中位帧差 → 首秒定格
FREEZE_ABS = 0.6              # 且首秒平均帧差 < 0.6（0–255 灰度、160 px 宽缩略图）
STILL_ABS = 1.2               # 逐帧“静止”= 帧差 < min(0.35 × 后段中位, 1.2)
FREEZE_RUN_S = 0.5            # 从第 0 帧起连续静止 ≥ 0.5 s → 开头定格（项目方：“停顿 1 秒多”）
# ---- L-200 排排站（W-DETECT 11-06 几何判：看图模型对 ep01 S1 / S14 / S15 的合影式并排全部漏判）
# 两张（含）以上测得的脸两两满足：脸高比 ≥ size_ratio（同一纵深）、脸心纵向差 ≤ dy（同一排）、横向间距 ≥ dx_k × 大脸高（肩并肩不是叠着）、
# 眼距 / 脸宽 ≥ iod_w（朝向镜头，不是侧脸）、|yaw_n| ≤ yaw（不是明显转头）。标定（data/_offline_pull ep01 11-06）：
# 正例 S1 v3 / S14 v2 / S15 v12 / S14 v11（三人一排、两人并排）4/4 命中；反例过肩镜 S4 / S5 / S12、前后错落 S7 / S9、单人 共 14 张 0 误报
LINEUP = {"size_ratio": 0.72, "dy": 0.08, "dx_k": 1.2, "iod_w": 0.26, "yaw": 0.6, "min_faces": 2}


def _models(root: Path | None = None):
    from . import face as QF
    root = root or Path(__file__).resolve().parents[3]
    paths = QF.find_models(root)
    if not paths:
        return None
    return QF.FaceModels(*paths, score=0.6)


def _iris_dx(gray: np.ndarray, eye: tuple[float, float], iod: float, axis: np.ndarray) -> float | None:
    """眼框内最暗 18% 像素质心相对眼框中心的偏移，投影到眼线方向（axis，单位向量，图像右为正）→ −1…1"""
    w, h = 0.42 * iod, 0.24 * iod
    x0, x1 = int(round(eye[0] - w / 2)), int(round(eye[0] + w / 2))
    y0, y1 = int(round(eye[1] - h / 2)), int(round(eye[1] + h / 2))
    H, W = gray.shape
    if x0 < 0 or y0 < 0 or x1 > W or y1 > H or x1 - x0 < 6 or y1 - y0 < 4:
        return None
    patch = gray[y0:y1, x0:x1].astype(np.float32)
    thr = np.percentile(patch, 18)
    ys, xs = np.nonzero(patch <= thr)
    if len(xs) < 3:
        return None
    cx, cy = xs.mean() - (patch.shape[1] - 1) / 2, ys.mean() - (patch.shape[0] - 1) / 2
    return float(np.clip((cx * axis[0] + cy * axis[1]) / (w / 2), -1, 1))


def face_gaze(gray: np.ndarray, f) -> dict:
    """单张脸（YuNet 一行）→ 特征 + 判定"""
    re_, le = np.array(f[4:6], float), np.array(f[6:8], float)
    nose = np.array(f[8:10], float)
    mr, ml = np.array(f[10:12], float), np.array(f[12:14], float)
    v = le - re_
    iod = float(np.linalg.norm(v))
    out = {"box": [round(float(x), 1) for x in f[:4]], "det": round(float(f[14]), 3), "iod": round(iod, 1)}
    if iod < 1:
        return {**out, "status": "未测", "why": "眼点重合"}
    axis = v / iod
    if axis[0] < 0:                      # 统一成“图像右为正”
        axis = -axis
    normal = np.array([-axis[1], axis[0]])
    if normal[1] < 0:
        normal = -normal                 # 朝下为正
    em, mm = (re_ + le) / 2, (mr + ml) / 2
    yaw_n = float(np.dot(nose - em, axis) / iod)
    em_mm = float(np.dot(mm - em, normal))
    pitch_n = float(np.dot(nose - em, normal) / em_mm) if em_mm > 1 else None
    out.update(yaw_n=round(yaw_n, 3), pitch_n=None if pitch_n is None else round(pitch_n, 3))
    if iod < MIN_IOD:
        return {**out, "status": "未测", "why": f"脸太小（眼距 {iod:.0f}px < {MIN_IOD:.0f}）"}
    dx = [d for d in (_iris_dx(gray, tuple(re_), iod, axis), _iris_dx(gray, tuple(le), iod, axis)) if d is not None]
    if not dx:
        return {**out, "status": "未测", "why": "眼框越界"}
    iris = float(np.mean(dx))
    gx = YAW_K * yaw_n + iris
    cam = abs(gx) <= CAM_GAZE_X and pitch_n is not None and CAM_PITCH[0] <= pitch_n <= CAM_PITCH[1]
    return {**out, "iris_dx": round(iris, 3), "gaze_x": round(gx, 3), "camera_gaze": bool(cam),
            "status": "疑似看镜头" if cam else "视线离轴"}


def image_gaze(bgr: np.ndarray, models=None, min_face_h: float = 0.0) -> dict:
    """整张图 → {status, faces:[…]}。status：疑似看镜头（任一测得的脸）/ 视线离轴 / 未测"""
    import cv2
    models = models or _models()
    if models is None:
        return {"status": "未测", "why": "没有 YuNet 模型（models_cache/vision）", "faces": []}
    faces = [f for f in models.faces(bgr) if float(f[3]) >= min_face_h * bgr.shape[0]]
    if not faces:
        return {"status": "未测", "why": "没检出脸", "faces": []}
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    rows = sorted((face_gaze(gray, f) for f in faces), key=lambda r: -r["box"][3])
    measured = [r for r in rows if r["status"] != "未测"]
    if not measured:
        return {"status": "未测", "why": "；".join(sorted({r.get("why", "") for r in rows})), "faces": rows}
    st = "疑似看镜头" if any(r["camera_gaze"] for r in measured) else "视线离轴"
    H, W = bgr.shape[:2]
    return {"status": st, "faces": rows, "measured": len(measured), "provisional": PROVISIONAL, "lineup": lineup_geo(rows, W, H)}


def lineup_geo(rows: list[dict], W: int, H: int, thr: dict | None = None) -> dict:
    """L-200 排排站几何判（见 LINEUP）：rows = face_gaze 行（box=[x,y,w,h] 像素、iod、yaw_n、status）。
    → {"lineup": bool, "n": 同一排的人数, "pairs": [[i, j], …], "thr": 阈值}；只有测得的脸（status != 未测）参与"""
    t = {**LINEUP, **(thr or {})}
    feats = []
    for i, r in enumerate(rows or []):
        if r.get("status") == "未测" or not r.get("box") or not W or not H:
            continue
        x, y, w, h = [float(v) for v in r["box"][:4]]
        if w <= 0 or h <= 0:
            continue
        feats.append((i, (x + w / 2) / W, (y + h / 2) / H, h / H, float(r.get("iod") or 0) / w, abs(float(r.get("yaw_n") or 0))))
    pairs, members = [], set()
    for a in range(len(feats)):
        for b in range(a + 1, len(feats)):
            ia, cxa, cya, ha, iwa, ya = feats[a]
            ib, cxb, cyb, hb, iwb, yb = feats[b]
            big = max(ha, hb)
            if (min(ha, hb) / big >= t["size_ratio"] and abs(cya - cyb) <= t["dy"] and abs(cxa - cxb) >= t["dx_k"] * big
                    and min(iwa, iwb) >= t["iod_w"] and max(ya, yb) <= t["yaw"]):
                pairs.append([ia, ib])
                members |= {ia, ib}
    return {"lineup": len(members) >= int(t["min_faces"]) and bool(pairs), "n": len(members), "pairs": pairs, "thr": t}


def _thumb(frame: np.ndarray) -> np.ndarray:
    import cv2
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.resize(g, (160, max(1, int(160 * g.shape[0] / g.shape[1]))), interpolation=cv2.INTER_AREA).astype(np.float32)


def caption_band(bgr: np.ndarray) -> list[tuple[float, float]]:
    """下 38% 画面里像“一行字”的横条（高对比细笔画、宽高比 ≥ 4、高 1.2–7% 画高）→ [(y 中心比例, 宽比例)]"""
    import cv2
    H, W = bgr.shape[:2]
    y0 = int(H * 0.62)
    g = cv2.cvtColor(bgr[y0:], cv2.COLOR_BGR2GRAY)
    grad = cv2.morphologyEx(g, cv2.MORPH_GRADIENT, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    _, bw = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, W // 60), 1)))
    n, _lab, stats, _c = cv2.connectedComponentsWithStats(bw)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if h < 0.012 * H or h > 0.07 * H or w < 4 * h or w < 0.2 * W:
            continue
        fill = area / float(w * h)
        cx = (x + w / 2) / W
        if 0.3 <= fill <= 0.85 and 0.3 <= cx <= 0.7:     # 字幕居中、笔画间有空隙
            out.append((round((y0 + y + h / 2) / H, 3), round(w / W, 3)))
    return out


def video_checks(path: str | Path, models=None, gaze_every_s: float = 0.5) -> dict:
    """视频 → 首秒定格 / 首秒看镜头 / 全片看镜头比例 / 疑似字幕带。全部只记录（BLOCK=False）"""
    import cv2
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    models = models or _models()
    diffs, prev, gz, caps, t = [], None, [], [], 0
    i = 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        ts = i / fps
        th = _thumb(fr)
        if prev is not None:
            diffs.append((ts, float(np.abs(th - prev).mean())))
        prev = th
        if ts <= FIRST_S + 1e-6 and i % max(1, int(round(fps / 4))) == 0 or (ts > FIRST_S and ts >= t):
            if models is not None:
                r = image_gaze(fr, models)
                gz.append({"t": round(ts, 2), "status": r["status"],
                           "gaze_x": [f.get("gaze_x") for f in r["faces"] if "gaze_x" in f][:3]})
            cb = caption_band(fr)
            if cb:
                caps.append({"t": round(ts, 2), "bands": cb[:3]})
            if ts > FIRST_S:
                t = ts + gaze_every_s
        i += 1
    cap.release()
    dur = i / fps if i else 0.0
    head = [d for ts, d in diffs if ts <= FIRST_S]
    rest = [d for ts, d in diffs if ts > FIRST_S]
    head_mean = float(np.mean(head)) if head else None
    rest_med = float(np.median(rest)) if rest else None
    still_run = 0.0
    if rest_med:
        for ts, d in diffs:
            if d < min(FREEZE_RATIO * rest_med, STILL_ABS):
                still_run = ts
            else:
                break
    freeze = None
    if head_mean is not None and rest_med:
        freeze = bool((head_mean < FREEZE_RATIO * rest_med and head_mean < FREEZE_ABS) or still_run >= FREEZE_RUN_S)
    first = [g for g in gz if g["t"] <= FIRST_S]
    fm = [g for g in first if g["status"] != "未测"]
    allm = [g for g in gz if g["status"] != "未测"]
    first_cam = (sum(g["status"] == "疑似看镜头" for g in fm) / len(fm)) if fm else None
    cam_frac = (sum(g["status"] == "疑似看镜头" for g in allm) / len(allm)) if allm else None
    # 字幕带：同一高度（±2% 画高）在 ≥3 个采样点出现
    ys = [b[0] for c in caps for b in c["bands"]]
    stable = 0
    if ys:
        ys_ = np.array(ys)
        stable = int(max(((np.abs(ys_ - y) <= 0.02).sum() for y in ys_)))
    return {
        "duration_s": round(dur, 2), "provisional": PROVISIONAL, "block": BLOCK,
        "first_sec": {"motion_mean": None if head_mean is None else round(head_mean, 3),
                      "rest_motion_median": None if rest_med is None else round(rest_med, 3),
                      "still_from_start_s": round(still_run, 2), "freeze": freeze,
                      "camera_gaze_frac": None if first_cam is None else round(first_cam, 2),
                      "status": ("未测" if freeze is None and first_cam is None else
                                 "疑似开头定格看镜头" if (freeze and (first_cam or 0) >= 0.5) else
                                 "疑似开头定格" if freeze else
                                 "疑似开头看镜头" if (first_cam or 0) >= 0.5 else "开头在动")},
        "camera_gaze": {"frac": None if cam_frac is None else round(cam_frac, 2), "measured": len(allm), "sampled": len(gz),
                        "status": "未测" if cam_frac is None else ("疑似看镜头" if cam_frac >= 0.3 else "视线离轴")},
        "caption": {"suspect": stable >= 3, "hits": stable, "samples": caps[:6]},
        "gaze_samples": gz[:40],
    }
