"""本地图片异常检测 P1（DETECT-S，CONSISTENCY_PLAN §4）：人数 / 头数 / 镜像 / 同一人出现两次 / 手脚。**只记录，不拦截**。

分两半：
- 远端测量（REMOTE_SRC，在哪张卡上跑由 device_prefix(host) 定：gpu36 = GPU2 打分道，其他主机 CPU，坏卡绝不用）：person_yolov8m-seg、face_yolov8m、hand_yolov9c、
  insightface buffalo_l（头 + ArcFace 向量，CPU）、DWPose（133 点）、镜面分割（OneFormer ADE20K，模型在才跑，不在 = 标“未做镜面分割”）。
  只出测量值（框 / 向量 / 关键点），不做判定。
- 本地判定 judge()：纯函数，输入测量值 + cast，输出缺陷列表。阈值全在 TH。

判定口径
  头：脸框 ≥ min_face_px 才算头。头落在镜面区域（脸框 ≥50% 在镜面 mask 内）= 镜中头。
  人数：非镜中头数 ＞ cast 里“在画”的人数 → 多头；其中两个头同一人（同一角色 cos ≥ same_cos，或头间 cos ≥ pair_cos）→ duplicate_person，否则 extra_head。
  镜中头：必须和在场某个人 ArcFace 相似度高（≥ mirror_cos）才放行为镜像；否则 extra_head（镜里是陌生人）。
  没有镜面分割时 mirror_seg="未做镜面分割"，照常判（镜像场景会误报，结果里带这个标记，不能当最终判）。
  手：每个人（DWPose 人框）归属的手检测框（NMS 后）≤ 2，全图手框 ≤ 2 × 人数；手指骨长 / 掌宽比例沿用 V6（0.25–2.5）。
  脚：DWPose 每人脚点按左 / 右各算一只，结构上 ≤ 2；额外查脚点簇（同侧两簇相距 > 0.5 个脚长）记入 feet_dup。

用法
    python tools/vqa/anomaly.py <png> [--cast 林晚,陈宇] [--host gpu36]       # 单图（cast 缺省 = 不比人数）
    python tools/vqa/anomaly.py --shots ep04-S10 ep02-S1 …                      # 按镜头取当前版本首帧 + cast
    python tools/vqa/anomaly.py --scan --raw res.json                           # 全剧当前版本首帧 / 尾帧；页面用 tools/vqa/anomaly_page.py
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

TH = {
    "min_face_px": 28,          # 脸框高度（原图像素）低于它不算头（背景小脸只记录）
    "face_det": 0.5,
    "same_cos": 0.40,           # 头对某角色身份脸 ArcFace ≥ 它 = 认得出是该角色
    "pair_cos": 0.50,           # 两个头之间 ArcFace ≥ 它 = 同一个人
    "mirror_cos": 0.40,         # 镜中头对在场某头的相似度 ≥ 它 = 镜像
    "mirror_cover": 0.5,        # 脸框被镜面 mask 覆盖比例 ≥ 它 = 在镜面区域
    "person_conf": 0.4,
    "hand_conf": 0.35,
    "hand_nms_iou": 0.45,
    "kp_conf": 0.3,
    "finger_lo": 0.25,          # V6 口径：手指总长 / 掌宽
    "finger_hi": 2.5,
    "small_face_frac": 0.045,   # cast 没写路人行时，脸高 < 画面高 x 它的后景头不算（总调度定 4%；ep08-S3 的走廊头实测 4.08%，故取 4.5%）
    "hand_wrist_reach": 1.0,    # 手框中心到腕点距离 <= 手框长边 x 它，才把这只手归给这个人
    "hand_merge_iou": 0.2,      # 手框交叠 IoU >= 它（或一个中心落在另一个框内）= 同一只手，不重复计
    "stranger_cos": 0.30,       # 头对所有角色身份脸的最高 ArcFace 都低于它 = 陌生人（cast 里没有路人行时判 extra_head）
    "mirror_size_ratio": 1.6,   # 几何镜像：两脸高度比 <=
    "mirror_dy": 0.25,          # 几何镜像：两脸中心高度差 <= 0.25 x 脸高
    "mirror_yaw_sum": 0.15,     # 几何镜像：两脸偏航代理绝对值之和 >=（都正脸时不算）
    "red_cord_lo": 0.008,       # 标志饰品：手腕窗口里红色像素占比在 [lo, hi]（细绳占比小，大块红 = 红衣服）
    "red_cord_hi": 0.15,
    "red_cord_span": 0.3,       # 最大红色连通块包围盒（长边）/ 窗口边长 >=
    "red_cord_fill": 0.30,      # 最大红色连通块面积 / 包围盒面积 <=（细环填充率低）
    "max_hands_per_person": 2,
    "max_feet_per_person": 2,
}
SIGNATURE_KEYWORD = "红绳"      # 标志饰品按“造型登记”判（总调度 11-02）：这个人本镜的造型饰品里登记了红绳才算对；不另写归属表
NAME_ID = {"林晚": "lin_wan", "顾沉": "gu_chen", "陈宇": "chen_yu", "陈阿兰": "ch_a_lan"}


def _ids() -> dict:
    """当前剧的 {人物名 / id: 角色 id}（OVERSIGHT-F 第 6 轮 P2：原来写死 V1 的 NAME_ID，V2 角色全被当成“没有身份参照的路人”，
    陌生人判据整个跳过）。资产包（清单）模式的剧 → 清单 characters（id 本身 + display_name）；否则 V1 的 NAME_ID。
    当前剧解析失败 → 空表（全部算未登记，judge 会判 untested，不退回 V1 名单）"""
    try:
        sys.path[:0] = [str(ROOT / "services/agent"), str(ROOT / "packages/schema"), str(ROOT / "tools/gen_contract")]
        import remote_score as RS
        from ns_agent import asset_refs as AR
        pj = RS.face_project()
        if AR.manifest_rel(pj):
            ch = (AR.manifest(pj) or {}).get("characters") or {}
            out = {k: k for k in ch}
            out.update({v.get("display_name"): k for k, v in ch.items() if isinstance(v, dict) and v.get("display_name")})
            return out
    except Exception as e:  # noqa: BLE001 — 解析不了当前剧：返回空表并显式报（下游判 untested），不退回 V1 名单
        print(f"[anomaly._ids] 当前剧人物清单解析失败：{e!r}"[:200], file=sys.stderr)
        return {}
    return dict(NAME_ID)


# ───────────────────────── 几何小工具（纯函数） ─────────────────────────
def _iou(a, b) -> float:
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / u if u > 0 else 0.0


def _cos(a, b) -> float:
    if not a or not b:
        return 0.0
    s = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return s / (na * nb) if na and nb else 0.0


def mirror_cover(box, mask) -> float:
    """脸框被镜面 mask 覆盖的比例。mask = {"w","h","rows":[“0101…” …]}（缩小的栅格），box 用原图像素，mask["W"],["H"] 为原图尺寸"""
    if not mask or not mask.get("rows"):
        return 0.0
    gw, gh = len(mask["rows"][0]), len(mask["rows"])
    sx, sy = gw / mask["W"], gh / mask["H"]
    x1, x2 = max(0, int(box[0] * sx)), min(gw, max(int(box[0] * sx) + 1, int(box[2] * sx)))
    y1, y2 = max(0, int(box[1] * sy)), min(gh, max(int(box[1] * sy) + 1, int(box[3] * sy)))
    tot = hit = 0
    for y in range(y1, y2):
        row = mask["rows"][y]
        for x in range(x1, x2):
            tot += 1
            hit += row[x] == "1"
    return hit / tot if tot else 0.0


def finger_bad(hand_kp, conf=None) -> bool | None:
    """V6 口径：手指总长 < 0.25 × 掌宽 或 > 2.5 × 掌宽 = 异常；点不全（score 低）返回 None（测不了）"""
    conf = TH["kp_conf"] if conf is None else conf
    if not hand_kp or len(hand_kp) < 21 or min(p[2] for p in hand_kp) < conf:
        return None
    p = [(q[0], q[1]) for q in hand_kp]
    palm = ((p[5][0] - p[17][0]) ** 2 + (p[5][1] - p[17][1]) ** 2) ** 0.5 + 1e-6
    for a, b in ((1, 4), (5, 8), (9, 12), (13, 16), (17, 20)):
        seg = sum(((p[k + 1][0] - p[k][0]) ** 2 + (p[k + 1][1] - p[k][1]) ** 2) ** 0.5 for k in range(a, b))
        if seg < TH["finger_lo"] * palm or seg > TH["finger_hi"] * palm:
            return True
    return False


def nms(boxes: list[dict], iou: float) -> list[dict]:
    out = []
    for b in sorted(boxes, key=lambda x: -x.get("conf", 0)):
        if all(_iou(b["box"], o["box"]) < iou for o in out):
            out.append(b)
    return out


def cast_info(cast: list[dict] | None) -> dict:
    """cast = [{name, presence, part, faced}] → 应露脸 / 可能露脸 / 入画人数。
    faced（可选）= 编译稿解析结果（framing.faced_people：在画，或局部但原文写到脸 / 头发）；缺省时只认 presence == 在画。
    局部（只露手 / 肩 / 臂）、背影的人不算应露脸，但给头数留 1 个名额（局部者的脸出没出由 Q1 管）。"""
    if cast is None:
        return {"faced": [], "n_faced": None, "n_in_frame": None, "known": False}

    def back(c):
        return c.get("presence") == "背影" or c.get("part") == "背影"

    def is_faced(c):
        if back(c):
            return False
        if "faced" in c:
            return bool(c["faced"])
        return c.get("presence") == "在画"
    faced = [_ids().get(c.get("name"), c.get("name")) for c in cast if is_faced(c)]
    partial = [c for c in cast if c.get("presence") in ("在画", "局部", "背影") and not is_faced(c)]
    inframe = [c for c in cast if c.get("presence") in ("在画", "局部", "背影")]
    known_ids = set(_ids().values())
    # W-ASSETS 11-06：cast 行可带 refless=True（身份脸候选作业：这个人就是还没有身份脸，按“没有身份参照的人”只数头，
    # 不判陌生人、不因缺参照记 untested）。原来 face-ch_* 的候选张张 count=untested（在场角色的身份脸缺），ep02 被挡两天
    refless = [c for c in cast if c.get("presence") != "画外"
               and (c.get("refless") or _ids().get(c.get("name"), c.get("name")) not in known_ids)]
    return {"faced": faced, "n_faced": len(faced), "n_partial": len(partial), "n_in_frame": len(inframe), "known": True,
            "has_back": any(back(c) for c in cast), "refless": len(refless),
            "in_frame_ids": [_ids().get(c.get("name"), c.get("name")) for c in inframe]}


def yaw_proxy(kps) -> float | None:
    """5 点关键点（左眼、右眼、鼻…）→ 偏航代理：鼻子相对两眼中点的水平偏移 / 眼距；缺点返回 None"""
    if not kps or len(kps) < 3:
        return None
    lx, rx, nx = kps[0][0], kps[1][0], kps[2][0]
    d = abs(rx - lx)
    return (nx - (lx + rx) / 2) / d if d > 1e-6 else None


def mirror_pair(a: dict, b: dict, th: dict, vlines: list | None = None) -> bool:
    """几何镜像判据（总调度 11-02，镜面分割没盖到时补充）：同身份的两张脸，同一水平高度、大小相近，
    且两脸之间隔着一条贯穿画面的竖线（镜框 / 门框 = 竖直镜面）。竖线缺测时退回“朝向左右相反”（偏航代理）。"""
    ha, hb = a["box"][3] - a["box"][1], b["box"][3] - b["box"][1]
    if min(ha, hb) <= 0 or max(ha, hb) / min(ha, hb) > th["mirror_size_ratio"]:
        return False
    dy = abs((a["box"][1] + a["box"][3]) / 2 - (b["box"][1] + b["box"][3]) / 2)
    if dy > th["mirror_dy"] * max(ha, hb):
        return False
    ca, cb = (a["box"][0] + a["box"][2]) / 2, (b["box"][0] + b["box"][2]) / 2
    lo, hi = min(ca, cb), max(ca, cb)
    if vlines is not None and any(lo < x < hi for x in vlines):
        return True
    ya, yb = yaw_proxy(a.get("kps")), yaw_proxy(b.get("kps"))
    if ya is None or yb is None:
        return False
    return ya * yb < 0 and abs(ya) + abs(yb) >= th["mirror_yaw_sum"]


def red_cord_like(w: dict, th: dict) -> bool:
    """手腕窗口里的红色像“细绳”而不是红衣服：总红色占比在 [lo, hi]，最大红色连通块包围盒够长、填充率低（细环）"""
    if w.get("red") is None or not (th["red_cord_lo"] <= w["red"] <= th["red_cord_hi"]):
        return False
    return w.get("comp_span", 0) >= th["red_cord_span"] and w.get("comp_fill", 1) <= th["red_cord_fill"]


def merge_hands(hands: list[dict], iou: float) -> list[dict]:
    """交叠的手框并成一只：IoU >= iou，或一个框的中心落在另一个框里"""
    out: list[dict] = []
    for h in sorted(hands, key=lambda x: -x.get("conf", 0)):
        b = h["box"]
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        dup = False
        for o in out:
            ob = o["box"]
            ox, oy = (ob[0] + ob[2]) / 2, (ob[1] + ob[3]) / 2
            if _iou(b, ob) >= iou or (ob[0] <= cx <= ob[2] and ob[1] <= cy <= ob[3]) or (b[0] <= ox <= b[2] and b[1] <= oy <= b[3]):
                dup = True
                break
        if not dup:
            out.append(h)
    return out


def assign_hand(box, dw: list[dict], th: dict):
    """手归给谁：DWPose 的腕点（腕到肘连线末端）离手框中心最近、且在 reach 内的那个人；没有人体点（只露手的局部人）→ None。
    dw 里没有 body 点的旧数据退回“手框中心落在人框内”。"""
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    reach = th["hand_wrist_reach"] * max(box[2] - box[0], box[3] - box[1])
    best, bd = None, 1e18
    for i, p in enumerate(dw):
        body = p.get("body")
        if not body or len(body) < 8:
            pb = p.get("box")
            if pb and pb[0] <= cx <= pb[2] and pb[1] <= cy <= pb[3]:
                d = ((cx - (pb[0] + pb[2]) / 2) ** 2 + (cy - (pb[1] + pb[3]) / 2) ** 2) ** 0.5
                if d < bd:
                    best, bd = i, d
            continue
        for wi, ei in ((4, 3), (7, 6)):       # 右腕 / 右肘，左腕 / 左肘（openpose 序）
            w, e = body[wi], body[ei]
            if w[2] < th["kp_conf"]:
                continue
            # 腕点沿肘 → 腕方向再延伸半个手长，当作手的预期位置
            ex, ey = (w[0] - e[0], w[1] - e[1]) if e[2] >= th["kp_conf"] else (0.0, 0.0)
            n = (ex * ex + ey * ey) ** 0.5 or 1.0
            hl = max(box[2] - box[0], box[3] - box[1]) * 0.5
            px, py = w[0] + ex / n * hl, w[1] + ey / n * hl
            d = ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5
            if d <= reach and d < bd:
                best, bd = i, d
    return best


def _owner_person(box, persons):
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    best, bi = None, 1e18
    for i, p in enumerate(persons):
        b = p["box"]
        if b[0] <= cx <= b[2] and b[1] <= cy <= b[3]:
            d = ((cx - (b[0] + b[2]) / 2) ** 2 + (cy - (b[1] + b[3]) / 2) ** 2)
            if d < bi:
                best, bi = i, d
    return best


# ───────────────────────── 判定（纯函数） ─────────────────────────
def judge(feat: dict, cast: list[dict] | None = None, th: dict | None = None) -> dict:
    """feat = 远端测量值（REMOTE_SRC 的输出一项）；cast 缺省 = 不比人数。
    返回 {"defects":[{"code","detail","conf"}], "metrics":{…}, "mirror_seg": "ok"|"未做镜面分割", "verdict": "hit"|"clean"}"""
    t = {**TH, **(th or {})}
    ci = cast_info(cast)
    defects: list[dict] = []
    H = feat.get("H") or 1
    mask = feat.get("mirror")
    seg = "ok" if mask is not None else "未做镜面分割"
    refs_ok = bool(feat.get("refs_loaded"))

    # —— 头 ——
    fdet = max(t["face_det"], 0.75) if ci.get("has_back") else t["face_det"]      # 背影镜：检测分 < 0.75 的不算脸（总调度已批）
    heads = [f for f in feat.get("faces") or []
             if f.get("score", 1) >= fdet and 0.5 <= (f["box"][2] - f["box"][0]) / max(f["box"][3] - f["box"][1], 1e-6) <= 1.8 and (f["box"][3] - f["box"][1]) >= t["min_face_px"]]
    small = [f for f in feat.get("faces") or [] if f not in heads]
    for f in heads:
        f["_mir"] = mirror_cover(f["box"], mask) >= t["mirror_cover"] if mask is not None else False
        sims = f.get("sims") or {}
        f["_id"], f["_sim"] = (max(sims.items(), key=lambda kv: kv[1]) if sims else (None, 0.0))
    real = [f for f in heads if not f["_mir"]]
    mir = [f for f in heads if f["_mir"]]
    geo = []        # 几何镜像：同身份、同高度、朝向相反 → 较靠边的那张算镜中头
    for i in range(len(real)):
        for j in range(i + 1, len(real)):
            a_, b_ = real[i], real[j]
            same = _cos(a_.get("emb"), b_.get("emb")) >= t["pair_cos"] or (a_["_id"] and a_["_id"] == b_["_id"])
            if same and mirror_pair(a_, b_, t, feat.get("vlines")):
                geo.append((a_, b_))
    for a_, b_ in geo:
        edge = min(a_, b_, key=lambda f: min(f["box"][0], feat.get("W", 1e9) - f["box"][2]))
        if edge in real:
            real.remove(edge)
            mir.append(edge)
            edge["_geo"] = True

    # 同一人出现两次（非镜面区域）
    dup_pairs = []
    for i in range(len(real)):
        for j in range(i + 1, len(real)):
            a, b = real[i], real[j]
            pc = _cos(a.get("emb"), b.get("emb"))
            same_role = a["_id"] and a["_id"] == b["_id"] and min(a["_sim"], b["_sim"]) >= t["same_cos"]
            if pc >= t["pair_cos"] or same_role:
                dup_pairs.append((i, j, round(pc, 3), a["_id"]))
    refs_have = set(feat.get("refs_loaded") or [])
    refs_ready = bool(refs_have) and set(ci.get("in_frame_ids") or []) <= refs_have      # 在场角色的身份脸缺 = 不做陌生人判据
    strangers = [f for f in real if f["_sim"] < t["stranger_cos"]
                 and (f["box"][3] - f["box"][1]) >= t["small_face_frac"] * H] if (refs_ready and not ci.get("refless")) else []
    if dup_pairs:
        who = "、".join(sorted({p[3] or "?" for p in dup_pairs}))
        defects.append({"code": "duplicate_person", "conf": "high",
                        "detail": f"非镜面区域同一人出现 {len(dup_pairs) + 1} 次：{who}（头间 cos " + "/".join(str(p[2]) for p in dup_pairs) + "）"})
    elif strangers and ci["known"]:
        defects.append({"code": "extra_head", "conf": "med",
                        "detail": f"{len(strangers)} 个头认不出是 cast 里任何人（最高 ArcFace {max(f['_sim'] for f in strangers):.2f} < {t['stranger_cos']}），cast 里没有路人行"})
    elif ci["known"]:
        # cast 没写路人行时，脸高 < small_face_frac 的后景小头不计入头数（前景陌生人照判）
        counted = [f for f in real if ci.get("refless") or (f["box"][3] - f["box"][1]) >= t["small_face_frac"] * H]
        if len(counted) > ci["n_faced"] + ci["n_partial"]:
            defects.append({"code": "extra_head", "conf": "med",
                            "detail": f"非镜面头 {len(counted)} 个 > cast 在画 {ci['n_faced']} + 局部 / 背影 {ci['n_partial']}"})
    # 镜中头：必须是在场某人的镜像
    for f in mir:
        best = max([_cos(f.get("emb"), r.get("emb")) for r in real] or [0.0])
        if best < t["mirror_cos"]:
            defects.append({"code": "extra_head", "conf": "med",
                            "detail": f"镜面区域里的头对不上在场任何人（最高 cos {best:.2f} < {t['mirror_cos']}）"})
    if mir and not any(d["code"] == "extra_head" for d in defects):
        pass   # 全部镜像 → 放行

    # —— 人框 ——
    persons = [p for p in feat.get("persons") or [] if p.get("conf", 1) >= t["person_conf"]]
    big = [p for p in persons if (p["box"][3] - p["box"][1]) >= 0.25 * H]

    # —— DWPose 人 / 手 / 脚 ——
    dw = feat.get("dw") or []
    npeople = max(len(dw), len(big), ci.get('n_in_frame') or 0, 1)      # 只露手的局部人没有人体框，手的名额按 cast 入画人数补
    hands = merge_hands(nms([h for h in feat.get("hands") or [] if h.get("conf", 1) >= t["hand_conf"]], t["hand_nms_iou"]), t["hand_merge_iou"])
    per_person = {}
    for h in hands:
        o = assign_hand(h["box"], dw, t)
        per_person[o] = per_person.get(o, 0) + 1
    over = {k: v for k, v in per_person.items() if k is not None and v > t["max_hands_per_person"]}
    if over:
        defects.append({"code": "extra_hands", "conf": "med",
                        "detail": "单人手框 " + "、".join(f"人{k}={v}" for k, v in over.items()) + f" > {t['max_hands_per_person']}"})
    elif len(hands) > t["max_hands_per_person"] * npeople:
        defects.append({"code": "extra_hands", "conf": "med",
                        "detail": f"手框 {len(hands)} > {t['max_hands_per_person']}×{npeople} 人"})
    bad_f = 0
    tested_f = 0
    feet_dup = 0
    for p in dw:
        for hk in (p.get("lhand"), p.get("rhand")):
            r = finger_bad(hk, t["kp_conf"])
            if r is not None:
                tested_f += 1
                bad_f += bool(r)
        feet = p.get("feet") or []        # 6 点：左 3 + 右 3
        for side in (feet[:3], feet[3:6]):
            pts = [(q[0], q[1]) for q in side if len(q) > 2 and q[2] >= t["kp_conf"]]
            if len(pts) >= 2:
                span = max(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a in pts for b in pts)
                if span > 0.25 * H:        # 同一只脚的三点相距太远 → 疑似两簇
                    feet_dup += 1
    if bad_f:
        defects.append({"code": "finger_ratio", "conf": "low", "detail": f"{bad_f}/{tested_f} 只手的手指骨长 / 掌宽超出 V6 口径"})
    if feet_dup:
        defects.append({"code": "feet_dup", "conf": "low", "detail": f"{feet_dup} 只脚的关键点分成两簇"})
    # —— 标志饰品（红绳）按造型登记判：这个人本镜造型的饰品里没登记红绳，手腕上却有 → 错位 ——
    acc_of = {_ids().get(c.get("name"), c.get("name")): c.get("look_acc") for c in (cast or []) if c.get("look_acc") is not None}
    for w in feat.get("wrists") or []:
        if not red_cord_like(w, t):
            continue
        owner = None
        if w.get("person") is not None and 0 <= w["person"] < len(dw):
            pb = dw[w["person"]]["box"]
            for f in heads:
                cx, cy = (f["box"][0] + f["box"][2]) / 2, (f["box"][1] + f["box"][3]) / 2
                if pb[0] <= cx <= pb[2] and pb[1] <= cy <= pb[3]:
                    owner = f["_id"] if f["_sim"] >= t["same_cos"] else None
        if owner is None and len(ci.get("in_frame_ids") or []) == 1:
            owner = ci["in_frame_ids"][0]
        if owner is not None and owner in acc_of and SIGNATURE_KEYWORD not in (acc_of[owner] or ""):
            defects.append({"code": "signature_item_misowned", "conf": "med",
                            "detail": f"手腕上有红绳（红色占比 {w['red']:.2f}），但 {owner} 本镜造型登记的饰品里没有红绳（{acc_of[owner] or '无饰品'}）"})
            break
    metrics = {"heads": len(heads), "heads_real": len(real), "heads_mirror": len(mir), "heads_mirror_geo": sum(1 for f in mir if f.get("_geo")), "faces_small": len(small),
               "persons_big": len(big), "dw_people": len(dw), "hands": len(hands), "expected_faced": ci["n_faced"],
               "expected_in_frame": ci["n_in_frame"], "fingers_tested": tested_f, "fingers_bad": bad_f}
    defects += blocking_defects(heads, cast, feat.get("W") or 0, t)      # K-REN-952：站位核对（cast x vs 认出的脸）
    verdict = "hit" if any(d["conf"] != "low" for d in defects) else "clean"
    untested = []
    if real and not refs_ready and not ci.get("refless"):
        # OVERSIGHT-F 第 6 轮（11-02）：身份参照没加载 / 在场角色缺身份脸时，陌生人判据整个跳过，原来静默判 clean = 未测当通过
        untested.append("身份参照没加载（在场角色的身份脸缺：" + "、".join(sorted(set(ci.get("in_frame_ids") or []) - refs_have) or ["全部"]) +
                        "），陌生人 / 多出人头判不了")
        if verdict == "clean":
            verdict = "untested"
    return {"defects": defects, "metrics": metrics, "mirror_seg": seg, "refs_loaded": refs_ok, "untested": untested,
            "verdict": verdict}


# K-REN-952（PROMPTFIX 11-02，fb-0005 ep05-S3“我的位置不对”：cast 林晚 = 中，出图林晚在左）：站位核对。
# cast 每人 x（左 / 中 / 右，导入稿唯一来源）vs 认得出是该角色（ArcFace ≥ same_cos）的非镜中脸中心横坐标：
#   左 = 中心 < 0.45 W；右 = 中心 > 0.55 W；中 = 0.25–0.75 W。对不上 → blocking_mismatch（med）。没 x / 没认出 = 不判
BLOCK_RANGE = {"左": (0.0, 0.45), "右": (0.55, 1.0), "中": (0.25, 0.75), "中偏左": (0.15, 0.55), "中偏右": (0.45, 0.85)}


def blocking_defects(heads: list[dict], cast: list[dict] | None, W: float, th: dict | None = None) -> list[dict]:
    t = {**TH, **(th or {})}
    if not cast or not W:
        return []
    out = []
    for c in cast:
        x = c.get("x")
        cid = _ids().get(c.get("name"), c.get("name"))
        if x not in BLOCK_RANGE or not cid:
            continue
        cand = [f for f in heads if not f.get("_mir") and (f.get("sims") or {}).get(cid, 0) >= t["same_cos"]]
        if not cand:
            continue
        f = max(cand, key=lambda f: (f.get("sims") or {}).get(cid, 0))
        cx = (f["box"][0] + f["box"][2]) / 2 / W
        lo, hi = BLOCK_RANGE[x]
        if not (lo <= cx <= hi):
            out.append({"code": "blocking_mismatch", "conf": "med",
                        "detail": f"{c.get('name')} 剧本站位 {x}，画面里脸中心在 {cx:.2f} W（应在 {lo:.2f}–{hi:.2f}）"})
    return out


# ───────────────────────── 远端测量脚本（gpu36 venv，GPU2） ─────────────────────────
WRIST_RED_SRC = r'''
def wrist_red(img, dw):
    # 手腕红色占比（标志饰品“红绳”）：手腕点周围、以掌长为尺的窗口里 HSV 红色像素的比例。
    # 口径（11-02 总调度亲看 ep01-S10 尾帧：0.10 的“红”其实是肤色 / 唇色）：色相 <6 或 >174、饱和度 >170、明度 >80
    H, W = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    red = (((hsv[..., 0] < 6) | (hsv[..., 0] > 174)) & (hsv[..., 1] > 170) & (hsv[..., 2] > 80))
    wr = []
    for pi, pp in enumerate(dw):
        for hk in (pp["lhand"], pp["rhand"]):
            if not hk or min(q[2] for q in hk) < 0.3:
                continue
            wx, wy = hk[0][0], hk[0][1]
            u = max(8.0, ((hk[9][0] - wx) ** 2 + (hk[9][1] - wy) ** 2) ** 0.5)
            x1, x2, y1, y2 = int(max(0, wx - 0.7 * u)), int(min(W, wx + 0.7 * u)), int(max(0, wy - 0.7 * u)), int(min(H, wy + 0.7 * u))
            if x2 > x1 and y2 > y1:
                win = red[y1:y2, x1:x2].astype(np.uint8)
                n, lab, st, _c = cv2.connectedComponentsWithStats(win, connectivity=8)
                span = fill = 0.0
                if n > 1:
                    j = 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))
                    bw, bh, ar = st[j, cv2.CC_STAT_WIDTH], st[j, cv2.CC_STAT_HEIGHT], st[j, cv2.CC_STAT_AREA]
                    span = max(bw, bh) / max(1.0, float(x2 - x1)); fill = ar / max(1.0, float(bw * bh))
                wr.append({"person": pi, "red": round(float(win.mean()), 3), "comp_span": round(float(span), 3), "comp_fill": round(float(fill), 3), "xy": [round(wx), round(wy)]})
    return wr
'''

REMOTE_SRC = r'''
import base64, glob, json, os, sys, io
for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ[k] = "4"
import numpy as np, cv2
cv2.setNumThreads(4)
req = json.load(sys.stdin)
COMFY = req["comfy"] + "/ComfyUI"
WORK = req["work"]
UL = COMFY + "/models/ultralytics/"
sys.path.insert(0, COMFY + "/custom_nodes/comfyui_controlnet_aux/src")
import warnings; warnings.filterwarnings("ignore")
from ultralytics import YOLO
yp, yf, yh = YOLO(UL + "segm/person_yolov8m-seg.pt"), YOLO(UL + "bbox/face_yolov8m.pt"), YOLO(UL + "bbox/hand_yolov9c.pt")
from insightface.app import FaceAnalysis
fa = FaceAnalysis(name="buffalo_l", root=WORK + "/idm_models", allowed_modules=["detection", "recognition"], providers=["CPUExecutionProvider"])
fa.prepare(ctx_id=-1, det_size=(640, 640))
from custom_controlnet_aux.dwpose.wholebody import Wholebody
D = COMFY + "/custom_nodes/comfyui_controlnet_aux/ckpts/yzd-v/DWPose/"
wb = Wholebody(D + "yolox_l.onnx", D + "dw-ll_ucoco_384.onnx")
# 镜面分割（可选；模型不在 = mirror 为 None，结果标“未做镜面分割”）
os.environ["HF_HUB_OFFLINE"] = "1"
seg = None
OF = WORK + "/hf_models/oneformer_ade20k_swin_tiny"
if os.path.isdir(OF):
    try:
        import torch
        from transformers import OneFormerProcessor, OneFormerForUniversalSegmentation
        proc = OneFormerProcessor.from_pretrained(OF, repo_path=OF, class_info_file="ade20k_panoptic.json")
        mdl = OneFormerForUniversalSegmentation.from_pretrained(OF).to("cuda").eval()
        seg = (proc, mdl, [8, 27], torch)      # ADE20K：8 windowpane、27 mirror（0 起）
    except Exception as e:
        print("seg load fail", repr(e)[:300], file=sys.stderr)

@@WRIST_RED@@
def emb_of(img):
    out = []
    for f in fa.get(img):
        out.append({"box": [float(x) for x in f.bbox], "score": float(f.det_score), "kps": [[round(float(a), 1), round(float(b), 1)] for a, b in f.kps], "emb": [round(float(x), 4) for x in f.normed_embedding]})
    return out

refs = {}
for cid, b64 in (req.get("refs") or {}).items():
    im = cv2.imdecode(np.frombuffer(base64.b64decode(b64), np.uint8), 1)
    fs = emb_of(im)
    if not fs:      # 紧裁脸 / 大头照检不出：四周补 50% 边再试（2026-11-02 林晚新身份脸 ar-c2351c9dcf 就是这样漏的）
        ph, pw = im.shape[0] // 2, im.shape[1] // 2
        fs = emb_of(cv2.copyMakeBorder(im, ph, ph, pw, pw, cv2.BORDER_REFLECT))
    if fs:
        refs[cid] = max(fs, key=lambda f: (f["box"][2] - f["box"][0]) * (f["box"][3] - f["box"][1]))["emb"]

def boxes(res, conf):
    r = res[0]
    return [{"box": [round(float(v), 1) for v in b.xyxy[0].tolist()], "conf": round(float(b.conf[0]), 3)} for b in r.boxes if float(b.conf[0]) >= conf]

def mirror_mask(img):
    if seg is None:
        return None
    proc, mdl, ids, torch = seg
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    with torch.no_grad():
        from PIL import Image
        inp = proc(images=Image.fromarray(rgb), task_inputs=["semantic"], return_tensors="pt").to("cuda")
        out = mdl(**inp)
    m = proc.post_process_semantic_segmentation(out, target_sizes=[img.shape[:2]])[0].cpu().numpy()
    sel = np.isin(m, ids).astype(np.uint8)
    gw = 96; gh = max(1, int(gw * img.shape[0] / img.shape[1]))
    small = cv2.resize(sel, (gw, gh), interpolation=cv2.INTER_AREA) > 0
    return {"W": img.shape[1], "H": img.shape[0], "rows": ["".join("1" if v else "0" for v in r) for r in small]}

res = {"@refs": refs}
for it in req["items"]:
    img = cv2.imdecode(np.frombuffer(base64.b64decode(it["b64"]), np.uint8), 1)
    H, W = img.shape[:2]
    o = {"W": W, "H": H, "refs_loaded": sorted(refs)}
    try:
        o["persons"] = boxes(yp.predict(img, classes=[0], verbose=False, conf=0.25), 0.25)
        yfs = boxes(yf.predict(img, verbose=False, conf=0.3), 0.3)
        o["yolo_faces"] = yfs
        o["hands"] = boxes(yh.predict(img, verbose=False, conf=0.25), 0.25)
        faces = emb_of(img)
        for f in faces:
            f["sims"] = {c: round(sum(a * b for a, b in zip(f["emb"], e)), 4) for c, e in refs.items()}
        o["faces"] = faces
        k = wb(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        dw = []
        if k is not None:
            for p in k:
                p = p.tolist()
                dw.append({"box": [min(q[0] for q in p[:18] if q[2] > .3) if any(q[2] > .3 for q in p[:18]) else 0,
                                   min(q[1] for q in p[:18] if q[2] > .3) if any(q[2] > .3 for q in p[:18]) else 0,
                                   max(q[0] for q in p[:18] if q[2] > .3) if any(q[2] > .3 for q in p[:18]) else 0,
                                   max(q[1] for q in p[:18] if q[2] > .3) if any(q[2] > .3 for q in p[:18]) else 0],
                           "body": p[:18], "feet": p[18:24], "lhand": p[92:113], "rhand": p[113:134]})
        o["dw"] = dw
        o["wrists"] = wrist_red(img, dw)
        o["red_v"] = 2
        # 贯穿画面的竖线（镜框 / 门框）：几何镜像判据用
        ed = cv2.Canny(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 60, 150)
        ls = cv2.HoughLinesP(ed, 1, np.pi / 180, 80, minLineLength=int(0.45 * H), maxLineGap=int(0.03 * H))
        ls = None if ls is None else ls.reshape(-1, 1, 4)
        o["vlines"] = sorted({round((l[0][0] + l[0][2]) / 2) for l in (ls if ls is not None else []) if abs(l[0][0] - l[0][2]) <= 0.03 * abs(l[0][3] - l[0][1])})
        o["mirror"] = mirror_mask(img)
    except Exception as e:
        o["error"] = repr(e)[:300]
    res[it["id"]] = o
print("@@RES@@" + json.dumps(res))
'''

REMOTE_SRC = REMOTE_SRC.replace("@@WRIST_RED@@", WRIST_RED_SRC)


_SELF_IP: dict[str, bool] = {}


def _is_self_ip(ip: str) -> bool:
    """这个 IP 是不是本机网卡上的地址：UDP 套接字能 bind 上 = 本机（不发包、不写盘）；结果缓存"""
    import socket
    if ip not in _SELF_IP:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((ip, 0))
            _SELF_IP[ip] = True
        except OSError:   # arch-ok: bind 不上 = 不是本机地址（别台 / 不可解析），照原地址发
            _SELF_IP[ip] = False
        finally:
            s.close()
    return _SELF_IP[ip]


def self_loopback(url: str, is_self=None) -> str:
    """W-SCORE 11-07：count_host 是本机自己的内网入口（gpu36 上的 http://10.0.0.36:8893）→ 改走 127.0.0.1:<同端口>。
    内网入口是 lan_proxy（--allow 10.0.0.33，只放行对端），本机从 10.0.0.36 连自己的 10.0.0.36 被直接 reset
    （gpu36 producer 11-06 22:02 起 Q10 人数检测全 untested → score_infra 空错误、ep02-S10 / S11 无限只重打分）。
    127.0.0.1:<同端口> 就是 lan_proxy 转发的那个常驻检测服务：同一个服务、同一张卡、结果相同"""
    from urllib.parse import urlsplit, urlunsplit
    try:
        u = urlsplit(url)
        host, port = u.hostname, u.port
    except ValueError:   # arch-ok: 地址解析不了 = 原样返回（下游照旧报“常驻检测服务不可用”带原因）
        return url
    if not host or host in ("127.0.0.1", "localhost") or not (is_self or _is_self_ip)(host):
        return url
    return urlunsplit((u.scheme, f"127.0.0.1:{port}" if port else "127.0.0.1", u.path, u.query, u.fragment))


def _measure_http(files: dict[str, Path], url: str, batch: int, refs: dict) -> dict:
    """常驻检测服务（VQAGATE 11-02 20:1x，总调度：人脸 / 人数 / 手形模型常驻，不每次冷启动；图片只进本地 / 自有服务器模型）"""
    import urllib.request
    url = self_loopback(url)          # W-SCORE 11-07：本机自己的内网入口 → 127.0.0.1（lan_proxy 不放行本机来源）
    out: dict = {}
    ids = list(files)
    for i in range(0, max(1, len(ids)), batch):
        part = ids[i:i + batch]
        body = json.dumps({"refs": refs, "items": [{"id": k, "b64": _jpeg_b64(Path(files[k]))} for k in part]}).encode("utf-8")
        req = urllib.request.Request(url.rstrip("/") + "/measure", body, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                out.update(json.loads(r.read()))
        except Exception as e:  # noqa: BLE001 — 服务不可用 = 这批 untested（调用方按 error 记），不当通过
            for k in part:
                out[k] = {"error": f"常驻检测服务不可用（{url}）：{type(e).__name__}: {e}"[:300]}
        if not ids:
            break
    return out


def _face_refs() -> dict[str, str]:
    sys.path[:0] = [str(ROOT / "services/agent"), str(ROOT / "packages/qc"), str(ROOT), str(ROOT / "tools/gen_contract")]
    try:
        import remote_score as RS
        out = {}
        for cid, f in RS.local_faces().items():
            out[cid] = base64.b64encode((ROOT / f).read_bytes()).decode()
        return out
    except Exception as e:  # noqa: BLE001
        print("身份脸读不到：", repr(e)[:200], file=sys.stderr)
        return {}


def _jpeg_b64(path: Path, side: int = 1536) -> str:
    import cv2
    im = cv2.imdecode(__import__("numpy").fromfile(str(path), dtype="uint8"), 1)
    h, w = im.shape[:2]
    if max(h, w) > side:
        s = side / max(h, w)
        im = cv2.resize(im, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", im, [cv2.IMWRITE_JPEG_QUALITY, 93])
    return base64.b64encode(buf.tobytes()).decode()


def ref_embs(host: str = "gpu36") -> dict:
    """只取身份脸的 ArcFace 向量（不测图）：旧测量值里缺角色时用来本地重算 sims"""
    return measure({}, host).get("@refs") or {}


def refresh_red(feats: dict, files: dict[str, Path]) -> int:
    """按当前红色口径（WRIST_RED_SRC）本地重算已落盘测量值里的 wrists（读本机图片，不连远端）"""
    import cv2
    import numpy as np
    ns = {"cv2": cv2, "np": np}
    exec(WRIST_RED_SRC, ns)      # noqa: S102 — 自己的源码常量，和远端同一份
    n = 0
    for k, f in feats.items():
        if k.startswith("@") or f.get("error") or k not in files or not f.get("dw"):
            continue
        im = cv2.imdecode(np.fromfile(str(files[k]), dtype="uint8"), 1)
        if im is None:
            print(f"读不了图，红色口径没重算：{files[k]}", file=sys.stderr)
            continue
        f["wrists"] = ns["wrist_red"](im, f["dw"])
        f["red_v"] = 2
        n += 1
    return n


def resim(feats: dict, refs: dict) -> int:
    """用 refs 向量重算每张脸的 sims 和每条 feat 的 refs_loaded，返回改动的脸数"""
    n = 0
    for k, f in feats.items():
        if k.startswith("@") or f.get("error"):
            continue
        f["refs_loaded"] = sorted(refs)
        for x in f.get("faces") or []:
            x["sims"] = {c: round(_cos(x["emb"], e), 4) for c, e in refs.items()}
            n += 1
    return n


DETECTOR_GPU = {"gpu36": 2}     # 只有 gpu36 的 GPU2（看图 / 打分专用卡）跑检测；别的主机一律 CPU（车道表可登记 detector_gpu 覆盖）


def device_prefix(host: str, hosts: dict | None = None) -> str:
    """检测器在哪张卡上跑（FACTORY 11-02 17:4x 🔴：gpu33 的 GPU2 是坏卡，硬规则永远不用；原来写死 CUDA_VISIBLE_DEVICES=2）。
    卡号 = 车道表 hosts[host].detector_gpu → 否则 DETECTOR_GPU；没有卡 → CPU（清空 CUDA_VISIBLE_DEVICES，K-OPS-F17：8 线程、
    nice 10、绑最后 16 核）。算出来的卡在 broken_gpus 里 → 直接抛错，绝不往坏卡上提交"""
    if hosts is None:
        sys.path.insert(0, str(ROOT))
        from tools.factory import hosts as H
        hosts = H.load()["hosts"]
    h = hosts.get(host) or {}
    gpu = h.get("detector_gpu", DETECTOR_GPU.get(host))
    if gpu is not None and int(gpu) in [int(x) for x in h.get("broken_gpus") or []]:
        raise RuntimeError(f"{host} 的 GPU{gpu} 在 broken_gpus 里，检测器不许用（K-OPS 坏卡硬规则）")
    if gpu is None:
        th = "OMP_NUM_THREADS=8 MKL_NUM_THREADS=8 OPENBLAS_NUM_THREADS=8 NS_SCORE_THREADS=8"
        return f'CUDA_VISIBLE_DEVICES="" {th} nice -n 10 taskset -c $(( $(nproc) - 16 ))-$(( $(nproc) - 1 ))'
    return f"CUDA_VISIBLE_DEVICES={int(gpu)} CUDA_DEVICE_ORDER=PCI_BUS_ID"


def measure(files: dict[str, Path], host: str = "gpu36", batch: int = 8, refs: dict | None = None) -> dict:
    """{id: 本机图片} → {id: 测量值}。host = 主机名 → rssh 冷启动跑 REMOTE_SRC；host = http(s) 地址 → 常驻检测服务
    （tools/vqa/anomaly_server.py，模型只加载一次，gpu36 内网入口；返回格式完全相同）"""
    sys.path.insert(0, str(ROOT))
    refs = _face_refs() if refs is None else refs
    if str(host).startswith(("http://", "https://")):
        return _measure_http(files, str(host), batch, refs)
    from tools.netfix.rssh import run_retry
    from tools.factory import hosts as H
    comfy_root = H.load()["hosts"][host]["comfy_root"].rstrip("/")      # 车道表登记的 ComfyUI 根；其上一级 = 打分 / 模型工作区
    work = comfy_root.rsplit("/", 1)[0]
    py = work + "/venv/bin/python"
    out: dict = {}
    ids = list(files)
    if not ids:       # 只取身份脸向量
        run_retry(host, "cat > /tmp/detect_anomaly.py", input=REMOTE_SRC, text=True, timeout=60)
        r = run_retry(host, f"{device_prefix(host)} {py} /tmp/detect_anomaly.py", text=True, timeout=600,
                      input=json.dumps({"comfy": comfy_root, "work": work, "refs": refs, "items": []}))
        line = next((ln for ln in (r.stdout or "").splitlines() if ln.startswith("@@RES@@")), None)
        return json.loads(line[7:]) if line else {}
    for i in range(0, len(ids), batch):
        part = ids[i:i + batch]
        payload = json.dumps({"comfy": comfy_root, "work": work, "refs": refs, "items": [{"id": k, "b64": _jpeg_b64(Path(files[k]))} for k in part]})
        # 脚本走第一行 heredoc 不行（stdin 被 JSON 占用）：脚本写到远端文件再跑
        run_retry(host, "cat > /tmp/detect_anomaly.py", input=REMOTE_SRC, text=True, timeout=60)
        r = run_retry(host, f"{device_prefix(host)} {py} /tmp/detect_anomaly.py", input=payload,
                      text=True, timeout=900)
        line = next((ln for ln in (r.stdout or "").splitlines() if ln.startswith("@@RES@@")), None)
        if not line:
            for k in part:
                out[k] = {"error": f"远端无结果 rc={r.returncode} {(r.stderr or '')[-300:]}"}
            continue
        out.update(json.loads(line[7:]))
    return out


# ───────────────────────── 镜头 / cast ─────────────────────────
def shot_accessories(sid: str, project: str | None = None) -> dict[str, str]:
    """{角色 id: 本镜造型登记的饰品原文}：asset_refs.registry_looks（shot_looks.json）给造型 id，load_looks（P1_looks）给“饰品=…”。
    登记里没有这个人 / 造型 → 不出现（检测器对它不判饰品）"""
    sys.path[:0] = [str(ROOT / "services/agent"), str(ROOT / "packages/schema"), str(ROOT / "tools/gen_contract")]
    from ns_agent import asset_refs as AR
    if project is None:
        import remote_score as RS      # 当前项目名的唯一来源（身份脸也是它）
        project = RS.FACE_PROJECT
    reg = AR.registry_looks(project).get(sid) or {}
    looks = AR.load_looks(project)
    out = {}
    for cid, lid in reg.items():
        lk = (looks.get(cid) or {}).get(lid)
        if lk is None:
            continue
        zh = lk.zh or ""
        out[cid] = zh.split("饰品=", 1)[1].split("｜", 1)[0] if "饰品=" in zh else ""
    return out


def shot_cast(sid: str) -> list[dict] | None:
    sys.path[:0] = [str(ROOT / "packages/schema"), str(ROOT / "services/render_router"), str(ROOT / "services/agent"), str(ROOT / "packages/qc"), str(ROOT)]
    from ns_render import pipeline as P
    s = P.load_script()
    ep_shot = P.find_shot(s, sid)
    if not ep_shot:
        return None
    from ns_agent import framing as FR
    names = [c.name for c in ep_shot[1].cast]
    faced, _partial = FR.faced_people(ep_shot[1], names)      # 编译稿口径：在画，或局部但原文写到脸 / 头发
    from ns_agent import prompt_template_v2 as PT2
    hidden = PT2.hidden_people(ep_shot[1])                    # PS-MASTER 11-05：过肩前景只露肩 / 后脑、背影 → face_hidden（人数闸计上限不计下限）
    acc = shot_accessories(sid)
    return [{"name": c.name, "presence": c.presence, "part": c.part, "faced": c.name in faced, "x": getattr(c, "x", None),
             **({"face_hidden": True, "hidden_part": hidden[c.name]} if c.name in hidden else {}),
             **({"look_acc": acc[_ids().get(c.name, c.name)]} if _ids().get(c.name, c.name) in acc else {})} for c in ep_shot[1].cast]


def current_images(include_last: bool = True) -> list[tuple[str, str, Path]]:
    """[(shot_id, stage, 路径)]：每镜 active 的首帧（和尾帧）。路径用 *_post.png"""
    out = []
    for f in sorted((ROOT / "data" / "renders").glob("ep*/S*/takes.json")):
        sid = "-".join(f.parent.parts[-2:])
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"takes.json 读不了，跳过 {sid}：{e!r}", file=sys.stderr)    # 显式报，不静默
            continue
        act = d.get("active") or {}
        for t in d.get("takes") or []:
            st = t.get("stage")
            if st in ("image", "last") and act.get(st) == t.get("n") and (st == "image" or include_last):
                p = ROOT / "data" / t["file"]
                if p.exists():
                    out.append((sid, st, p))
    return out


def check(image_path, cast: list[dict] | None, host: str = "gpu36") -> dict:
    """VQAGATE 契约接口（Q1d，只记录）：{"heads":{expected,got,mirror,extra}, "hands":{…}, "feet":{…}, "status": pass|fail|untested,
    "reasons":[…], "mode": …}。远端 / 模型不可用 → untested（带原因），不静默放过。"""
    k = "x"
    feats = measure({k: Path(image_path)}, host)
    f = feats.get(k) or {}
    if f.get("error") or not f:
        return {"status": "untested", "reasons": [f.get("error") or "远端无结果"], "mode": "error", "heads": {}, "hands": {}, "feet": {}}
    r = judge(f, cast)
    m = r["metrics"]
    hits = [d for d in r["defects"] if d["conf"] != "low"]
    status = "fail" if hits else ("untested" if r.get("untested") else "pass")     # 未测不当通过（config/vqa.json untested_blocks）
    return {"status": status, "mode": "full" if r["mirror_seg"] == "ok" else "no_mirror_seg",
            "reasons": [f"{d['code']}：{d['detail']}" for d in r["defects"]] + list(r.get("untested") or []),
            "heads": {"expected": m["expected_faced"], "got": m["heads_real"], "mirror": m["heads_mirror"],
                      "extra": [d["detail"] for d in r["defects"] if d["code"] in ("duplicate_person", "extra_head")]},
            "hands": {"got": m["hands"], "fingers_bad": m["fingers_bad"], "fingers_tested": m["fingers_tested"]},
            "feet": {"dup": [d["detail"] for d in r["defects"] if d["code"] == "feet_dup"]}}


def _atomic_json(path, obj) -> None:
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def run_files(files: dict[str, Path], casts: dict[str, list | None], host: str = "gpu36", feats_out: str | None = None,
              feats_in: str | None = None) -> dict:
    """feats_out：把远端测量值落盘（标定阈值时不用重扫）；feats_in：读已落盘的测量值，只重判（不连远端）"""
    if feats_in:
        feats = json.loads(Path(feats_in).read_text(encoding="utf-8"))
    else:
        old = {}
        if feats_out and Path(feats_out).exists():          # 续跑：已测好且没出错的不重测
            try:
                old = {k: v for k, v in json.loads(Path(feats_out).read_text(encoding="utf-8")).items()
                       if k in files and not v.get("error") and "vlines" in v}
            except (OSError, ValueError) as e:
                print(f"旧测量值读不了，全部重测：{e!r}", file=sys.stderr)
        todo = {k: v for k, v in files.items() if k not in old}
        feats = {**old, **(measure(todo, host) if todo else {})}
        if feats_out:
            _atomic_json(feats_out, feats)
    stale = [k for k, f in feats.items() if not k.startswith("@") and not f.get("error") and f.get("dw") is not None and f.get("red_v") != 2]
    if stale and (feats_in or feats_out):
        print(f"红色口径重算：{refresh_red({k: feats[k] for k in stale}, files)} 张", file=sys.stderr)
        if feats_out or feats_in:
            _atomic_json(feats_out or feats_in, feats)
    want = set(_ids().values())
    miss = [k for k, f in feats.items() if not k.startswith("@") and not f.get("error") and want - set(f.get("refs_loaded") or [])]
    if miss:
        refs = feats.get("@refs") or ref_embs(host)
        if want - set(refs):
            print(f"身份脸缺：{sorted(want - set(refs))}（这些角色的陌生人判据会跳过）", file=sys.stderr)
        print(f"重算 sims：{resim(feats, refs)} 张脸", file=sys.stderr)
        feats["@refs"] = refs
        if feats_out:
            _atomic_json(feats_out, feats)
    res = {}
    for k, f in feats.items():
        if k.startswith("@"):
            continue
        if f.get("error"):
            res[k] = {"error": f["error"], "verdict": "error", "defects": []}
            continue
        r = judge(f, casts.get(k))
        r["feat_summary"] = {"faces": [{"box": x["box"], "sims": x.get("sims")} for x in f.get("faces", [])]}
        res[k] = r
    return res


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="*")
    ap.add_argument("--cast", default=None)
    ap.add_argument("--host", default="gpu36")
    ap.add_argument("--shots", nargs="*")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--feats", default=None, help="远端测量值落盘路径（可重判）")
    ap.add_argument("--rejudge", default=None, help="读已落盘的测量值重判，不连远端")
    ap.add_argument("--raw", default=None, help="把远端测量值存到这个 json（扫全剧时可复用）")
    a = ap.parse_args(argv)
    files, casts = {}, {}
    if a.scan:
        for sid, st, p in current_images():
            k = f"{sid}|{st}"
            files[k] = p
            casts[k] = shot_cast(sid)
    elif a.shots:
        for sid, st, p in current_images():
            if sid in a.shots:
                k = f"{sid}|{st}"
                files[k], casts[k] = p, shot_cast(sid)
    else:
        cast = [{"name": n, "presence": "在画"} for n in a.cast.split(",")] if a.cast else None
        for p in a.images:
            files[p], casts[p] = Path(p), cast
    res = run_files(files, casts, a.host, a.feats, a.rejudge)
    if a.raw:
        _atomic_json(a.raw, res)
    print(json.dumps({k: {"verdict": v["verdict"], "defects": v.get("defects"), "metrics": v.get("metrics"), "mirror_seg": v.get("mirror_seg")}
                      for k, v in res.items()}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
