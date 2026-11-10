"""R4 质量 6：自动摆位图（turbo_previz 的深度控制图）修两处——多脸镜把相机拉远两档 + 默认人人朝镜头站成一列。

ep05 首帧走 turbo_previz：8 步 Turbo + Fun ControlNet 深度图，深度图由旧 tools/previz/auto_blocking.py 从分镜字段确定性算出。
构图由这张深度图定，提示词怎么改都几乎不动（r4_staging A/B：同种子四种站位写法出图几乎一样，排排站 8/12、9/12、7/12、8/12）。

旧 auto_blocking 的两个问题（ep05-S1 摆位 JSON 实测）：
1. 主体距离 d_g = max(景别距离, 宽度距离)，宽度距离按“每张脸 0.5 m 全部并排塞进 9:16 竖幅”算：
   中景 50mm 三张脸：景别 1.94 m，宽度 3.70 m → 取 3.70 m，竖向可见 2.67 m = 全身 → Q3 远两档（S1 三版全是档 5）；
   中近景 85mm 两张脸（S11）：景别 2.24 m，宽度 4.20 m。
2. 没写朝向的人默认 (±0.5, -0.85)——几乎正对镜头；横向 ±0.28 m、纵深 0.6 m 一级 → 斜着排成一列 = 排排站。

修（只在 NS2_PREVIZ_FIT=1 时替换，默认关；旧文件不动，运行时换掉模块里的 auto_blocking 并改 RENDER_VERSION → 缓存另起一份）：
  a. 宽度距离封顶为景别距离 × FIT_CAP（1.15）：后排的人可以小、可以被画框切，景别以焦点人物为准；
  b. 两张以上露脸、站位没写明朝向（正面 / 侧左 / 侧右 / 背对）的人，默认朝向改成转向画面中心的人：(±0.8, -0.6)。
其余逐行同旧 auto_blocking（RENDER_VERSION 3）。
"""
from __future__ import annotations

import math
import os
import re

FIT_CAP = 1.15
TURN = (0.8, -0.6)             # 默认朝向：侧向画面中心（旧 (0.5, -0.85)）
RENDER_TAG = "ns2r4fit1"       # 进输入哈希：新旧摆位图分开缓存，旧缓存不被覆盖


def enabled() -> bool:
    return os.environ.get("NS2_PREVIZ_FIT") == "1"


STANCE_RE = re.compile(r"背对|正面|侧右|侧左|侧前(?!景)")     # “画面右侧前景”里的“侧前”不是朝向


def explicit_stance(stance: str) -> bool:
    return bool(STANCE_RE.search(stance or ""))


def subject_distance(d_size: float, d_fit: float, cap: float = FIT_CAP) -> float:
    return round(max(d_size, min(d_fit, d_size * cap)), 2)


def make(AB):
    """→ 替换用的 auto_blocking(n, f, cast)（用旧模块 AB 的全部辅助函数与常量）"""

    def auto_blocking(n: int, f: dict, cast: dict) -> dict:
        img = f.get("img", "")
        ppl = AB.parse_people(f, cast)
        focal, T, cam_h = AB.parse_optics(f.get("光学", ""))
        size = AB.parse_size(f.get("机位与景别", ""), img)
        ang = AB.parse_angle(f.get("机位与景别", ""), [p["name"] for p in ppl])
        meta = {"shot": n, "size": size, "focal_mm": focal, "T": T, "cam_h": cam_h, "angle": {k: v for k, v in ang.items() if k != "raw"},
                "ns2_fix": RENDER_TAG}
        vis_all = [p for p in ppl if p["presence"] != "off"]
        if vis_all and all(p["presence"] == "partial" for p in vis_all):
            spec = AB.hands_shot(f, ppl, focal, cam_h)
            meta.update(focus_distance_m=round(spec["camera"]["pos"][2] - spec["camera"]["look_at"][2], 2), bg_distance_m=None)
            spec.update({"name": f"S{n} hands insert", "size": [1152, 2016], "meta": meta})
            return spec
        vis = [p for p in ppl if p["presence"] == "in"]
        faced = [p for p in vis if not p["hidden"]]
        hid = [p for p in vis if p["hidden"]]
        focus = faced or vis
        d_size = AB.VIS[size] * focal / AB.SENSOR_H
        d_fit = (0.50 * len(faced)) * focal / AB.SENSOR_W_EFF if len(faced) >= 2 else 0
        d_g = subject_distance(d_size, d_fit)                                  # 修 a
        meta.update(d_size=round(d_size, 2), d_fit=round(d_fit, 2))
        wide = lambda d, frac: frac * d * AB.SENSOR_W_EFF / focal   # noqa: E731
        actors, table_h = [], None
        if "高圆桌" in AB._text(f):
            table_h = 1.05
        depth_off = {"前": -0.6, "中": 0.0, "后": 0.6}
        d_front = d_g
        for p in vis:
            c = p["cast"]
            H = c["height"]
            if p["hidden"]:
                standing_fg = not re.search(r"蹲|坐在", p["seg"])
                d = max(0.6, d_g * (0.55 if standing_fg else 0.6))
                sx = AB.X_FRAC.get(p["x"], 0.0)
                mag = 0.48 if standing_fg else 0.42
                frac = (mag if sx >= 0 else -mag) if abs(sx) > 0 else (mag if (focus and AB.X_FRAC.get(focus[0]["x"], 0) < 0) else -mag)
            else:
                d = d_g + depth_off.get(p["depth"], 0.0)
                frac = AB.X_FRAC.get(p["x"], 0.0) * (0.6 if len(faced) == 1 else 1.0)
            d_front = min(d_front, d)
            pos = [round(wide(d, frac), 3), round(d, 3), 0.0]
            pose, ex = AB.pose_of(p["seg"], p["hidden"])
            side_sign = 1.0 if frac < 0 else -1.0
            if len(faced) >= 2 and not p["hidden"] and not explicit_stance(p["stance"]):
                fv = (TURN[0] * side_sign, TURN[1]) if frac != 0 else (0.0, -1.0)    # 修 b：侧向画面中心；正中的人照旧
            else:
                fv = AB.stance_facing(p["stance"], p["hidden"], side_sign)
            yaw_tgt = [pos[0] + fv[0], pos[1] + fv[1]]
            a = {"id": p["name"], "height": H, "pos": pos, "face_to": [round(v, 3) for v in yaw_tgt], "pose": pose}
            if c.get("hair"):
                a["hair"] = c["hair"]
            a.update(ex)
            loc = AB.hand_targets(p["seg"], 1.0, H, pose, table_h)
            if loc:
                a["wrists"] = {k: AB.to_world(v, pos, fv) for k, v in loc.items()}
            actors.append(a)
        fa = next((a for a in actors if a["id"] == (focus[0]["name"] if focus else "")), actors[0] if actors else None)
        top = AB.head_top(fa["height"], fa["pose"], fa) if fa else 1.7
        if size in ("特写", "大特写", "极近特写"):
            zc = top - 0.12
        else:
            zc = top + 0.12 - AB.VIS[size] / 2
        look = [0.0, d_g, round(zc, 3)]
        if ang["height"] == "high":
            look[2] = round(max(cam_h - d_g * 0.35, 0.6), 3)
        cam = {"pos": [0.0, 0.0, cam_h], "look_at": look, "focal_mm": focal}
        props = AB.build_props(f, d_g, d_front, {"focal": focal, "hidden": bool(hid)})
        meta.update(focus_distance_m=round(math.hypot(look[1], cam_h - look[2]), 2), bg_distance_m=round(d_g + 4.2, 2))
        return {"name": f"S{n} auto", "size": [1152, 2016], "camera": cam, "actors": actors, "props": props, "meta": meta}

    auto_blocking._ns2_fit = True
    return auto_blocking


def apply(AB=None, on: bool | None = None) -> bool:
    """换掉旧 auto_blocking 模块里的 auto_blocking + RENDER_VERSION（进输入哈希）。返回是否生效"""
    if not (enabled() if on is None else on):
        return False
    if AB is None:
        try:
            import sys
            from pathlib import Path
            p = str(Path.cwd() / "tools" / "previz")
            if p not in sys.path:
                sys.path.insert(0, p)
            import auto_blocking as AB          # noqa: N813
        except Exception:  # noqa: BLE001  旧树没有摆位模块 → 不动
            return False
    if getattr(AB.auto_blocking, "_ns2_fit", False):
        return True
    AB.auto_blocking = make(AB)
    AB.RENDER_VERSION = f"{AB.RENDER_VERSION}+{RENDER_TAG}"
    return True
