"""出图后自动验收入口（ALIGNMENT.md 第 8 步；VQAGATE 牵头、DETECT-S 配合；总调度 11-02 17:0x）。

每张帧一条结构化标签，四项全过才算过，结果写进 postflight 的 Q10 项作为闸门（不再只记录）：
  identity  ArcFace 对 asset_refs.face_of（契约 Q1 已算：三段线 + 身份库 top-k + 复核带）→ Q1 的 status / 逐人分
  outfit    对造型登记（P1_looks + shot_looks，K-REV-V20）：契约 Q2（服装区域 CLIP + 外层单品）+ Q1a（逐人头发）
  prompt    本地 Qwen3-VL 按编译稿逐条核对：人数、谁在左谁在右、动作、场景（K-REV-V01，图只进本地模型）
  count     头数 / 手数 / 同一人两次 / 饰品归属：tools/vqa/anomaly.py（DETECT-S）judge
汇总：任一项 fail → fail；否则任一项 untested → untested（闸门挡，基础设施没跑成的标 score_infra 只重打分不重出）；否则 pass。
检测器没有常驻服务之前用现有远端调用（anomaly.measure → rssh；服务器上调本机 = 本机执行），慢但准。

    python tools/vqa/accept.py --shots ep01 [--feats data/review_pages/anomaly/feats_vqagate_1102.json] [--via-tunnel]
        → data/vqa/accept/<集>.json + 打印逐帧表
    python tools/vqa/accept.py --regression      # data/vqa/regression_anomaly/labeled.json 的人工标签全部判对才退出 0
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in ("services/agent", "services/render_router", "packages/schema", "packages/qc", ""):
    if str(ROOT / _p) not in sys.path:
        sys.path.insert(0, str(ROOT / _p))
OUT = ROOT / "data" / "vqa" / "accept"
ORDER = ("identity", "outfit", "prompt", "count")


def _item(pf: dict, iid: str) -> dict | None:
    return next((i for i in pf.get("items") or [] if i.get("id") == iid), None)


def identity(pf: dict) -> dict:
    q1 = _item(pf, "Q1")
    if not q1:
        return {"status": "untested", "why": "契约里没有 Q1（没打分）"}
    ev = q1.get("evidence") or {}
    wrong = [f for f in ev.get("per_face") or [] if f.get("verdict") not in ("过", "可放行", "小脸不判", "未判")]
    return {"status": q1.get("status"), "sims": ev.get("sims"), "anchor": ev.get("anchor") or {}, "why": str(q1.get("detail") or "")[:200],
            "per_face": ev.get("per_face") or [], "wrong_faces": wrong}   # 逐脸：多人镜指出是哪张脸错了（总调度 20:1x）


WARDROBE_Q = ("看【待检】图里红框框住那张脸的这个人（红框外的人不算），对照造型登记逐项核对，只按画面回答，输出 JSON："
              '{"outfit": "符合|不符|看不清", "worn": "这个人实际穿的外层衣服（颜色 + 款式）", "nails": "长美甲|普通|看不见", "e": "不符的地方一句话"}。'
              "outfit = 画面里看得到的衣服是否就是登记的这一身（款式、颜色、材质；外层多出 / 换成别的衣服，例如漆皮、风衣、别人的衣服，都算不符）。"
              "上装和下装分开比；画面里看不到的单品（下装出框、被挡住）不算不符，只按看得到的部分判；深灰和黑色这类相近暗色不算不符。"
              "造型登记：")


# “谁穿什么”的硬失败判据：看图描述的衣服里出现登记服装原文没有的这些类型词（材质 / 款式），才算穿错（K-REV-V20 对造型登记）
GARMENT_TERMS = ("风衣", "大衣", "皮衣", "夹克", "毛衣", "卫衣", "羽绒", "西装", "外套", "衬衫", "T恤", "背心", "吊带", "束腰",
                 "连衣裙", "长裙", "短裙", "铅笔裙", "西裤", "牛仔", "漆皮", "皮裙", "高领", "围裙", "工装", "旗袍", "睡衣", "浴袍")


def look_texts(sid: str) -> dict[str, dict]:
    """{角色 id: {"服装", "饰品"}}：和 anomaly.shot_accessories 同一个来源（asset_refs.registry_looks + load_looks，K-REV-V20）"""
    sys.path.insert(0, str(ROOT / "tools/gen_contract"))
    from ns_agent import asset_refs as AR
    import remote_score as RS
    pj = RS.FACE_PROJECT
    looks = AR.load_looks(pj)
    out = {}
    for cid, lid in (AR.registry_looks(pj).get(sid) or {}).items():
        lk = (looks.get(cid) or {}).get(lid)
        if lk is None:
            continue
        parts = dict(x.split("=", 1) for x in (lk.zh or "").split("｜") if "=" in x)
        out[cid] = {"服装": parts.get("服装", ""), "饰品": parts.get("饰品", "")}
    return out


def wardrobe_check(sid: str, image, pf: dict) -> dict:
    """谁穿什么（PROMPTFIX 根因 11-02，ep08-S5 顾沉被画成穿黑色漆皮长风衣，judge 却判过）：每个露脸的人红框裁图问本地看图，
    对照他自己的造型登记——不符 = 硬失败；林晚的钻石水晶甲逐件核对（K-REN-955 撤了非特写的指甲参考，只能靠验收兜底），
    明确答“普通” = 不过，“看不见”不判。看图没跑成 = untested"""
    from PIL import Image, ImageDraw
    from ns_agent.vqa import client as VC
    from ns_agent.vqa import hair_attr as HA
    from ns_agent.vqa.contract import rubric_loads
    boxes = ((_item(pf, "Q1") or {}).get("evidence") or {}).get("boxes") or {}
    looks = look_texts(sid)
    todo = {c: b for c, b in boxes.items() if b and c in looks}
    if not todo:
        return {"status": "na", "why": "没有可核对的露脸角色（或造型未登记）"}
    im = Image.open(image).convert("RGB")
    bad, asked = [], {}
    for cid, box in todo.items():
        x1, y1, x2, y2 = box
        cx1, cy1, cx2, cy2 = HA.crop_box(im.width, im.height, box)
        c = im.crop((cx1, cy1, cx2, int(min(im.height, y2 + 5.0 * (y2 - y1)))))      # 往下多裁到手和下装
        ImageDraw.Draw(c).rectangle([x1 - cx1, y1 - cy1, x2 - cx1, y2 - cy1], outline=(255, 0, 0), width=max(3, int((x2 - x1) / 40)))
        fp = OUT / "crops" / f"{Path(str(image)).stem}_wardrobe_{cid}.jpg"
        fp.parent.mkdir(parents=True, exist_ok=True)
        c.save(fp, quality=92)
        lk = looks[cid]
        res = VC.generate({"system": "你是画面质检员，只看图、只报事实，只输出 JSON。",
                           "text": WARDROBE_Q + f"服装：{lk['服装']}；饰品：{lk['饰品']}",
                           "images": [{"path": str(fp), "label": "【待检】", "max_pixels": 400000}]}, wait_s=900)
        d = rubric_loads(res.get("text", "")) if "error" not in res else None
        if not d:
            return {"status": "untested", "why": f"{cid} 看图没跑成", "asked": asked}
        asked[cid] = d
        new_terms = [w for w in GARMENT_TERMS if w in str(d.get("worn") or "") and w not in lk["服装"]]
        if d.get("outfit") == "不符" and new_terms:
            # 看图说不符，且它描述的衣服里有登记里根本没有的服装类型词，才硬失败（ep08-S5：漆皮长风衣 vs 白衬衫配黑西裤）；
            # 只是颜色深浅 / 下装出框看不到这类（ep01-S1 尾帧：黑色西装外套 vs 登记的短款西装外套）不算
            bad.append(f"{cid} 穿的是 {d.get('worn')}（登记里没有：{'、'.join(new_terms)}），登记是 {lk['服装'][:40]}")
        elif d.get("outfit") == "不符":
            asked[cid]["note"] = "看图说不符，但没有登记外的服装类型词（颜色深浅 / 出框），不拦"
        if cid == "lin_wan" and "水晶甲" in lk["饰品"] and d.get("nails") == "普通":
            bad.append("lin_wan 指甲不是登记的 5 厘米钻石水晶甲")
    return {"status": "fail" if bad else "pass", "asked": asked, "why": "；".join(bad)}


def outfit(pf: dict, sid: str | None = None, image=None) -> dict:
    parts = {k: _item(pf, k) for k in ("Q2", "Q1a")}
    st = [(k, (v or {}).get("status")) for k, v in parts.items() if v]
    wd = wardrobe_check(sid, image, pf) if sid and image is not None else None
    if wd is not None:
        parts["谁穿什么"] = {"status": wd["status"], "detail": wd.get("why")}
        st.append(("谁穿什么", wd["status"]))
    if not st:
        return {"status": "untested", "why": "契约里没有 Q2 / Q1a"}
    if any(s == "fail" for _, s in st):
        status = "fail"
    elif any(s == "untested" for _, s in st):
        status = "untested"
    else:
        status = "pass"
    return {"status": status, "items": dict(st), "why": "；".join(f"{k} {str((parts[k] or {}).get('detail') or '')[:120]}" for k, s in st if s != "pass")}


PROMPT_Q = ("下面是这张图的编译稿（出图提示词）。只按画面逐条核对，输出 JSON："
            '{"people_count": "符合|不符|看不清", "positions": "符合|不符|原文没写", "action": "符合|不符|看不清", '
            '"scene": "符合|不符|看不清", "e": "不符的地方一句话"}。'
            "people_count = 画面里露脸 / 入画的人数是否等于原文写的人数；positions = 原文写了谁在左谁在右时是否一致；"
            "action = 主要动作是否一致；scene = 场景是否一致。\n编译稿：")


POS_REASK_Q = ("只核对画面里人物的左右站位。已知（以此为准，不要复述原文）：{note}。画面里是否如此？只输出 JSON："
               '{{"positions": "符合|不符|看不清", "e": "一句话：画面里谁在左、谁在右"}}。')


def position_selfcheck(sid: str, image, d: dict, cast: list | None = None, ask=None) -> tuple[dict, bool]:
    """positions 判不符时的自洽检查：判词里模型复述的“原文”左右与 cast（编译输入的站位）不一致 / 原文与画面说同一侧 → 作废重问一次。
    → (新判词, 仍未定)。判词自洽 / 取不到 cast = 原样返回、不重问"""
    from ns_agent.vqa import selfcheck as SC
    if cast is None:
        try:
            from tools.vqa import anomaly as AN
            cast = AN.shot_cast(sid)
        except Exception:  # noqa: BLE001  # arch-ok: 取不到 cast = 不做自洽检查（判词原样，不放宽）
            cast = None
    why = SC.position_contradiction(str(d.get("e") or ""), cast)
    if not why:
        return d, False
    if ask is None:
        from ns_agent.vqa import client as VC
        ask = VC.generate
    from ns_agent.vqa.contract import rubric_loads
    res = ask({"system": "你是画面质检员，只看图、只报事实，只输出 JSON。", "text": POS_REASK_Q.format(note=SC.truth_note(cast)),
               "images": [{"path": str(image), "label": "【待检】", "max_pixels": 600000}]}, wait_s=900, refresh=True)
    r2 = rubric_loads(res.get("text", "")) if "error" not in res else None
    if not r2 or r2.get("positions") not in ("符合", "不符"):
        return {**d, "selfcheck": why, "reask": r2 or {"error": res.get("error")}}, True
    if r2["positions"] == "不符" and SC.position_contradiction(str(r2.get("e") or ""), cast):
        return {**d, "selfcheck": why, "reask": r2}, True
    return {**d, "positions": r2["positions"], "e": str(r2.get("e") or ""), "selfcheck": why, "reask": r2}, False


# L-191：35 步 base 会多画人（S1 2/5 张出第 4 人）。严格人数闸：同一次看图里加问“可见人数”（整数，不告诉它期望值，免得被带偏），
# 代码按分镜 cast 比对——少于“在画”人数或多于“在画 + 局部 + 背影”人数 = people_count 不符 → Q10 fail → 换种子重出。
# 开关：config/gen_contract.json image_thresholds.Q10_accept.people_count_strict = true，或本剧首帧档 first_frame_tier = base35（自动跟随）
PEOPLE_Q = ('另外在同一个 JSON 里加一项 "visible_people": 整数——画面里能看到的人数（含背影、侧脸、远处、只露一部分的人；'
            "不含海报 / 照片 / 屏幕 / 镜中倒影里的人）。\n")


def people_gate_on(project: str | None = None) -> bool:
    try:
        from ns_agent.gen_contract import cfg as GC
        t = (GC().get("image_thresholds") or {}).get("Q10_accept") or {}
        if t.get("people_count_strict"):
            return True
        if "count" in (GC().get("blocking_items") or []):   # REVIEW-B 11-05：L-196 人数是硬项 → 严格人数闸不随首帧档（base35 / turbo_previz）变
            return True
        from ns_agent import frame_job as FJ
        if project is None:
            from ns_agent.gen_contract import style as ST
            project = ST._run()
        return FJ.first_frame_tier(project)[0] == "base35"
    except Exception:  # noqa: BLE001  # arch-ok: 读不到配置 = 不开严格闸（只回到原有 people_count 判法）
        return False


def people_range(cast: list | None) -> tuple[int, int] | None:
    """分镜 cast → (最少, 最多)可见人数：最少 = 在画且露脸；最多 = 在画 + 局部 + 背影。没有 cast → None。
    PS-MASTER 11-05：过肩前景只露肩 / 后脑、背影、被遮的人（cast 项 face_hidden=True，ns_agent.prompt_template_v2.hidden_people 判）
    计入上限、不计入下限——看图模型可能只数出露脸的人"""
    if not cast:
        return None
    on = [c for c in cast if c.get("presence") == "在画"]
    lo = sum(1 for c in on if not c.get("face_hidden"))
    hi = len(on) + sum(1 for c in cast if c.get("presence") in ("局部", "背影"))
    return lo, hi


def people_verdict(visible, cast: list | None) -> str | None:
    """→ 不符说明；符合 / 看图没给整数 / 没有 cast → None（不新增判定）"""
    try:
        n = int(visible)
    except (TypeError, ValueError):
        return None
    rng = people_range(cast)
    if rng is None:
        return None
    lo, hi = rng
    if n > hi:
        return f"画面可见 {n} 人，分镜最多 {hi} 人（在画 {lo}）：多画了人"
    if n < lo:
        return f"画面可见 {n} 人，分镜在画 {lo} 人：少人"
    return None


def prompt_check(sid: str, image, via_tunnel: bool = False, stage: str = "image") -> dict:
    from ns_agent.vqa import client as VC
    from ns_agent.vqa import continuity as CT
    from ns_agent.vqa.contract import rubric_loads
    from ns_agent.gen_contract.accept import shot_description
    # W38：尾帧核结束状态（与共用验收 QB 同一函数），不再拿首帧描述核尾帧
    text = shot_description(CT.shot_rec(sid) or {}, "last" if stage == "last" else None)
    if not text:
        return {"status": "untested", "why": "没有编译稿"}
    if via_tunnel:
        _up = VC._local_up
        VC._local_up = lambda u: False if u.rstrip("/").endswith(":8870") else _up(u)
    strict = people_gate_on()
    res = VC.generate({"system": "你是画面质检员，只看图、只报事实，只输出 JSON。", "text": (PEOPLE_Q if strict else "") + PROMPT_Q + text[:1800],
                       "images": [{"path": str(image), "label": "【待检】", "max_pixels": 600000}]}, wait_s=900)
    if "error" in res:
        return {"status": "untested", "why": "本地看图没跑成：" + str(res["error"])[:150]}
    d = rubric_loads(res.get("text", "")) or {}
    if d.get("positions") == "不符":
        d, undecided = position_selfcheck(sid, image, d)
        if undecided:
            # 11-04 判词自洽：模型把“原文”左右读反（ep01-S15 v1/v2），重问仍矛盾 → untested（infra：只重打分，不算 fail、不计重出次数）
            return {"status": "untested", "checks": d, "infra": True, "why": "站位判词与编译输入矛盾、重问仍未定：" + str(d.get("e") or "")[:120]}
    bad = [k for k in ("people_count", "positions", "action", "scene") if d.get(k) == "不符"]
    people = None
    if strict:
        # W-DETECT 11-06：严格人数闸以“可见人数（整数）vs 分镜 cast 区间”为准（结构化 people），看图模型自由判的 people_count=不符
        # 只在它没给整数时才算数——ep01-S2 7 张：cast 唐瑶=局部背影（上限 2 人）、画面 2 人，模型却按“编译稿只写 1 人”判不符；
        # S15 5 张抬头写 people_count 实为站位 / 动作不符。blocking._q10_prompt_people_bad 读 people.status 判人数硬项
        try:
            from tools.vqa import anomaly as AN
            cast = AN.shot_cast(sid)
        except Exception:  # noqa: BLE001  # arch-ok: 取不到 cast = 不加判定（原有 people_count 判法照旧）
            cast = None
        rng = people_range(cast)
        pv = people_verdict(d.get("visible_people"), cast)
        if rng is not None and isinstance(d.get("visible_people"), int):
            people = {"status": "fail" if pv else "pass", "visible": d["visible_people"], "range": list(rng), "why": pv}
            if not pv and "people_count" in bad:
                bad.remove("people_count")
                d["people_count_vlm"] = "不符（只记录：可见人数 %d 在分镜区间 %d–%d 内）" % (d["visible_people"], rng[0], rng[1])
        elif pv:
            people = {"status": "fail", "visible": d.get("visible_people"), "range": list(rng) if rng else None, "why": pv}
        if pv and "people_count" not in bad:
            return {"status": "fail", "checks": d, "people": people, "why": "people_count 不符：" + pv}
    if bad:
        out = {"status": "fail", "checks": d, "why": "、".join(bad) + "不符：" + str(d.get("e") or "")[:120]}
        if people is not None:
            out["people"] = people
        return out
    if not d:
        return {"status": "untested", "why": "看图输出不是 JSON"}
    out = {"status": "pass", "checks": d}
    if people is not None:
        out["people"] = people
    return out


def count_check(sid: str, image, feat: dict | None = None, host: str | None = None) -> dict:
    from tools.vqa import anomaly as AN
    cast = AN.shot_cast(sid)
    if feat is None:
        try:
            feat = AN.measure({"x": Path(image)}, host or os.environ.get("OFFLINE_SELF") or "gpu36").get("x") or {}
        except Exception as e:  # noqa: BLE001 — 检测器没跑成 = untested（闸门挡，记 score_infra），不当通过
            return {"status": "untested", "why": f"检测器没跑成：{type(e).__name__}: {e}"[:200], "infra": True}
    if not feat or feat.get("error"):
        return {"status": "untested", "why": "检测器没跑成：" + str((feat or {}).get("error") or "无结果")[:150], "infra": True}
    faced = any(c.get("faced") for c in cast or [])
    if not feat.get("refs_loaded") and (faced or feat.get("faces") or feat.get("yolo_faces")):
        # L-126 失败关闭（VQAGATE 11-02 20:1x 实测：同一张陌生人图，带身份参照判 extra_head，不带参照判干净）：本剧身份脸一张都没加载
        # （V2 清单还没合进身份脸）→ 认不出脸是谁、陌生人 / 多出人头判不了 → untested，不当通过
        return {"status": "untested", "why": "身份参照没加载（本剧资产清单里没有身份脸），陌生人 / 多出人头判不了"}
    r = AN.judge(feat, cast)
    m = r.get("metrics") or {}
    if r.get("untested") and not [d for d in r["defects"] if d.get("conf") != "low"]:
        return {"status": "untested", "why": "；".join(r["untested"])[:200]}          # judge 判未测（身份参照没加载）= 未测，不当通过
    hits = [d for d in r["defects"] if d.get("conf") != "low"]
    # 单人镜的站位（blocking_mismatch）记日志、不拦（VQAGATE 11-02 17:2x 亲看 ep01-S3 / S4：单人近景脸在画面正中附近 0.47–0.54，
    # 线 0.55，编译稿没写左右，判“站位不符”偏严；多人镜左右站反才是真错，照拦）。口径待总调度定
    single = (m.get("expected_faced") or 0) <= 1
    noted = [d for d in hits if single and d.get("code") == "blocking_mismatch"]
    hits = [d for d in hits if d not in noted]
    return {"status": "fail" if hits else "pass", "heads": f"{m.get('heads_real')}/{m.get('expected_faced')}", "hands": m.get("hands"),
            "defects": [f"{d['code']}：{d['detail']}"[:160] for d in hits], "noted": [f"{d['code']}：{d['detail']}"[:160] for d in noted],
            "mirror_seg": r.get("mirror_seg")}


def summarize(lab: dict) -> str:
    """deferred（推迟到批量验收再做，总调度 11-02 17:4x：gpu33 上不跑人数检测）不计入：既不算失败也不算通过"""
    st = [lab[k]["status"] for k in ORDER if k in lab and lab[k]["status"] != "deferred"]
    if "fail" in st:
        return "fail"
    if "untested" in st:
        return "untested"
    return "pass"


def _parallel_on() -> bool:
    """config/gen_contract.json image_thresholds.Q10_accept.parallel（默认关）"""
    try:
        from ns_agent.gen_contract import cfg as GC
        return bool(((GC().get("image_thresholds") or {}).get("Q10_accept") or {}).get("parallel"))
    except Exception:  # noqa: BLE001  # arch-ok: 读不到配置 = 关（原串行口径）
        return False


def label(sid: str, stage: str, image, pf: dict, *, feat: dict | None = None, via_tunnel: bool = False, host: str | None = None,
          defer_count: bool = False, skip_outfit: bool = False) -> dict:
    """skip_outfit（L-205 P2，contract.skip_nonblocking("Q10")）：服装不在硬项名单 → 不跑“谁穿什么”看图，记 na skipped_nonblocking
    （summarize 不算 fail / untested）。身份（契约 Q1）、提示词核对（含看图人数 people_count，count 硬项）、人数检测照跑"""
    skipped = {"status": "na", "record_only": True, "skipped": "skipped_nonblocking", "why": "服装非硬项（L-196），L-205 不跑看图"}
    deferred = {"status": "deferred", "why": "本机没有检测卡（gpu33 的 GPU2 是坏卡）：人数检测由笔记本发起的批量验收在 gpu36 上做，"
                                             "结果写 data/vqa/accept/epNN.json，视频闸门读它"}
    if _parallel_on() and not via_tunnel:
        # B1（11-05，默认关）：提示词核对 / 谁穿什么（各自调看图）/ 人数检测（打 :8893）互不依赖，并发发出；
        # 看图侧靠 client 多实例分发（route.balance）才真正并行，单实例时只是把排队交给服务端、总耗时不变也不变差。各项判定与证据照旧
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="q10") as ex:
            f_pr = ex.submit(prompt_check, sid, image, False, stage)
            f_cnt = None if defer_count else ex.submit(count_check, sid, image, feat, host)
            f_out = None if skip_outfit else ex.submit(outfit, pf, sid, image)
            pr, out = f_pr.result(), (skipped if f_out is None else f_out.result())
            cnt = deferred if f_cnt is None else f_cnt.result()
    else:
        pr = prompt_check(sid, image, via_tunnel, stage)      # 先跑：via_tunnel 时它会把本机 8870 判为不可用，后面的看图都走隧道
        cnt = deferred if defer_count else count_check(sid, image, feat, host)
        out = skipped if skip_outfit else None
    if stage == "last" and pr.get("status") in ("fail", "untested"):
        # L-183（总调度 11-04 04:1x/04:2x）：尾帧不是成片输入（L-088 Pika 只给首帧）→ 提示词 positions/action 核对只记录、不阻断；
        # 原结论留在 would / why 里（审片可见）。身份 / 造型 / 人数照样判
        pr = {**pr, "status": "na", "record_only": True, "would": pr.get("status"), "why": "尾帧只记录：" + str(pr.get("why") or "")}
    lab = {"identity": identity(pf), "outfit": out if out is not None else outfit(pf, sid, image), "prompt": pr, "count": cnt}
    lab["ok"] = summarize(lab)
    lab["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return lab


def apply(pf: dict, lab: dict) -> dict:
    """标签 → postflight 的 Q10 项（闸门）。fail / untested 都进 blocking；只因检测器 / 看图没跑成的 untested 标 score_infra"""
    st = lab["ok"]
    why = "；".join(f"{k} {lab[k]['status']}" + (f"（{lab[k].get('why') or '；'.join(lab[k].get('defects') or [])}）" if lab[k]["status"] != "pass" else "")
                   for k in ORDER if k in lab)
    item = {"id": "Q10", "title": "出图后验收（身份 / 服装 / 提示词 / 人数，四项全过）", "status": st, "detail": why[:600],
            "evidence": {k: lab[k] for k in ORDER if k in lab}}
    pf["items"] = [i for i in pf.get("items") or [] if i.get("id") != "Q10"] + [item]
    if st != "pass":
        pf["ok"] = False
        pf["blocking"] = sorted(set(pf.get("blocking") or []) | {"Q10"})
        pf.setdefault("reasons", []).append("Q10 " + why[:300])
        infra = [k for k in ORDER if lab.get(k, {}).get("status") == "untested"]
        if st == "untested" and set(pf["blocking"]) <= {"Q10", "Q9"} and infra:
            pf["score_infra"] = True
            # W-SCORE 11-07：原来只标 score_infra、不带原因 → producer 日志 error=""，看不出是人数检测连不上（gpu36 自连内网入口被拒）
            pf["infra_error"] = infra_error_text(lab, infra)
    return item


def infra_error_text(lab: dict, infra: list) -> str:
    """Q10 哪几项没测成 + 各自原因 → 一行（永不为空）：'Q10 未测 count：检测器没跑成：…；prompt：本地看图没跑成：…'"""
    parts = [f"{k}：{str((lab.get(k) or {}).get('why') or '无原因字段')[:160]}" for k in infra]
    return ("Q10 未测 " + "；".join(parts))[:400] if parts else "Q10 未测（无未测子项记录）"


# ───────────────────────── 批量（ep01 表 / 回归集） ─────────────────────────
def _take_pf(sid: str, file: Path) -> dict:
    ep, s = sid.split("-")
    d = json.loads((ROOT / "data" / "renders" / ep / s / "takes.json").read_text(encoding="utf-8"))
    t = next((x for x in d.get("takes") or [] if Path(str(x.get("file") or "")).name == file.name), None)
    return (t or {}).get("postflight") or {}


def run_shots(prefix: str, feats_path: str | None, via_tunnel: bool) -> list[dict]:
    from tools.vqa import anomaly as AN
    feats = json.loads(Path(feats_path).read_text(encoding="utf-8")) if feats_path else {}
    rows = []
    for sid, st, p in AN.current_images(True):
        if not sid.startswith(prefix + "-"):
            continue
        k = f"{sid}|{st}"
        lab = label(sid, st, p, _take_pf(sid, Path(p)), feat=feats.get(k), via_tunnel=via_tunnel)
        rows.append({"key": k, "file": str(p), **lab})
        print(k, lab["ok"], *(f"{x}={lab[x]['status']}" for x in ORDER), flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = OUT / f"{prefix}.json.tmp"
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, OUT / f"{prefix}.json")
    return rows


def legacy_run() -> str:
    """无前缀老路径的那部剧（人工标注回归集的出处）：只从运行登记表读（tools/autopilot/runs.legacy_run，hard_run）"""
    from tools.autopilot import runs
    return runs.legacy_run()


def regression() -> int:
    """人工标注回归集（data/vqa/regression_anomaly/labeled.json）：每条 label 的 true_issue / false_positive 都要判对"""
    from tools.vqa import anomaly as AN
    d = json.loads((ROOT / "data/vqa/regression_anomaly/labeled.json").read_text(encoding="utf-8"))
    bad = n = 0
    old = os.environ.get("NS_RUN_ID")
    for k, v in d.items():
        # 回归样本按它所属的剧判（人物清单、身份参照随剧走；这批人工标注都是 V1 的，缺省 series10_v1）——主剧切到 V2 也不变口径
        os.environ["NS_RUN_ID"] = v.get("run") or legacy_run()
        try:
            codes = {x["code"] for x in AN.judge(v["feat"], v["cast"])["defects"] if x.get("conf") != "low"}
        finally:
            if old is None:
                os.environ.pop("NS_RUN_ID", None)
            else:
                os.environ["NS_RUN_ID"] = old
        for code, lab in v["labels"].items():
            n += 1
            if (lab["label"] == "true_issue") != (code in codes):
                bad += 1
                print("BAD", k, code, "应", lab["label"], "得", sorted(codes))
    print(f"ACCEPT_REGRESSION {'PASS' if not bad else 'FAIL'}：{n - bad}/{n}")
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots")
    ap.add_argument("--feats")
    ap.add_argument("--via-tunnel", action="store_true")
    ap.add_argument("--regression", action="store_true")
    a = ap.parse_args(argv)
    if a.regression:
        return regression()
    if a.shots:
        run_shots(a.shots, a.feats, a.via_tunnel)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
