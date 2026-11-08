"""lens_sim v2：按真实光学标定的虚化（L-192 ③；项目方 11-05 判 v1 S4 不合格：“想想 f/1.2 的虚化、焦段不对、没有电影感”）。

默认关：只有 lens_sim 配置 enabled=true 且 optics_v2.enabled=true 才走本模块（lens_sim.apply 懒加载）；v1 路径与像素不变。

模型：
 1) 单目相对视差（Depth Anything V2 Small，0 = 最远，1 = 最近）→ 度量深度 Z。1/Z 与视差线性：
      主体距离 Zs：shot spec 直接给，或 脸框高 × 真实脸高先验（0.21 m）按针孔公式 Zs = f·H_face / (h_px·pitch)；没脸 → 默认 2.5 m；
      视差 0（最远）= Zfar（Zs + bg_extra_m，或 spec 给），1/Z = 1/Zfar + (1/Zs − 1/Zfar)·(d / d_focus)。
    （没有用度量深度模型：Depth Anything V2 Metric 室内版权重本机没有；相对视差 + 场景尺度先验在影棚单一场景里够用，局限见报告。）
 2) 薄透镜弥散圆：传感器上直径 c = f²/(N·(S−f)) · |Z−S|/Z（mm），N 取 T 值；全画幅 36×24，短边 24 mm ↔ 画面短边像素
    （1152 宽 → 0.02083 mm/px）。校验：85 mm f/1.2、S = 2.5 m、背景 7.5 m → ≈ 80 px 直径（测试里固定）。
 3) 高光：照片里的小灯已被削顶，直接散成圆斑会变灰。对每个小亮点连通域补回丢失的能量：
    增益 = hl_target·πR²/面积（上限 hl_max），散成半径 R 的圆斑后亮度 ≈ 源 × hl_target，边缘清晰成盘。
    人物层 + hl_guard 以内不扩（hl_skip_near，防白晕）；白顶帽只认小亮点。
 4) 分层散焦沿用 v1：带符号半径、归一化卷积（只用本层像素）、由远到近 over 合成；遮挡感知（Z 取近侧，occlusion_dilate）；
    person_limit 同平面人物限幅。散景核 = 圆盘 × 轻微边缘增亮（rim，定焦“边缘亮”，无洋葱环）+ 画面边缘猫眼（4 向柠檬形核按源点位置加权）
    + 可选变形竖椭圆（anamorphic>1，默认关）。
 5) 脸部对焦衰减：眼睛平面 = 焦平面；脸椭圆两侧（耳 / 颊）按 ear_depth_m 后退，额头 / 发顶略后退 → f/1.2 下耳朵比眼睛软。
 6) 前景（过肩肩膀 / 灯架）：Z < S 时同一公式（|Z−S|/Z 对近处更大），正半径，分层里按近景层最后合成。
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import cv2
import numpy as np

from . import lens_sim as LS

VERSION_V2 = "lens_sim/v2-optics"

V2_DEFAULTS: dict = {
    "enabled": False,
    "focal_mm": 85.0,           # 全画幅等效焦段（可被 shot spec 覆盖）
    "t_stop": 1.4,
    "sensor_short_mm": 24.0,    # 36×24 全画幅；竖屏 = 短边 24 mm
    "focus_distance_m": None,   # None = 由脸高估
    "default_subject_m": 2.5,
    "face_height_m": 0.21,
    "bg_distance_m": None,
    "bg_extra_m": 5.0,
    "min_distance_m": 0.35,
    "max_radius_frac": 0.062,   # 半径上限 × 短边（1152 宽 ≈ 71 px）
    "min_radius_px": 0.4,
    "hl_target": 0.85,
    "hl_max": 80.0,
    "hl_min_radius_px": 3.0,
    "rim": 0.30,
    "cat_eye": 0.45,
    "anamorphic": 1.0,          # 1 球面；1.5–2 变形宽银幕竖椭圆
    "face_falloff": {"enabled": True, "ear_depth_m": 0.09, "width": 1.25},
    "occlusion_dilate": 0.022,    # 比 v1 大：发顶 / 发丝处单目深度糊，0.008 时留下灰 / 暗斑（0.02 起消失）
    "near_ramp": [0.12, 0.40],  # 近景散焦只给视差比焦点高 >0.12 的物体（满额在 0.40）；防头发 / 肩膀暗晕
    "near_gain": 0.8,           # 前景半径折减（单目深度常把主体自己的手臂 / 肩判成“更近”，全额散焦会虚掉主体身体）；过肩前景要更虚可调到 1
}


def coc_diameter_px(focal_mm: float, t_stop: float, focus_m: float, dist_m, pitch_mm: float):
    """薄透镜弥散圆直径（像素）。dist_m 可为数组；对焦点 → 0"""
    S = float(focus_m) * 1000.0
    Z = np.maximum(np.asarray(dist_m, np.float64) * 1000.0, 1.0)
    c = focal_mm ** 2 / (t_stop * (S - focal_mm)) * np.abs(Z - S) / Z
    return (c / pitch_mm).astype(np.float32)


def pitch_mm_for(short_side: int, sensor_short_mm: float = 24.0) -> float:
    return sensor_short_mm / float(short_side)


def parse_optics_line(text: str) -> dict:
    """【光学】行 “85mm｜T1.4｜高度=1.5m 眼平｜焦点=祁念｜距离=2.5m” → focal_mm / t_stop / camera_height_m / focus_subject / focus_distance_m"""
    t = text or ""
    o: dict = {}
    m = re.search(r"(\d{2,3})\s*(?:mm|毫米)", t, re.I)
    if m:
        o["focal_mm"] = float(m.group(1))
    m = re.search(r"(?<![A-Za-z])T\s*(\d+(?:\.\d+)?)", t, re.I)
    if m:
        o["t_stop"] = float(m.group(1))
    m = re.search(r"高度\s*[=：:]?\s*(\d+(?:\.\d+)?)\s*(?:m|米)", t)
    if m:
        o["camera_height_m"] = float(m.group(1))
    m = re.search(r"焦点\s*[=：:]\s*([^｜；，。\s]+)", t)
    if m:
        o["focus_subject"] = m.group(1)
    m = re.search(r"(?:距离|对焦距离)\s*[=：:]\s*(\d+(?:\.\d+)?)\s*(?:m|米)", t)
    if m:
        o["focus_distance_m"] = float(m.group(1))
    return o


def optics_from_spec(spec: dict | None) -> dict:
    """shot spec（focal_mm / t_stop / focus_distance_m / bg_distance_m / focus_subject，或“光学”/optic 行字符串）→ optics 覆盖项"""
    spec = dict(spec or {})
    out: dict = {}
    line = spec.get("光学") or spec.get("optic") or spec.get("optics")
    if isinstance(line, str):
        out.update(parse_optics_line(line))
    for k in ("focal_mm", "t_stop", "focus_distance_m", "bg_distance_m", "focus_subject", "camera_height_m"):
        if spec.get(k) is not None:
            out[k] = spec[k]
    return out


def metric_depth(disp: np.ndarray, fi: dict, o: dict, pitch_mm: float):
    """相对视差 → 度量深度 Z（米）。返回 (Z, Zs, 记录)"""
    f = float(o["focal_mm"])
    src = "given"
    zs = o.get("focus_distance_m")
    face = fi.get("face") if fi.get("mode") == "face" else None
    if not zs and face:
        zs = f * float(o["face_height_m"]) / max(face[3] * pitch_mm, 1e-6)       # mm·m / mm = m
        src = "face_height"
    if not zs:
        zs, src = float(o["default_subject_m"]), "default"
    zs = float(np.clip(zs, 0.6, 60.0))
    zfar = float(o.get("bg_distance_m") or (zs + float(o["bg_extra_m"])))
    zfar = max(zfar, zs * 1.05)
    ds = max(float(fi.get("disp", 0.5)), 0.08)
    inv = 1.0 / zfar + (1.0 / zs - 1.0 / zfar) * (disp / ds)
    z = np.maximum(1.0 / np.maximum(inv, 1.0 / 400.0), float(o["min_distance_m"]))
    return z.astype(np.float32), zs, {"subject_m": round(zs, 2), "subject_src": src, "bg_far_m": round(zfar, 2), "disp_focus": round(ds, 3)}


def face_falloff_z(z: np.ndarray, faces: list, ff: dict) -> np.ndarray:
    """脸部深度起伏：眼睛平面 = 脸区中值深度；椭圆两侧（耳 / 颊）后退 ear_depth_m，额头 / 发顶后退一点"""
    if not ff or not ff.get("enabled") or not faces:
        return z
    h, w = z.shape
    z = z.copy()
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    for f in faces:
        cx, cy = f[0] + f[2] / 2, f[1] + f[3] * 0.45
        rx, ry = f[2] / 2 * float(ff.get("width", 1.25)), f[3] * 0.75
        e = np.hypot((xx - cx) / rx, (yy - cy) / ry)
        inside = 1 - LS._smooth(e, 0.85, 1.15)
        lat = LS._smooth(np.abs(xx - cx) / (f[2] / 2), 0.45, 1.05)
        top = 0.35 * LS._smooth((cy - yy) / (f[3] * 0.6), 0.1, 1.0)
        dz = float(ff.get("ear_depth_m", 0.09)) * np.clip(lat + top, 0, 1.3) * inside
        x0, x1 = max(0, int(f[0])), min(w, int(f[0] + f[2]) + 1)
        y0, y1 = max(0, int(f[1])), min(h, int(f[1] + f[3]) + 1)
        if x1 <= x0 or y1 <= y0:
            continue
        zf = float(np.median(z[y0:y1, x0:x1]))
        # 只改“人物深度”的像素（|Z − zf| < 0.4 m）：椭圆里露出的背景不动（早先整块替换成脸深度，在发顶外留下椭圆形暗 / 灰斑）
        person = (np.abs(z - zf) < 0.4).astype(np.float32)
        z = z + dz * person
    return z


def bokeh_kernel(r: float, rim: float = 0.0, aspect: float = 1.0, squash: float = 0.0, direction=(1.0, 0.0)) -> np.ndarray:
    """散景核：圆盘（aspect>1 竖椭圆）× 边缘增亮 rim；squash>0 → 与偏移 squash·r 的第二圆求交（猫眼 / 柠檬形）。面积归一"""
    if r <= 0.05:
        return np.ones((1, 1), np.float32)
    a = max(1.0, float(aspect or 1.0))
    rx, ry = r / math.sqrt(a), r * math.sqrt(a)
    nx, ny = int(math.ceil(rx)) + 2, int(math.ceil(ry)) + 2
    ss = 3 if r > 20 else 4
    ys, xs = np.mgrid[-ny:ny + 1, -nx:nx + 1].astype(np.float32)
    acc = np.zeros_like(xs)
    for dy in range(ss):
        for dx in range(ss):
            ox, oy = (dx + 0.5) / ss - 0.5, (dy + 0.5) / ss - 0.5
            u, v = (xs + ox) / rx, (ys + oy) / ry
            rho = np.sqrt(u * u + v * v)
            inside = rho <= 1.0
            if squash > 0:
                u2, v2 = (xs + ox - direction[0] * squash * rx) / rx, (ys + oy - direction[1] * squash * ry) / ry
                inside &= (u2 * u2 + v2 * v2) <= 1.0
            acc += inside * ((1.0 + rim * LS._smooth(rho, 0.72, 1.0)) if rim > 0 else 1.0)
    k = acc / (ss * ss)
    return (k / k.sum()).astype(np.float32)


def levels_v2(rmax: float) -> list[float]:
    lv = [0.0, 0.8]
    while lv[-1] < rmax:
        lv.append(round(lv[-1] * 1.38 + 0.2, 2))
    return sorted({-x for x in lv} | set(lv))


def layered_defocus_v2(lin: np.ndarray, r_signed: np.ndarray, o: dict):
    """分层归一化卷积（遮挡感知：每层只用本层像素；由远到近 over）+ 散景核（边缘增亮 / 猫眼 / 变形）"""
    h, w = r_signed.shape
    rmax = float(np.abs(r_signed).max())
    levels = levels_v2(rmax)
    r = np.clip(r_signed, levels[0], levels[-1])
    out = np.zeros_like(lin)
    cov = np.zeros((h, w), np.float32)
    rim, ce, an = float(o.get("rim", 0)), float(o.get("cat_eye", 0)), float(o.get("anamorphic", 1))
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    ux, uy = (xx - w / 2) / (w / 2), (yy - h / 2) / (h / 2)
    edge = LS._smooth(np.hypot(ux, uy) / math.sqrt(2), 0.25, 0.95)
    ax, ay = np.abs(ux), np.abs(uy)
    hx = ax / np.maximum(ax + ay, 1e-6)
    dirs = (("x+", hx * (ux > 0), (1.0, 0.0)), ("x-", hx * (ux <= 0), (-1.0, 0.0)),
            ("y+", (1 - hx) * (uy > 0), (0.0, 1.0)), ("y-", (1 - hx) * (uy <= 0), (0.0, -1.0)))
    used = 0
    for i, lv in enumerate(levels):
        lo = levels[i - 1] if i > 0 else lv - 1
        hi = levels[i + 1] if i + 1 < len(levels) else lv + 1
        m = np.where(r < lv, (r - lo) / max(lv - lo, 1e-6), (hi - r) / max(hi - lv, 1e-6))
        m = np.clip(m, 0, 1).astype(np.float32)
        if lv == levels[0]:
            m = np.where(r <= lv, 1.0, m).astype(np.float32)
        if lv == levels[-1]:
            m = np.where(r >= lv, 1.0, m).astype(np.float32)
        if m.max() <= 0:
            continue
        used += 1
        pack = np.concatenate([lin * m[..., None], m[..., None]], -1).astype(np.float32)     # 颜色 + 覆盖，一次卷积
        R = abs(lv)
        if R < 0.5:
            acc = pack
        elif ce > 0 and R >= 5:
            wc = np.clip(edge * ce, 0, 1)
            acc = cv2.filter2D(pack * (1.0 - wc)[..., None], -1, bokeh_kernel(R, rim, an), borderType=cv2.BORDER_REFLECT)
            for _n, wt, dvec in dirs:
                wk = (wc * wt).astype(np.float32)
                if wk.max() < 0.02:
                    continue
                sqk = float(0.55 * wk[wk > 0.02].mean() / max(wt[wk > 0.02].mean(), 1e-3))
                sqk = min(sqk, 0.6)
                acc = acc + cv2.filter2D(pack * wk[..., None], -1, bokeh_kernel(R, rim, an, sqk, dvec), borderType=cv2.BORDER_REFLECT)
        else:
            acc = cv2.filter2D(pack, -1, bokeh_kernel(R, rim, an), borderType=cv2.BORDER_REFLECT)
        a = acc[..., 3]
        c = acc[..., :3] / np.maximum(a, 1e-4)[..., None]
        a = np.clip(a, 0, 1)
        out = out * (1 - a)[..., None] + c * a[..., None]
        cov = cov * (1 - a) + a
    out = out / np.maximum(cov, 1e-4)[..., None]
    return out.astype(np.float32), {"levels": used, "r_max": round(rmax, 2)}


def boost_highlights(lin: np.ndarray, r: np.ndarray, disp: np.ndarray, o: dict, p: dict, ss: int):
    """小亮点按 面积/(πR²) 补回被削顶的能量：散成半径 R 的圆斑后亮度 ≈ 源 × hl_target。白幕大面积亮区 / 人物层不动"""
    lum = LS._lum(lin)
    peak = LS._tophat(lum, ss)
    s = LS._smooth(lum, p["hl_thr"] ** 2.2, 1.0) * np.clip((peak - 0.10) / 0.12, 0, 1)
    s = np.where(np.abs(r) >= float(o["hl_min_radius_px"]), s, 0.0).astype(np.float32)
    if p.get("hl_skip_near"):
        fg = (disp > p["hl_near_disp"]).astype(np.uint8)
        g = max(1, int(p["hl_guard"] * ss))
        fg = cv2.dilate(fg, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * g + 1, 2 * g + 1)))
        s = np.where(fg > 0, 0.0, s).astype(np.float32)
    mask = (s > 0.5).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    gain = np.ones(n, np.float32)
    for k in range(1, n):
        area = float(stats[k, cv2.CC_STAT_AREA])
        if area < 6:                                  # 白幕上的噪点 / 高光斑点不是灯
            gain[k] = 1.0
            continue
        rk = float(np.abs(r[lab == k]).mean())
        gain[k] = float(np.clip(float(o["hl_target"]) * math.pi * rk * rk / max(area, 1.0), 1.0, float(o["hl_max"])))
    gmap = cv2.dilate(gain[lab], cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    boost = 1.0 + (gmap - 1.0) * s
    return (lin * boost[..., None]).astype(np.float32), {"hl_blobs": int(n - 1), "hl_gain_max": round(float(gain.max()), 1)}


def radius_field(disp: np.ndarray, fi: dict, o: dict, p: dict, ss: int, faces: list | None = None):
    """相对视差 → (带符号半径 px, 度量深度 Z, 主体距离 Zs, 直径 px, 记录)"""
    pitch = pitch_mm_for(ss, float(o["sensor_short_mm"]))
    z, zs, zinfo = metric_depth(disp, fi, o, pitch)
    rr = int(round(float(o.get("occlusion_dilate") or 0) * ss))
    if rr >= 1:                                    # 遮挡感知：深度跳变处取近侧（Z 最小）
        z = cv2.erode(z, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * rr + 1, 2 * rr + 1)), borderType=cv2.BORDER_REPLICATE)
    z = face_falloff_z(z, faces if faces is not None else ([fi["face"]] if fi.get("face") else []), o.get("face_falloff") or {})
    dia = coc_diameter_px(float(o["focal_mm"]), float(o["t_stop"]), zs, z, pitch)
    r = (dia / 2.0) * np.where(z > zs, -1.0, float(o.get("near_gain", 1.0))).astype(np.float32)          # 远 = 负，近 = 正
    # 暗晕根因：单目深度把主体的头发 / 肩判成“比焦点略近”（r 为正），按近景层散开，暗色向外扩成一圈暗晕。
    # 近景只对“明显近得多”的物体（过肩肩膀、灯架）生效：视差比焦点高出 near_ramp[0] 以内当主体身体不散焦，到 near_ramp[1] 才满
    lo, hi = o.get("near_ramp") or (0.0, 0.0)
    if hi > lo:
        ds = max(float(fi.get("disp", 0.5)), 0.08)
        r = np.where(r > 0, r * LS._smooth(disp - ds, float(lo), float(hi)), r).astype(np.float32)
    cap = float(o["max_radius_frac"]) * ss
    r = np.clip(r, -cap, cap)
    r = np.where(np.abs(r) < float(o["min_radius_px"]), 0.0, r).astype(np.float32)
    pr = LS.person_ratio_field(disp, fi, p.get("person_limit") or {})
    if pr is not None:
        r = (r * pr).astype(np.float32)
    zinfo["pitch_mm"] = round(pitch, 5)
    return r, z, zs, dia, zinfo


def lens_array_v2(y: np.ndarray, disp: np.ndarray, p: dict, fi: dict, faces: list | None = None):
    p = {**LS.DEFAULTS, **(p or {})}
    o = {**V2_DEFAULTS, **(p.get("optics_v2") or {})}
    h, w = y.shape[:2]
    ss = min(h, w)
    r, _z, _zs, dia, zinfo = radius_field(disp, fi, o, p, ss, faces)
    lin = np.power(np.clip(y, 0, 1), 2.2).astype(np.float32)
    lin, hinfo = boost_highlights(lin, r, disp, o, p, ss)
    out, dinfo = layered_defocus_v2(lin, r, o)
    out = LS.soft_clip(np.power(np.clip(out, 0, None), 1 / 2.2))
    out = np.where((r == 0)[..., None], y, out).astype(np.float32)
    out = LS.halation(out, p.get("halation") or {}, ss)
    out = LS.chromatic(out, p["ca"])
    out = LS.vignette(out, p["vignette"])
    bg = r < -0.5
    rec = {"version": VERSION_V2, "optics": {k: v for k, v in o.items() if k != "enabled"}, "focus": fi, "metric": zinfo,
           "coc_bg_diam_px": round(float(dia[bg].mean()) if bg.any() else 0.0, 1), "coc_max_diam_px": round(float(dia.max()), 1),
           **hinfo, **dinfo, "radius": {"bg_frac": round(float(bg.mean()), 3), "near_frac": round(float((r > 0.5).mean()), 3)}}
    return np.clip(out, 0, 1).astype(np.float32), rec


def apply_v2(im, p: dict, *, depth: np.ndarray | None = None, depth_out: Path | None = None, spec: dict | None = None):
    """PIL → (PIL, 记录)。spec（shot spec：focal_mm / t_stop / focus_distance_m / 光学行）覆盖 optics_v2 配置"""
    from PIL import Image
    p = {**LS.DEFAULTS, **(p or {})}
    o = {**V2_DEFAULTS, **(p.get("optics_v2") or {}), **optics_from_spec(spec or p.get("spec"))}
    p = {**p, "optics_v2": o}
    rgb = np.asarray(im.convert("RGB"))
    d = LS.prepare_depth(rgb, p, depth)
    if depth_out is not None:
        Path(depth_out).parent.mkdir(parents=True, exist_ok=True)
        np.save(str(depth_out), d)
    faces = LS.detect_faces(rgb) if p["focus"] in ("auto", "all_faces") else []
    fi = LS.pick_focus(d, faces, p["focus"], p["dof_band"], p["all_faces_min_ratio"])
    y = rgb.astype(np.float32) / 255.0
    out, rec = lens_array_v2(y, d, p, fi, faces)
    rec.update(enabled=True, input_sha16=LS._sha16(rgb), depth_sha16=LS._sha16((d * 65535).astype(np.uint16)))
    return Image.fromarray((out * 255 + 0.5).astype(np.uint8)), rec
