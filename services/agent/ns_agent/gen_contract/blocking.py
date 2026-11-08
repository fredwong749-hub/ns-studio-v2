"""L-196（项目方 11-05：“我需要你全自动，不要检测那么多了”）：首 / 尾帧闸门只留硬项。

config/gen_contract.json "blocking_items"（缺键 / 非列表 = 不启用本策略，保持旧口径）：
  Q1     身份（Q1 项；Q10 identity、QB identity 同源）
  count  人数（Q10 count、QB count、Q9 extra_person）
  text   文字 / 水印 / 字幕（Q6、QB ocr、Q9 text_watermark / brand_logo）
  adult  成人安全（Q9 underage_look、QB age）
  gaze   视线 / 排排站（L-200 项目方 11-06 看 ep01 总览：画中任何人看镜头、或多人正面并排站成一排合影式 = 不过、换种子重出）：
         Q9 看图 eye_contact / lineup 缺陷 + G1（YuNet 头部朝向）疑似看镜头。两者都有结论时以看图为准：看图真看过
         （evidence.model）且没报视线缺陷 → G1 只记录（YuNet 误报多）；看图没结果 → G1 自己挡。
  FL     首尾帧差异（L-203 项目方 11-06：尾帧与通过契约的首帧太像 = 不过、换种子重出；只有尾帧阶段才有 FL 项，
         gen_contract/first_last.py 出；首帧 postflight 里没有这一项，所以“只对 stage=last 生效”）
  也可直接写项 id（如 "Q3"），该项整项照旧挡。
不在名单里的项（Q1a/Q1x/Q2–Q5/Q7/Q8/G1/Q9 其余缺陷/Q10 其余/QB 其余）照常跑、照常记，但 status 改成 na（原判留
status_recorded + record_only=true），不进 blocking。apply() 原地改 pf 并返回；ok / blocking / score_infra 重算。"""
from __future__ import annotations

from . import cfg

BAD = ("fail", "untested")
GAZE_DEFECTS = {"eye_contact"}                 # L-200：看图里算“视线”硬项的缺陷 id（config/vqa.json checks）
# W-DETECT 11-06（复审）：看图的 lineup 口径放宽了但没标定 → 只记录、不挡、不算 never_adopt（VQA_RECORD_DEFECTS）；
# 排排站硬项只认 G1 的几何判 lineup_geo（ns_qc.gaze.lineup_geo，11-06 标定 5/5 正例、14 反例 0 误报）
VQA_RECORD_DEFECTS = {"lineup"}
# W-DETECT 11-06：看图报 eye_contact、但 YuNet（G1）测得的每张脸 |gaze_x| 都 ≥ 此值（明显离轴）→ 视线项降为只记录。
# 今天 ep01 15 张 Q9 eye_contact 全部 G1 离轴（gaze_x 0.64–1.30，最小 0.506）；亲看 S4 v24 / S14 v12 / S15 v16 都在看画面里的人，不看镜头
EYE_OFFAXIS_X = 0.45
Q9_HARD = {"count": {"extra_person"}, "text": {"text_watermark", "brand_logo"}, "adult": {"underage_look"}, "gaze": GAZE_DEFECTS}
QB_HARD = {"Q1": ("identity",), "count": ("count",), "text": ("ocr",), "adult": ("age",)}
Q10_HARD = {"Q1": ("identity",), "count": ("count",)}


def hard_set(conf: dict | None = None) -> set | None:
    v = (conf if conf is not None else cfg()).get("blocking_items")
    return set(map(str, v)) if isinstance(v, list) else None


def _lab_bad(lab: dict, key: str) -> bool:
    v = lab.get(key)
    st = v.get("status") if isinstance(v, dict) else v
    return st in BAD


def _q10_prompt_people_bad(ev: dict) -> bool:
    """Q10 evidence.prompt 判 fail 且原因是人数（people_count）→ 算人数硬项。
    W-DETECT 11-06：accept.prompt_check 给了结构化 people（可见人数 vs cast 区间）就以它为准——看图模型把“positions / action 不符”
    也写成“people_count、positions、action不符”抬头（ep01 S2 ×7、S15 ×5 只是站位 / 动作不符，人数其实对），字符串匹配会把它当人数硬项"""
    p = ev.get("prompt")
    if not isinstance(p, dict) or p.get("status") != "fail":
        return False
    people = p.get("people")
    if isinstance(people, dict) and people.get("status") in ("pass", "fail"):
        return people["status"] == "fail"
    return "people_count" in str(p.get("why") or "")


def _eye_contact_contradicted(defects: set, g1_ev: dict | None) -> bool:
    """看图只报 eye_contact（没报 lineup），而 YuNet 测得的每张脸都明显离轴（|gaze_x| ≥ EYE_OFFAXIS_X）→ 矛盾，视线项只记录"""
    if not g1_ev or (defects & (GAZE_DEFECTS | VQA_RECORD_DEFECTS)) != {"eye_contact"}:
        return False
    rows = g1_ev.get("faces") or []
    if int(g1_ev.get("n_faces") or len(rows)) > len(rows):
        return False                                   # 证据里的脸被截断（只存前 4 张）→ 没看全，不降级
    faces = [f for f in rows if f.get("status") != "未测" and f.get("gaze_x") is not None]
    return bool(faces) and all(abs(float(f["gaze_x"])) >= EYE_OFFAXIS_X for f in faces)


def vqa_gaze_verdict(pf: dict) -> bool | None:
    """L-200：看图（Q9）对视线 / 排排站的结论。True = 看图真看过（evidence.model）且没报 eye_contact / lineup；
    False = 报了；None = 没有看图结论（没跑 / 排队超时 / 出错）"""
    q9 = next((i for i in (pf or {}).get("items") or [] if i.get("id") == "Q9"), None)
    ev = (q9 or {}).get("evidence") or {}
    if not ev.get("model"):
        return None
    return not (set(ev.get("defects") or []) & GAZE_DEFECTS)


def _is_hard(it: dict, hard: set, vqa_gaze: bool | None = None, g1_ev: dict | None = None) -> bool:
    iid, ev = it.get("id"), it.get("evidence") or {}
    if iid in hard:
        return True
    if iid == "FL":
        return False        # L-203：FL 不在名单里 = 只记录（名单里有 "FL" 已在上一行判硬）
    if iid == "G1":
        if "gaze" in hard and (ev.get("lineup_geo") or {}).get("lineup"):
            return True     # W-DETECT 11-06：排排站几何判（ns_qc.gaze.lineup_geo）不被看图否掉——看图对合影式并排全漏（ep01 S1 / S14 / S15）
        # L-200：gaze 硬项时 YuNet 的 G1 挡，但看图真看过且没报视线缺陷 → 以看图为准（G1 只记录，防 YuNet 误报）
        return "gaze" in hard and vqa_gaze is not True
    if iid == "Q6":
        return "text" in hard
    if iid == "Q9":
        defects = set(ev.get("defects") or [])
        if "gaze" in hard and _eye_contact_contradicted(defects, g1_ev):
            ev["eye_contact_demoted"] = "看图报看镜头，但 YuNet 测得每张脸都明显离轴（|gaze_x| ≥ %.2f）→ 视线只记录" % EYE_OFFAXIS_X
            ev["ignored_defects"] = sorted(set(ev.get("ignored_defects") or []) | {"eye_contact"})   # producer.gaze_bad 同口径（不算 never_adopt）
            defects = defects - {"eye_contact"}
        if any(defects & Q9_HARD[k] for k in Q9_HARD if k in hard):
            return True
        return False
    if iid == "Q10":
        if any(_lab_bad(ev, k) for h, ks in Q10_HARD.items() if h in hard for k in ks):
            return True
        # REVIEW-B 11-05：L-191 的“看图人数闸”（tools/vqa/accept.prompt_check：visible_people vs cast → people_count 不符）落在 Q10 的
        # prompt 子项，不在 count（检测器，gpu33 上常 deferred）。count 是硬项时它判出的“多画了人 / 少人”同样要挡，否则 35 步多画人直接放行
        return "count" in hard and _q10_prompt_people_bad(ev)

    if iid == "QB":
        lab = ev.get("label") or {}
        return any(_lab_bad(lab, k) for h, ks in QB_HARD.items() if h in hard for k in ks)
    return False


def apply(pf: dict, conf: dict | None = None) -> dict:
    hard = hard_set(conf)
    if hard is None or not isinstance(pf, dict) or not pf.get("items"):
        return pf
    kept, recorded = [], []
    vqa_gaze = vqa_gaze_verdict(pf) if "gaze" in hard else None
    g1_ev = next((i.get("evidence") or {} for i in pf["items"] if i.get("id") == "G1"), None)
    for it in pf["items"]:
        if it.get("status") in BAD and it.get("blocking", True) is not False and not _is_hard(it, hard, vqa_gaze, g1_ev):
            it["status_recorded"] = it["status"]
            it["record_only"] = True
            it["detail"] = "只记录（L-196 全自动闸门，不挡）：" + str(it.get("detail") or "")
            it["status"] = "na"
            recorded.append(it.get("id"))
        elif it.get("status") in BAD and it.get("blocking", True) is not False:
            kept.append(it.get("id"))
    if not recorded:
        return pf
    pf["blocking"] = sorted(set(kept))
    pf["record_only_items"] = recorded
    pf["reasons"] = [r for r in pf.get("reasons") or [] if not any(str(r).startswith(str(x)) for x in recorded)]
    if pf.get("ok") is not None or kept or recorded:
        pf["ok"] = not kept and not (pf.get("error") or pf.get("remote_error"))
    if not any(i.get("status") == "untested" for i in pf["items"]):
        pf.pop("score_infra", None)
        pf.pop("accept_retry", None)
    return pf
