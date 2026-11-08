"""契约 Q9（出图）/ V9（视频）“本地看图复核”：把本地看图模型判定变成一条契约项。

只记录期（默认）：有缺陷 → status "na" + evidence.would_fail=True（与 postflight 的 record_only 口径一致）。
某检查项 config/vqa.json checks.<id>.blocking = true（总调度按标定 ≥0.85 批准后才改）→ 该项缺陷 = "fail"。
underage_look 从第一天起阻断（HC-07）。没有判定 / 服务不可用 = "untested"（不算通过）。

REFLOCK 在 postflight.evaluate 里加（一处）：
    from ns_agent.vqa import contract as VQ
    st, d, ev = VQ.q9(vlm)            # vlm = payload.get("vlm") 或 VQ.cached_for(image)
    add("Q9", st, d, **ev)            # TITLES["Q9"] = VQ.TITLE
VIDEO10 / evaluate_video 同理用 VQ.v9(score.get("vlm"))，VTITLES["V9"] = VQ.VTITLE。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import ROOT, cfg

TITLE = "本地看图复核（VLM，多手 / 鞋 / 造型 / 身份 / 多人 / 场景 / 文字 / 看镜头 / 塑料感 / 坏块 / 低清）"
VTITLE = "本地看图复核（VLM 抽帧：片内一致 / 变形 / 开头盯镜头 + 出图各项）"
INDEX = ROOT / "data" / "vqa" / "index.json"     # 图片相对路径 → 判定缓存键（client 写）


def _all_checks() -> dict:
    c = cfg()
    return {**(c.get("checks") or {}), **(c.get("video_checks") or {})}


def _fmt(ids: list[str], v: dict) -> str:
    zh = {k: x.get("zh", k) for k, x in _all_checks().items()}
    return "；".join(f"{zh.get(i, i)}：{(v.get('checks') or {}).get(i, {}).get('e', '')}".rstrip("：") for i in ids)


def gate_cfg() -> dict:
    """config/vqa.json gate（VQAGATE 11-01，项目方原话转阻断，D-VQA-01）"""
    return cfg().get("gate") or {}


def enforced() -> bool:
    return bool(gate_cfg().get("enforce")) and bool((cfg().get("judge") or {}).get("enforce"))


SKIPPED = "skipped_nonblocking"


def skip_nonblocking(item_id: str) -> bool:
    """L-205 P2（KPI 一集 ≤50 min）：config/gen_contract.json vqa_skip_nonblocking=true 且 L-196 blocking_items 生效时，
    不在硬项名单里的看图子检查（Q1a 逐人头发看图、Q1x 同场跨镜、Q10 服装）不再跑——反正只记录不挡，白占看图 GPU。
    名单里显式写了该项 id → 照跑。硬项（Q1 身份复核带 / 人数 / 文字 / 成人 / 视线 / FL）永远照跑（不经过这里）"""
    from ns_agent.gen_contract import cfg as GC
    from ns_agent.gen_contract import blocking as BK
    c = GC()
    if c.get("vqa_skip_nonblocking") is not True:
        return False
    hard = BK.hard_set(c)
    return hard is not None and item_id not in hard


def _item(v: dict | None, video: bool) -> tuple[str, str, dict]:
    if not v:
        return "untested", "没有本地看图复核结果（服务排队 / 未跑）", {}
    if v.get("error"):
        return "untested", f"本地看图复核失败：{v['error']}", {"key": v.get("key")}
    sanitize(v)                        # 唯一出口：不管哪条路径来的看图结果，都先查“结论与证据矛盾”（总调度 11-02 21:xx）
    checks = _all_checks()
    defects = [d for d in v.get("defects") or [] if d in checks]
    # 只有 defect（高置信度）且该项 blocking 才拦；unsure = 低置信度只记录。enforce 关掉时只有 underage_look 拦（HC-07）
    blocking = [d for d in defects if (checks[d] or {}).get("blocking") and (enforced() or d == "underage_look")]
    record = [d for d in defects if d not in blocking]
    ev = {"defects": defects, "unsure": v.get("unsure") or [], "key": v.get("key"), "model": v.get("model"),
          "maps_to": {d: checks[d].get("video" if video and "video" in checks[d] else "contract") for d in defects},
          "record_only": bool(record and not blocking)}
    if blocking:
        return "fail", _fmt(blocking, v) + ("；另有只记录项：" + _fmt(record, v) if record else ""), ev
    if v.get("contradiction"):
        ev["contradiction"] = v["contradiction"]
        return "untested", "看图模型的结论与证据矛盾、重问后仍未定：" + _fmt(list(v["contradiction"]), v), ev
    if record:
        ev["would_fail"] = True
        return "na", "只记录（不阻断，待标定）：" + _fmt(record, v), ev
    return "pass", "本地看图模型未报缺陷" + (f"（拿不准：{'、'.join(ev['unsure'])}）" if ev["unsure"] else ""), ev


def q9(v: dict | None) -> tuple[str, str, dict]:
    return _item(v, False)


def v9(v: dict | None) -> tuple[str, str, dict]:
    return _item(v, True)


def cached_for(image: str | Path) -> dict | None:
    """postflight 拿不到 payload["vlm"] 时：按图片路径查 data/vqa/index.json → data/vqa/verdicts/<key>.parsed.json"""
    try:
        idx = json.loads(INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    p = str(image).replace("\\", "/")
    rel = p.split("NS-Final Studio/", 1)[-1]
    key = idx.get(rel) or idx.get(p)
    f = ROOT / "data" / "vqa" / "verdicts" / f"{key}.parsed.json" if key else None
    if f and f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return None


def remember(image: str | Path, verdict: dict) -> None:
    """把判定写进索引（client 调用后由调用方存）"""
    if not verdict.get("key"):
        return
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    try:
        idx = json.loads(INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        idx = {}
    rel = str(image).replace("\\", "/").split("NS-Final Studio/", 1)[-1]
    idx[rel] = verdict["key"]
    INDEX.write_text(json.dumps(idx, ensure_ascii=False, indent=0), encoding="utf-8")
    (ROOT / "data" / "vqa" / "verdicts" / f"{verdict['key']}.parsed.json").write_text(
        json.dumps(verdict, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- VQAGATE：出图闸门（接在 PRODUCER _postflight / gate_scan 上）
def inspect_for(sid: str, kind: str, image: str | Path, prompt: str, references: list[str] | None, **kw) -> dict:
    """本地看图（带跨镜连贯参考）→ 判定；写 index 供 cached_for 查。kind = first | last"""
    from . import client
    from . import continuity as CT
    extra, note = CT.refs_for(sid, kind)
    me = str(Path(image).resolve())
    extra = [(f, lab) for f, lab in extra if str(Path(f).resolve()) != me]
    v = client.inspect_image(str(image), prompt or "", list(references or []), extra_refs=extra,
                             spec={"continuity": note} if note else None, **kw)
    if v.get("error") == "看图模型输出不是 JSON":      # 偶发格式坏（DEBUG-RSI 11-02 gpu33 score_infra）：重看一次，不当基础设施失败
        v = client.inspect_image(str(image), prompt or "", list(references or []), extra_refs=extra,
                                 spec={"continuity": note} if note else None, **{**kw, "refresh": True})
    v["continuity_ref"] = [f for f, _ in extra]
    if not v.get("error"):
        faces = gaze_mp(image)
        merge_gaze(v, faces, prompt or "")
        recheck_gaze_band(v, faces, image, prompt or "")
    remember(image, v)
    return v


GAZE_Q = ("这是从一张图里裁出来的一个人的脸。只判断他 / 她的眼睛（瞳孔）是不是正对镜头、看着观众，输出 JSON："
          '{"gaze": "看镜头|看别处|闭眼或垂眼|拿不准", "e": "依据，不超过 20 字"}。头偏了但瞳孔转回来正对观众的也算看镜头。')


def recheck_gaze_band(v: dict, faces: list[dict] | None, image, prompt: str) -> None:
    """看镜头拿不准带复判（L-102：总调度亲审的漏检 s20261304 落成判定）：MediaPipe 分数落在 (T, 2T] 的“差一点”带、
    俯仰 / 垂眼 / 闭眼条件都满足、看图模型整图没判 eye_contact → 把这张脸裁出来单独问本地看图（K-REV-V01）；答“看镜头”= eye_contact defect"""
    g = gate_cfg().get("gaze_mp") or {}
    if not faces or not g.get("enabled", True) or (v.get("checks") or {}).get("eye_contact", {}).get("s") == "defect":
        return
    from . import rubric
    if rubric.spec_from_prompt(prompt).get("camera_ok") == "允许":
        return
    A, T, P = float(g.get("A", 0.3)), float(g.get("T", 0.05)), float(g.get("P", 0.12))
    D, BL, band = float(g.get("down_max", 0.35)), float(g.get("blink_max", 0.6)), float(g.get("band", 2.0))
    cand = [f for f in faces if f.get("ybox") and T < abs(A * f["yaw_n"] + f["eye_x"]) <= band * T and f["pitch_n"] <= P
            and f.get("bs_y", 0) <= D and f.get("blink", 0) <= BL]
    if not cand:
        return
    try:
        from PIL import Image
        from . import client
        im = Image.open(image).convert("RGB")
        out_dir = ROOT / "data" / "vqa" / "crops"
        out_dir.mkdir(parents=True, exist_ok=True)
        answers = []
        for k, f in enumerate(cand):
            x, y, w, h = f["ybox"]
            c = im.crop((int(max(0, x - 0.4 * w)), int(max(0, y - 0.4 * h)), int(min(im.width, x + 1.4 * w)), int(min(im.height, y + 1.3 * h))))
            fp = out_dir / f"{Path(str(image)).stem}_gaze{k}.jpg"
            c.save(fp, quality=92)
            res = client.generate({"system": "你是画面质检员，只看图、只报事实，只输出 JSON。", "text": GAZE_Q,
                                   "images": [{"path": str(fp), "label": "【裁切】", "max_pixels": 200000}]}, wait_s=900)
            d = rubric_loads(res.get("text", "")) if "error" not in res else None
            answers.append((d or {}).get("gaze") or "拿不准")
    except Exception as e:  # noqa: BLE001
        v["gaze_recheck"] = f"复判失败：{e}"[:200]
        return
    v["gaze_recheck"] = answers
    if "看镜头" in answers:
        c = v.setdefault("checks", {})
        c["eye_contact"] = {"s": "defect", "e": f"拿不准带裁脸复判：{answers.count('看镜头')} 张脸看镜头（MediaPipe 分数在 {T}–{band * T}）"}
        v["defects"] = sorted(set(v.get("defects") or []) | {"eye_contact"})
        v["unsure"] = [u for u in v.get("unsure") or [] if u != "eye_contact"]


def gaze_mp(image: str | Path) -> list[dict] | None:
    """看镜头旁证（同一套 Q9 的 eye_contact，不另起检测器）：MediaPipe 虹膜（packages/qc/ns_qc/gaze_mp.py），
    YuNet 框裁切放大跑小脸。只在装了 mediapipe 的项目 .venv 里有；没有 → None"""
    try:
        import sys
        qc = str(ROOT / "packages" / "qc")
        if qc not in sys.path:
            sys.path.insert(0, qc)
        import cv2
        from ns_qc import gaze as GZ
        from ns_qc import gaze_mp as GM
        bgr = cv2.imread(str(image))
        if bgr is None or GM.landmarker() is None:
            return None
        m = GZ._models()
        boxes = [tuple(float(x) for x in f[:4]) for f in (m.faces(bgr) if m else [])]
        out = []
        for b in boxes:
            if b[3] < 0.03 * bgr.shape[0]:
                continue
            f = GM.face_in_box(bgr, b)
            if f:
                out.append({**f, "ybox": list(b)})        # 原图坐标的 YuNet 框（拿不准时裁脸复判用）
        return out if boxes else (GM.faces(bgr) or [])
    except Exception:  # noqa: BLE001
        return None


_NEG_EYE = ("非看镜头", "不是看镜头", "未看镜头", "没有看镜头", "没看镜头", "不看镜头", "均未看", "都没有看", "无人看镜头", "没有人看镜头")


_NEG_LIMB = ("各5指", "各 5 指", "被遮挡", "遮挡", "数量正常", "无多余", "没有多余", "未见多余", "正常")


def per_person_identity(v: dict) -> None:
    """identity_drift 逐人判（IMGFIX 11-02：原来整张图判一次，多人镜第二人不像也放过）：people[].identity 有人“不像” → identity_drift = defect"""
    bad = [p for p in v.get("people") or [] if isinstance(p, dict) and str(p.get("identity") or "").strip() == "不像"]
    if not bad:
        return
    c = v.setdefault("checks", {})
    if (c.get("identity_drift") or {}).get("s") != "defect":
        c["identity_drift"] = {"s": "defect", "e": "逐人：" + "、".join(f"{p.get('who') or '?'}（{p.get('pos') or '?'}）不像身份参考" for p in bad)}
        v["defects"] = sorted(set(v.get("defects") or []) | {"identity_drift"})
        v["unsure"] = [u for u in v.get("unsure") or [] if u != "identity_drift"]


def sanitize(v: dict) -> None:
    """自相矛盾的 defect 降成 unsure：多肢 / 手指（ep02-S8：判 extra_limbs defect、证据却写“双手各5指，但左手小指被遮挡”）"""
    per_person_identity(v)
    for k in ("extra_limbs", "hand_malformed"):
        c = (v.get("checks") or {}).get(k) or {}
        e = str(c.get("e") or "")
        if c.get("s") == "defect" and any(n in e for n in _NEG_LIMB) and not re.search(r"(多|三|3)\s*只手|多出|第三只|多一只", e):
            c.update(s="unsure", e=f"（自相矛盾，降为拿不准）{e}"[:300])
            v["defects"] = [d for d in v.get("defects") or [] if d != k]
            v["unsure"] = sorted(set(v.get("unsure") or []) | {k})
    _sanitize_eye(v)


_POS_EYE = ("直视镜头", "正对镜头", "看向镜头", "看着镜头", "盯着镜头", "瞳孔朝向镜头", "朝向镜头", "眼神对镜", "与镜头对视", "看镜头")
_DIR_AWAY = re.compile(r"(朝向|看向|望向|看着|视线(朝|向|落在)?)\s*(画面)?(左|右|上|下|外|远处|别处|对岸|窗外|地面|他|她|对方|手机|桌面|侧面)")


def eye_contradiction(c: dict) -> bool:
    """eye_contact 的结论和证据互相矛盾（总调度 11-02 21:xx：V2 金样 image_000_90d1cc8e 判“看镜头”，证据写“右眼瞳孔朝向画面右，未看镜头”）：
    判 defect 但证据只说没看 / 朝别处；或判 ok 但证据只说看镜头"""
    e = str(c.get("e") or "")
    neg = any(n in e for n in _NEG_EYE) or bool(_DIR_AWAY.search(e))
    pos = any(k in e for k in _POS_EYE) and not any(n in e for n in _NEG_EYE)
    if c.get("s") == "defect":
        old = neg and not pos
    elif c.get("s") == "ok":
        old = pos and not neg
    else:
        old = False
    # 11-04：“非直视镜头 / 未直视 / 看向右下鞋尖”这类否定短语里的“直视镜头”不能当肯定（ep01-S16 v2、S8 尾帧判 defect 的证据）
    from . import selfcheck
    return old or selfcheck.gaze_contradiction(str(c.get("s")), e)


def _sanitize_eye(v: dict) -> None:
    """看图模型自相矛盾的 eye_contact（ep03-S4：判 defect、证据却写“两人均未看镜头”；V2 金样 90d1cc8e：判看镜头、证据“朝向画面右，未看镜头”）
    → 改记 untested（不再降成拿不准放过）+ 登记 v["contradiction"]，由 reask_eye 重问一次；gaze_mp 高置信度命中会在 merge_gaze 里直接定 defect"""
    c = (v.get("checks") or {}).get("eye_contact") or {}
    if c.get("s") not in ("defect", "ok"):
        return
    e = str(c.get("e") or "")
    looks = any("镜头" in str((p or {}).get("gaze") or "") and not any(n in str((p or {}).get("gaze")) for n in ("不", "未", "没"))
                for p in v.get("people") or [] if isinstance(p, dict))
    people_say_no = c.get("s") == "defect" and v.get("people") and not looks and not any(k in e for k in _POS_EYE)
    if eye_contradiction(c) or people_say_no:
        c.update(s="untested", e=f"（结论 {c.get('s')} 与证据矛盾，待重问）{e}"[:300], orig_s=c.get("s"))
        v["defects"] = [d for d in v.get("defects") or [] if d != "eye_contact"]
        v["unsure"] = [u for u in v.get("unsure") or [] if u != "eye_contact"]
        v["contradiction"] = sorted(set(v.get("contradiction") or []) | {"eye_contact"})


REASK_Q = ("只看【待检】图里人物的眼睛和瞳孔方向，回答这个人是不是在看镜头（看向观众）。只输出 JSON："
           '{"gaze": "看镜头|没看镜头|看不清", "e": "一句依据：眼睛 / 瞳孔朝哪个方向"}。结论必须和依据一致。')


def reask_eye(v: dict | None, image) -> str | None:
    """eye_contact 证据与结论矛盾时重问一次（只问视线，K-REV-V01 本地看图）：回答自洽 → 按回答定（看镜头 = defect / 没看 = ok），
    消掉矛盾；仍矛盾 / 看不清 / 没跑成 → 保持 untested（Q9 挡）。→ 说明或 None"""
    if not v or "eye_contact" not in (v.get("contradiction") or []):
        return None
    from . import client
    res = client.generate({"system": "你是画面质检员，只看图、只报事实，只输出 JSON。", "text": REASK_Q,
                           "images": [{"path": str(image), "label": "【待检】", "max_pixels": 600000}]}, wait_s=900, refresh=True)
    d = rubric_loads(res.get("text", "")) if "error" not in res else None
    c = v.setdefault("checks", {}).setdefault("eye_contact", {})
    if not d or d.get("gaze") not in ("看镜头", "没看镜头"):
        c["reask"] = d or {"error": res.get("error")}
        return None
    ans = {"s": "defect" if d["gaze"] == "看镜头" else "ok", "e": str(d.get("e") or "")}
    if eye_contradiction(ans):
        c["reask"] = d
        return None
    c.update(s=ans["s"], e=f"（重问）{ans['e']}"[:300], reask=d)
    v["contradiction"] = [x for x in v.get("contradiction") or [] if x != "eye_contact"]
    if ans["s"] == "defect":
        v["defects"] = sorted(set(v.get("defects") or []) | {"eye_contact"})
    return f"eye_contact 重问后定为 {ans['s']}：{ans['e'][:60]}"


def refresh_gaze(v: dict, image, prompt: str) -> None:
    """旧判定里 gaze_mp 记的 eye_contact 按现行阈值重算（阈值改过时用）：先还原看图模型原判，再重跑 merge_gaze"""
    c = (v.get("checks") or {}).get("eye_contact") or {}
    if str(c.get("e") or "").startswith("gaze_mp"):
        m = re.search(r"看图模型原判 (\w+)", c["e"])
        orig = m.group(1) if m else "ok"
        v["checks"]["eye_contact"] = {"s": orig, "e": ""}
        v["defects"] = [d for d in v.get("defects") or [] if d != "eye_contact"] + (["eye_contact"] if orig == "defect" else [])
        v.pop("gaze_mp", None)
    if "gaze_mp" not in v:
        merge_gaze(v, gaze_mp(image), prompt)
    else:
        sanitize(v)


def merge_gaze(v: dict, faces: list[dict] | None, prompt: str) -> None:
    """高置信度看镜头（config gate.gaze_mp：|A×yaw + eye_x| ≤ T 且 pitch ≤ P，标定见 docs/vqa/GATE_CALIBRATION.md）
    且原文没写看镜头 → 把 eye_contact 记成 defect（证据注明 gaze_mp）。VLM 已判 defect 的不动"""
    sanitize(v)
    g = gate_cfg().get("gaze_mp") or {}
    if faces is None or not g.get("enabled", True):
        return
    from . import rubric
    A, T, P = float(g.get("A", 0.3)), float(g.get("T", 0.05)), float(g.get("P", 0.12))
    D, BL = float(g.get("down_max", 0.35)), float(g.get("blink_max", 0.6))
    # 垂眼 / 闭眼排除（总调度 18:4x：ep04-S2 林晚垂眼看碗，眼睑下垂被当成正视）：blendshape eyeLookDown（bs_y）> D 或 eyeBlink > BL 不算
    hits = [f for f in faces if abs(A * f["yaw_n"] + f["eye_x"]) <= T and f["pitch_n"] <= P
            and f.get("bs_y", 0) <= D and f.get("blink", 0) <= BL]
    v["gaze_mp"] = {"faces": [{k: f.get(k) for k in ("yaw_n", "pitch_n", "eye_x", "gaze_x", "bs_y", "blink")} for f in faces][:4],
                    "hits": len(hits)}
    c0 = v.setdefault("checks", {})
    if (g.get("primary") and not hits and faces and (c0.get("eye_contact") or {}).get("s") == "defect"
            and len(faces) >= len([p for p in v.get("people") or [] if isinstance(p, dict)])
            and all(abs(A * f["yaw_n"] + f["eye_x"]) > float(g.get("band", 2.0)) * T for f in faces)):
        # 总调度 11-02（0.5 / 0.6 MP 之间 VLM eye_contact 来回翻，判定余量薄）：gaze_mp 主判——测到的每张脸都明显没看镜头（> band×T）、
        # 且测到的脸数不少于看图数到的人数 → VLM 的 defect 降为 ok（证据保留原判）。A/B 见 GATE_CALIBRATION §19，开关 gate.gaze_mp.primary
        c0["eye_contact"] = {"s": "ok", "e": f"gaze_mp 主判：{len(faces)} 张脸都明显没看镜头；看图模型原判 defect（{str(c0['eye_contact'].get('e') or '')[:60]}）"}
        v["defects"] = [d for d in v.get("defects") or [] if d != "eye_contact"]
        v["gaze_mp"]["primary_override"] = True
        return
    if not hits or rubric.spec_from_prompt(prompt).get("camera_ok") == "允许":
        return
    c = v.setdefault("checks", {})
    v["contradiction"] = [x for x in v.get("contradiction") or [] if x != "eye_contact"]    # 确定性高置信度命中直接定，不再待重问
    if (c.get("eye_contact") or {}).get("s") != "defect":
        c["eye_contact"] = {"s": "defect", "e": f"gaze_mp 虹膜测得 {len(hits)} 张脸正对镜头（|{A}×yaw+eye_x|≤{T}）"
                                                + (f"；看图模型原判 {c['eye_contact']['s']}" if c.get("eye_contact") else "")}
        v["defects"] = sorted(set(v.get("defects") or []) | {"eye_contact"})
        v["unsure"] = [u for u in v.get("unsure") or [] if u != "eye_contact"]


def apply(pf: dict, v: dict | None) -> dict:
    """把看图判定并进契约结果：替换 / 加上 Q9 项，按 enforce 重算 ok / blocking / reasons。原地改并返回 pf"""
    st, d, ev = q9(v)
    if st == "untested" and not (enforced() and gate_cfg().get("untested_blocks", True)):
        st, d = "na", "只记录（没有本地看图结果）：" + d
    items = [i for i in pf.get("items") or [] if i.get("id") != "Q9"]
    items.append({"id": "Q9", "title": TITLE, "status": st, "detail": d, "evidence": ev})
    pf["items"] = items
    old_blk = list(pf.get("blocking") or [])
    blk = [b for b in old_blk if b != "Q9"]
    pf["reasons"] = [r for r in pf.get("reasons") or [] if not str(r).startswith("Q9")]   # 旧的 Q9 说法（如“没有本地看图结果”）先清掉
    if st in ("fail", "untested"):
        blk.append("Q9")
        pf["reasons"] = pf["reasons"] + [f"Q9 {d}"]
    pf["blocking"] = blk
    relaxed = q1_yaw_relax(pf, v)                       # Q1 按转角放宽（要看图身份 ok，所以在这里和 Q9 一起算）
    if relaxed and not pf["blocking"]:
        pf["ok"] = True
    if pf.get("ok") is not None or st in ("fail", "untested"):
        # K-OPS-E10（DEBUG-RSI 11-02 02:5x）：原来 ok = 旧 ok and Q9 没挡——契约先按“Q9 未跑”判了 ok=False，看图回来 pass 也翻不回 True，
        # 结果 blocking 为空的图照样 redo_fail（gpu36 ep09-S2 v3：blocking []、vqa_gate pass，仍记“没过”）。现在 ok 只看挡住项是否为空
        others_bad = any(i.get("status") in ("fail", "untested") and i.get("id") != "Q9" and i.get("blocking", True) is not False
                         for i in pf.get("items") or [])
        q9_only = bool(pf.get("ok")) or set(old_blk) <= {"Q9"}       # 之前判不过只是因为 Q9（或本来就过）
        pf["ok"] = (not pf["blocking"]) and st not in ("fail", "untested") and q9_only and not others_bad
    pf["vqa_gate"] = {"q1_yaw_relax": relaxed, "status": st, "defects": ev.get("defects") or [], "unsure": ev.get("unsure") or [],
                      "key": ev.get("key"), "enforce": enforced(), "continuity_ref": (v or {}).get("continuity_ref")}
    return pf


def q1_yaw_relax(pf: dict, v: dict | None) -> str | None:
    """Q1 按转角放宽（总调度 11-01 23:0x 采纳 IMGFIX 方案；参数 config/gen_contract.json image_thresholds.Q1_identity.yaw_relax）：
    单脸镜里林晚身份分只差在转角掉分（Q1 只因“lin_wan x < 0.70”不过），MediaPipe 测得 |yaw_n| 落在放宽档、分数达到该档线，
    并且本地看图 identity_drift = ok → Q1 改判 pass（证据写明放宽档）。看图身份不是 ok（defect / unsure / 没测）→ 维持 0.70。
    → 改判说明或 None（原地改 pf）"""
    try:
        from ns_agent.gen_contract import cfg as gcfg
        yr = ((gcfg().get("image_thresholds") or {}).get("Q1_identity") or {}).get("yaw_relax") or {}
    except Exception:  # noqa: BLE001
        return None
    q1 = next((i for i in pf.get("items") or [] if i.get("id") == "Q1"), None)
    if not yr or not yr.get("enabled", True) or not q1 or q1.get("status") != "fail" or not v or v.get("error"):
        return None                                    # K-REV-V18 修订后转角放宽由三段判定 + 复核带取代（yaw_relax.enabled=false）
    ev = q1.get("evidence") or {}
    sims = ev.get("sims") or {}
    if len(sims) != 1 or ev.get("strangers") or "；" in str(q1.get("detail") or ""):
        return None                                    # 只处理“单脸、唯一不过原因是分数线”的情形
    cid, sim = next(iter(sims.items()))
    faces = ((v.get("gaze_mp") or {}).get("faces")) or []
    from ns_agent.gen_contract.cards import is_lead_listed     # CARRYOVER 第 10 项：名单里 "@lead" = 本剧主角（V2 lead_f）
    if not is_lead_listed(cid, yr.get("characters") or []) or sim is None or len(faces) != 1:
        return None
    if yr.get("require_vlm_identity_ok", True) and ((v.get("checks") or {}).get("identity_drift") or {}).get("s") != "ok":
        return None
    yaw = abs(float(faces[0].get("yaw_n") or 0))
    need = next((t for lim, t in yr.get("tiers") or [] if yaw < lim), None)
    if need is None or sim < need:
        return None
    note = f"Q1 按转角放宽：{cid} {sim:.3f} ≥ {need}（|yaw_n| {yaw:.2f}；看图身份 ok；原线 0.70）"
    q1.update(status="pass", detail=note + "；原判：" + str(q1.get("detail") or ""),
              evidence={**ev, "yaw_relax": {"yaw": round(yaw, 3), "need": need}})
    pf["blocking"] = [b for b in pf.get("blocking") or [] if b != "Q1"]
    pf["reasons"] = [r for r in pf.get("reasons") or [] if not str(r).startswith("Q1")]
    return note


BACK_Q = ("这是从一张图里裁出来的局部。请只判断这块区域是什么，输出 JSON："
          '{"kind": "正脸|侧脸|后脑或耳朵|不是人|拿不准", "e": "依据，不超过 20 字"}。'
          "看得见眼睛鼻子嘴的是正脸或侧脸；只有头发、后脑勺、耳朵、发际线、看不到五官的算“后脑或耳朵”。")


def recheck_strangers(pf: dict, image, out_dir: Path | None = None) -> str | None:
    """K-REV-V14（总调度 11-02，IMGFIX s20261304 / ep02-S4）：cast 里有背影 / 局部人物、Q1 只因“认不出的脸”不过时，
    把每张认不出的脸裁出来交本地看图模型复核（K-REV-V01，只走本地服务）：全部确认是后脑 / 耳朵 / 不是人 → 不算多出的人，Q1 改判 pass；
    有一张是正脸 / 侧脸 / 拿不准 → 照挡。→ 改判说明或 None（原地改 pf）"""
    q1 = next((i for i in pf.get("items") or [] if i.get("id") == "Q1"), None)
    if not q1 or q1.get("status") != "fail":
        return None
    ev = q1.get("evidence") or {}
    boxes = [b for b in ev.get("stranger_boxes") or [] if b]
    reasons = [r for r in str(q1.get("detail") or "").split("；") if r]
    if not (ev.get("partial_cast") and boxes and reasons and all(r.startswith("画面里多了不该有的人") or r.startswith("本镜可出现")
                                                                   or "无参考可抵" in r or r.startswith("脸高") for r in reasons)):
        return None
    try:
        from PIL import Image
        from . import client
        im = Image.open(image).convert("RGB")
        out_dir = out_dir or (ROOT / "data" / "vqa" / "crops")
        out_dir.mkdir(parents=True, exist_ok=True)
        kinds = []
        for k, (x1, y1, x2, y2) in enumerate(boxes):
            w, h = x2 - x1, y2 - y1
            c = im.crop((int(max(0, x1 - 0.3 * w)), int(max(0, y1 - 0.3 * h)), int(min(im.width, x2 + 0.3 * w)), int(min(im.height, y2 + 0.3 * h))))
            f = out_dir / f"{Path(str(image)).stem}_stranger{k}.jpg"
            c.save(f, quality=92)
            res = client.generate({"system": "你是画面质检员，只看图、只报事实，只输出 JSON。", "text": BACK_Q,
                                   "images": [{"path": str(f), "label": "【裁切】", "max_pixels": 200000}]}, wait_s=900)
            d = rubric_loads(res.get("text", "")) if "error" not in res else None
            kinds.append((d or {}).get("kind") or "拿不准")
    except Exception as e:  # noqa: BLE001
        q1.setdefault("evidence", {})["back_of_head_check"] = f"复核失败：{e}"[:200]
        return None
    q1["evidence"] = {**ev, "back_of_head_check": kinds}
    if all(k in ("后脑或耳朵", "不是人") for k in kinds):
        note = f"Q1 复核：认不出的 {len(kinds)} 张“脸”经本地看图确认是后脑 / 耳朵（{'、'.join(kinds)}），不算多出的人"
        q1.update(status="pass", detail=note + "；原判：" + str(q1.get("detail") or ""))
        pf["blocking"] = [b for b in pf.get("blocking") or [] if b != "Q1"]
        pf["reasons"] = [r for r in pf.get("reasons") or [] if not str(r).startswith("Q1")]
        if not pf["blocking"]:
            pf["ok"] = True
        return note
    return None


def rubric_loads(text: str) -> dict | None:
    from . import rubric
    return rubric._loads_loose(text or "")


BAND_Q = ("参考图是这个角色项目方认定的当前身份脸；【待检】是新出图里裁出来的同一角色的脸。只看脸，回答三项，输出 JSON："
          '{"same_person": "是|否|拿不准", "style_ok": "是|否|拿不准", "sharp": "是|否|拿不准", "e": "依据，不超过 30 字"}。'
          "same_person：五官、脸型、年龄感是不是同一个人（侧脸 / 转角不算不同）；style_ok：画风和质感和参考一致（不是另一种脸型画法、没有发平发糊的塑料感）；"
          "sharp：脸部清晰，不糊。")


BAND_SHARP_MIN = 42.3      # 总调度 11-02 04:0x 批准 band_recalib 提议（40 → 42.3，随 sv-05）     # 复核带确定性清晰度：脸部裁切缩到 240×280、取中间 160×180 的 Laplacian 方差（GATE_CALIBRATION §16）


def face_sharpness(crop_path) -> float:
    """脸部清晰度（确定性，和看图模型并用）：03-S2 v14（总调度判“发糊发平”）= 23.9，其余 19 张同一人 60.6–522"""
    import cv2
    g = cv2.cvtColor(cv2.resize(cv2.imread(str(crop_path)), (240, 280)), cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(g[60:240, 40:200], cv2.CV_64F).var())


BAND_CALIB = ROOT / "data" / "vqa" / "gate_calib" / "band_sharp_auto.jsonl"


def band_calib_log(row: dict) -> None:
    BAND_CALIB.parent.mkdir(parents=True, exist_ok=True)
    with BAND_CALIB.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + chr(10))


def band_ref(cid: str) -> str | None:
    """复核带并排复核的参照：每个角色都用当前身份脸 asset_refs.face_of（K-OPS-E29，文案不写死决定编号）。
    读不到运行登记表 / 资产 → 抛异常，由调用方记“复核失败”（不失败开放，arch_scan silent_read）"""
    # K-OPS-E29（总调度 11-02 16:3x）：身份脸唯一入口 asset_refs.face_of，林晚也不例外（原来写死 L-086，L-122 换脸后复核带还拿旧脸比）
    from ns_agent import asset_refs as AR
    reg = json.loads((ROOT / "config" / "pipeline" / "runs.json").read_text(encoding="utf-8"))   # 项目 id 只从运行登记表读（hard_run）
    f = AR.face_of(reg.get("primary"), cid)
    return str(ROOT / f) if f else None


def q1_band_review(pf: dict, image) -> str | None:
    """K-REV-V18 修订：Q1 落在复核带（证据 review_band）→ 每个角色的脸裁出来和参照左右并排交本地看图（K-REV-V01）：
    同一人 / 画风质感 / 清晰度三项都是“是”才算这个角色过；全部过、且 Q1 没有别的不过原因 → Q1 改判 pass。→ 改判说明或 None"""
    q1 = next((i for i in pf.get("items") or [] if i.get("id") == "Q1"), None)
    if not q1 or q1.get("status") != "fail":
        return None
    ev = q1.get("evidence") or {}
    band = ev.get("review_band") or []
    reasons = [r for r in str(q1.get("detail") or "").split("；") if r]
    if not band or any("复核带" not in r for r in reasons):
        return None
    try:
        from PIL import Image
        from . import client
        im = Image.open(image).convert("RGB")
        out_dir = ROOT / "data" / "vqa" / "crops"
        out_dir.mkdir(parents=True, exist_ok=True)
        results = []
        for k, b in enumerate(band):
            ref = band_ref(b["cid"])
            if not ref or not b.get("bbox"):
                results.append({"cid": b["cid"], "ok": False, "why": "没有参照或脸框"})
                continue
            x1, y1, x2, y2 = b["bbox"]
            w, h = x2 - x1, y2 - y1
            fp = out_dir / f"{Path(str(image)).stem}_band{k}.jpg"
            im.crop((int(max(0, x1 - 0.3 * w)), int(max(0, y1 - 0.3 * h)), int(min(im.width, x2 + 0.3 * w)),
                     int(min(im.height, y2 + 0.3 * h)))).save(fp, quality=92)
            if b.get("mode") == "sharp_only":     # L-098：身份库 top-k 已过线、只差锚 → 只做确定性清晰度（decider=code，不调看图）
                sharp_v = face_sharpness(fp)
                results.append({"cid": b["cid"], "sim": b.get("sim"), "topk": b.get("topk"), "ok": sharp_v >= BAND_SHARP_MIN,
                                "sharpness": round(sharp_v, 1), "mode": "sharp_only"})
                if sharp_v < BAND_SHARP_MIN:
                    band_calib_log({"image": str(image), "crop": str(fp), "cid": b["cid"], "sim": b.get("sim"), "topk": b.get("topk"),
                                    "sharpness": round(sharp_v, 1), "mode": "sharp_only", "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
                continue
            res = client.generate({"system": "你是画面质检员，只看图、只报事实，只输出 JSON。", "text": BAND_Q,
                                   "images": [{"path": ref, "label": "参考图：当前身份脸", "max_pixels": 150000},
                                              {"path": str(fp), "label": "【待检】", "max_pixels": 200000}]}, wait_s=900)
            d = rubric_loads(res.get("text", "")) if "error" not in res else None
            d = d or {}
            sharp_v = face_sharpness(fp)
            # 本地看图三项（同一人 / 画风 / 清晰）+ 确定性清晰度都过才放行：标定里看图模型对 20 张全答“是”（含 03-S2 v14 反例），
            # 单靠它放不住发糊的脸，所以清晰度另用 Laplacian 方差把关
            ok = all(d.get(x) == "是" for x in ("same_person", "style_ok", "sharp")) and sharp_v >= BAND_SHARP_MIN
            results.append({"cid": b["cid"], "sim": b.get("sim"), "ok": ok, "sharpness": round(sharp_v, 1),
                            **{x: d.get(x) for x in ("same_person", "style_ok", "sharp", "e")}})
            if sharp_v < BAND_SHARP_MIN:      # 总调度 11-02：被清晰度拦下的自动进标定集，阈值每周按标定集重算（tools/vqa/band_recalib.py）
                band_calib_log({"image": str(image), "crop": str(fp), "cid": b["cid"], "sim": b.get("sim"), "sharpness": round(sharp_v, 1),
                                "vlm": {x: d.get(x) for x in ("same_person", "style_ok", "sharp")}, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    except Exception as e:  # noqa: BLE001
        q1.setdefault("evidence", {})["band_review"] = f"复核失败：{e}"[:200]
        return None
    q1["evidence"] = {**ev, "band_review": results}
    if results and all(r["ok"] for r in results):
        note = "Q1 复核带复核通过（" + "、".join(f"{r['cid']} {r['sim']}" + (f" top-k {r['topk']} 清晰度 {r['sharpness']}" if r.get("mode") == "sharp_only"
                                                   else "：同一人 / 画风 / 清晰都是") for r in results) + "）"
        q1.update(status="pass", detail=note + "；原判：" + str(q1.get("detail") or ""))
        pf["blocking"] = [x for x in pf.get("blocking") or [] if x != "Q1"]
        pf["reasons"] = [r for r in pf.get("reasons") or [] if not str(r).startswith("Q1")]
        if not pf["blocking"]:
            pf["ok"] = True
        return note
    return None


def q1a_vlm(pf: dict, image) -> str | None:
    """Q1a 的看图部分（decider=local_model，K-REV-V01）：确定性已过的，逐人裁头肩问性别 / 发色 / 发长，与人物卡不符 → Q1a 改 fail。
    看图没跑成 → 证据记 vlm=untested，不改判（确定性分割已判过；零容忍靠分割兜底）。→ 改判说明或 None"""
    it = next((i for i in pf.get("items") or [] if i.get("id") == "Q1a"), None)
    if not it:
        return None
    ev = it.get("evidence") or {}
    seg_off = it.get("status") == "untested" and ev.get("q1a_mode") == "unavailable"
    if it.get("status") != "pass" and not seg_off:
        return None
    if skip_nonblocking("Q1a"):              # L-205 P2：Q1a 非硬项 → 不跑看图逐人问答（确定性分割结论照留、只记录）
        it["evidence"] = {**ev, "vlm": SKIPPED}
        return None
    from ns_agent.gen_contract import cfg as GC
    from . import hair_attr as HA
    # ISS-1103-024206-c8c0：用 postflight 判 Q1a 时那份按剧人物卡（evidence.cards）；旧记录没有时退回 config cards
    cards = {**(((GC().get("image_thresholds") or {}).get("Q1a_attr") or {}).get("cards") or {}), **(ev.get("cards") or {})}
    bad, asked = [], {}
    for cid, box in (ev.get("boxes") or {}).items():
        try:
            ans = HA.vlm_ask(image, box)
        except Exception as e:  # noqa: BLE001 — 看图失败只记录，不改判
            ans = None
            asked[cid] = {"error": str(e)[:120]}
        if ans is None:
            asked.setdefault(cid, {"vlm": "untested"})
            continue
        asked[cid] = ans
        bad += HA.vlm_judge(cid, ans, cards.get(cid) or {})
    it["evidence"] = {**ev, "vlm": asked}
    if not bad and seg_off:
        # 分割不可用（服务器没装 mediapipe）：看图逐人全部答了且相符才放行（fail-closed），记 vlm_only 并报警，不静默
        if not (ev.get("boxes") or {}) or any("vlm" in x or "error" in x for x in asked.values()):
            return None
        it.update(status="pass", detail="Q1a 分割不可用，看图逐人核对相符（vlm_only）")
        it["evidence"]["q1a_mode"] = "vlm_only"
        _q1a_alert(str(image), ev.get("why") or "")
        pf["blocking"] = [x for x in pf.get("blocking") or [] if x != "Q1a"]
        pf["reasons"] = [r for r in pf.get("reasons") or [] if not str(r).startswith("Q1a")]
        if not pf["blocking"]:
            pf["ok"] = True
        return "Q1a vlm_only 放行"
    if not bad:
        return None
    note = "Q1a 看图逐人核对不符：" + "；".join(bad)
    it.update(status="fail", detail=note)
    pf["ok"] = False
    pf["blocking"] = sorted(set(pf.get("blocking") or []) | {"Q1a"})
    pf.setdefault("reasons", []).append("Q1a " + note)
    return note


def hair_primary(pf: dict, v: dict | None) -> bool:
    """总调度 11-02（发色判定余量薄：0.5 / 0.6 MP 之间 VLM hair_mismatch 来回翻）：Q1a 确定性分割主判——
    Q1a 用分割测过且 pass（q1a_mode=seg）→ VLM 的 hair_mismatch 降为 unsure（只作旁证）；Q1a fail 照拦；Q1a 没测 / vlm_only → VLM 照判。
    开关 gate.hair_primary（A/B 见 GATE_CALIBRATION §19）。→ 是否降级"""
    if not v or not gate_cfg().get("hair_primary") or "hair_mismatch" not in (v.get("defects") or []):
        return False
    it = next((i for i in pf.get("items") or [] if i.get("id") == "Q1a"), None)
    if not it or it.get("status") != "pass" or (it.get("evidence") or {}).get("q1a_mode") != "seg":
        return False
    v["defects"] = [d for d in v["defects"] if d != "hair_mismatch"]
    v["unsure"] = sorted(set(v.get("unsure") or []) | {"hair_mismatch"})
    c = (v.get("checks") or {}).get("hair_mismatch") or {}
    v.setdefault("checks", {})["hair_mismatch"] = {"s": "unsure", "e": "Q1a 分割主判已过；看图原判 defect：" + str(c.get("e") or "")[:80]}
    return True


def _q1a_alert(image: str, why: str) -> None:
    """Q1a 降级（分割不可用）报警：写 data/vqa/alerts.jsonl（SENTINEL 探针读）"""
    p = ROOT / "data" / "vqa" / "alerts.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": "q1a_vlm_only", "image": image,
                            "why": why[:200]}, ensure_ascii=False) + chr(10))


def _emb_of(file) -> dict:
    import hashlib
    p = Path(str(file))
    if not p.exists():
        return {}
    e = ROOT / "data" / "identity" / "emb" / (hashlib.sha256(p.read_bytes()).hexdigest()[:16] + ".json")
    if not e.exists():
        return {}
    return json.loads(e.read_text(encoding="utf-8"))


def same_scene_peers(sid: str) -> list[str]:
    """同一场（shot_rec.scene_id）的其他镜头"""
    from . import continuity as CT
    sc = (CT.shot_rec(sid) or {}).get("scene_id")
    if not sc:
        return []
    out = []
    for tj in sorted(CT.renders_dir().glob("ep*/S*/takes.json")):          # 按当前剧（I-14）
        o = f"{tj.parent.parent.name}-{tj.parent.name}"
        if o != sid and (CT.shot_rec(o) or {}).get("scene_id") == sc:
            out.append(o)
    return out


def q1x(sid: str, stage: str, image, pf: dict) -> dict | None:
    """Q1x 同场跨镜比对（总调度批准设计，GATE_CALIBRATION §20）：本图每个在画角色那张脸，对同场其他镜头当前首帧里同一角色的脸，
    取最像的 3 张余弦平均 → ≥ pass 线过；复核下限–pass 线记复核带（先只记录，不拦）；< 复核下限不过（“和同场 Sx 不像同一人”）；
    同场没有可比的当前首帧 = na。小脸（脸高 < 0.10）两条线都下移 0.10。阈值 config/gen_contract.json Q1x_scene。→ 加进 pf 的项"""
    if stage != "image":
        return None
    from ns_agent.gen_contract import cfg as GC
    from . import continuity as CT
    import numpy as np
    t = (GC().get("image_thresholds") or {}).get("Q1x_scene") or {}
    if not t.get("enabled"):
        return None
    if skip_nonblocking("Q1x"):              # L-205 P2：Q1x 非硬项 → 不跑，记 na（只记录）
        item = {"id": "Q1x", "title": "同场跨镜身份一致（对同场其他镜头当前首帧）", "status": "na", "record_only": True,
                "detail": SKIPPED + "（L-196 非硬项，L-205 不跑）", "evidence": {"skipped": SKIPPED}}
        pf["items"] = [i for i in pf.get("items") or [] if i.get("id") != "Q1x"] + [item]
        return item
    mine = _emb_of(image)
    peers = {}
    for o in same_scene_peers(sid):
        f = CT.current_first(o)
        if f:
            e = _emb_of(f)
            if e:
                peers[o] = e
    item = {"id": "Q1x", "title": "同场跨镜身份一致（对同场其他镜头当前首帧）", "status": "na", "detail": "", "evidence": {}}
    if not mine or not peers:
        item["detail"] = "同场没有可比的当前首帧" if mine else "本图没有角色脸特征"
    else:
        bad, band, got = [], [], {}
        for cid, m in mine.items():
            v = np.asarray(m["emb"], np.float32)
            v /= np.linalg.norm(v) + 1e-9
            sims = []
            for o, e in peers.items():
                if cid in e:
                    w = np.asarray(e[cid]["emb"], np.float32)
                    sims.append((float(v @ (w / (np.linalg.norm(w) + 1e-9))), o))
            if not sims:
                continue
            sims.sort(reverse=True)
            top = sims[:int(t.get("k", 3))]
            score = sum(x for x, _ in top) / len(top)
            small = (m.get("h") or 0) < float(t.get("small_face", 0.10))
            need, lo = float(t.get("pass", 0.60)) - (0.10 if small else 0), float(t.get("review_min", 0.50)) - (0.10 if small else 0)
            got[cid] = {"score": round(score, 3), "vs": [o for _, o in top], "need": need, "lo": lo}
            if score < lo:
                bad.append(f"{cid} 和同场 {'、'.join(o for _, o in top)} 不像同一人（{score:.3f} < {lo}）")
            elif score < need:
                band.append(f"{cid} {score:.3f} 在复核带 {lo}–{need}")
        if got:
            item.update(status="fail" if bad else "pass", detail="；".join(bad + band) or "同场一致（" + "、".join(f"{c} {g['score']}" for c, g in got.items()) + "）",
                        evidence={"per_person": got, "band": band})
        else:
            item["detail"] = "同场其他镜头里没有同一角色的脸"
    pf["items"] = [i for i in pf.get("items") or [] if i.get("id") != "Q1x"] + [item]
    if item["status"] == "fail":
        pf["ok"] = False
        pf["blocking"] = sorted(set(pf.get("blocking") or []) | {"Q1x"})
        pf.setdefault("reasons", []).append("Q1x " + item["detail"])
    return item


def accept_gate(sid: str, stage: str, image, pf: dict) -> dict | None:
    """出图后验收闸门（ALIGNMENT 第 8 步，总调度 11-02 17:0x：不再只记录）：tools/vqa/accept.py 四项标签（身份 / 服装 / 提示词 / 人数）
    → postflight Q10 项，四项全过才算过。开关 config image_thresholds.Q10_accept.enabled；人数检测 host 默认本机（OFFLINE_SELF）"""
    from ns_agent.gen_contract import cfg as GC
    t = (GC().get("image_thresholds") or {}).get("Q10_accept") or {}
    if not t.get("enabled"):
        return None
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from tools.vqa import accept as ACC
    from tools.vqa import anomaly as AN
    import os
    try:                                  # L-206：审图位主机本机常驻检测活着 → 人数检测留在审图位（CPU / 审图卡），不发到出图位的卡
        from ns_agent.gen_contract import accept as GA
        local = GA.review_local_anomaly(ROOT)
    except Exception:  # noqa: BLE001  # arch-ok: 探测出错 = 原口径 count_host
        local = None
    host = local or t.get("count_host") or os.environ.get("OFFLINE_SELF") or ""
    # 总调度 11-02 17:4x：服务器上没有检测卡的主机（gpu33：GPU2 是坏卡，硬规则永远不用）内联不跑人数检测，记 deferred；
    # 人数检测统一由笔记本发起的批量验收在 gpu36（有镜面分割）上做，视频闸门读 data/vqa/accept/epNN.json
    try:
        from tools.factory import hosts as _H
        _h = (_H.load().get("hosts") or {}).get(host) or {}
    except Exception:  # noqa: BLE001 — 车道表读不到：按“没有检测卡”处理（只会推迟，不会用错卡）
        _h = {}
    # 配了常驻检测服务（count_host = http 地址，gpu36 内网入口）就不推迟：服务器上也能直接测人数
    defer = bool(host) and not host.startswith(("http://", "https://")) and _h.get("detector_gpu", AN.DETECTOR_GPU.get(host)) is None
    try:
        lab = ACC.label(sid, stage, image, pf, host=host or None, defer_count=defer,
                        skip_outfit=skip_nonblocking("Q10"))     # L-205 P2：服装（谁穿什么看图）非硬项不跑；身份 / 提示词人数 / 人数照跑
    except Exception as e:  # noqa: BLE001 — 验收本身出错 = untested（闸门挡、记 score_infra），不当通过
        lab = {k: {"status": "untested", "why": f"验收出错：{type(e).__name__}: {e}"[:200]} for k in ACC.ORDER}
        lab["ok"] = "untested"
    return ACC.apply(pf, lab)


def q1d(sid: str, stage: str, image, pf: dict) -> dict | None:
    """Q1d 本地图片异常检测（CONSISTENCY_PLAN §4，DETECT-S tools/vqa/anomaly.py；人数 / 头数 / 镜面 / 手脚 / 标志饰品归属）。
    总调度 11-02：先记日志、不拦——契约状态永远 na（不进 blocking），真实判定放 evidence.verdict（pass / fail / untested）和 reasons，
    等 DETECT-S 出标注表、总调度标定后再改成阻断。默认关（config image_thresholds.Q1d_anomaly.enabled）：
    anomaly.check 走 rssh 连 gpu36，gpu33 上的 producer 没有到 gpu36 的密钥，要先给检测器一个服务器上能直达的入口再开。
    带超时（timeout_s）：检测慢或远端挂了只记 untested，不拖慢出图线。→ 加进 pf 的项"""
    from ns_agent.gen_contract import cfg as GC
    t = (GC().get("image_thresholds") or {}).get("Q1d_anomaly") or {}
    if not t.get("enabled"):
        return None
    import concurrent.futures as cf
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    ev: dict = {"record_only": True}
    try:
        from tools.vqa import anomaly as AN
        cast = AN.shot_cast(sid)             # 带 faced 字段的 cast（编译稿口径），不是契约 req 里的 cast
        ex = cf.ThreadPoolExecutor(max_workers=1)
        try:
            res = ex.submit(AN.check, image, cast, t.get("host", "gpu36")).result(timeout=float(t.get("timeout_s", 120)))
        finally:
            ex.shutdown(wait=False)
        ev.update(verdict=res.get("status"), mode=res.get("mode"), reasons=res.get("reasons") or [],
                  heads=res.get("heads"), hands=res.get("hands"), feet=res.get("feet"))
    except Exception as e:  # noqa: BLE001 — 检测器出错只记日志，不影响出图；原因写进证据
        ev.update(verdict="untested", mode="error", reasons=[f"{type(e).__name__}: {e}"[:200]])
    detail = "记日志、不拦：" + ("；".join(ev.get("reasons") or []) or "未发现异常") if ev["verdict"] != "untested"         else "记日志、不拦：检测没跑成（" + "；".join(ev.get("reasons") or []) + "）"
    item = {"id": "Q1d", "title": "本地图片异常检测（人数 / 头数 / 镜面 / 手脚 / 饰品归属；记日志、不拦）", "status": "na", "detail": detail, "evidence": ev}
    pf["items"] = [i for i in pf.get("items") or [] if i.get("id") != "Q1d"] + [item]
    return item


def q1_needs_vlm(pf: dict) -> bool:
    """Q1 不过时，看图结果还能不能把它救回来（决定要不要排队看图，K-OPS-V15）：
    单脸、分数落在转角放宽最低档以上（q1_yaw_relax 的候选），或者只因“认不出的脸”且 cast 有背影 / 局部人物（recheck_strangers 的候选）"""
    q1 = next((i for i in pf.get("items") or [] if i.get("id") == "Q1"), None)
    if not q1 or q1.get("status") != "fail":
        return False
    ev = q1.get("evidence") or {}
    detail = str(q1.get("detail") or "")
    if ev.get("partial_cast") and ev.get("stranger_boxes") and detail.startswith("画面里多了不该有的人"):
        return True
    if ev.get("review_band") and all("复核带" in r for r in detail.split("；") if r):
        return True
    sims = ev.get("sims") or {}
    try:
        from ns_agent.gen_contract import cfg as gcfg
        yr = ((gcfg().get("image_thresholds") or {}).get("Q1_identity") or {}).get("yaw_relax") or {}
    except Exception:  # noqa: BLE001
        return False
    floor = min((t for _lim, t in yr.get("tiers") or []), default=None)
    if len(sims) == 1 and "；" not in detail and not ev.get("strangers") and floor is not None:
        cid, sim = next(iter(sims.items()))
        from ns_agent.gen_contract.cards import is_lead_listed
        return is_lead_listed(cid, yr.get("characters") or []) and sim is not None and sim >= floor
    return False


def q9_passed(pf: dict | None) -> bool:
    """build_jobs.passed_frames 用：视频只派“图片闸门通过”的首帧（gate.video_requires_q9）；没有看图结果 = 未通过"""
    if not (enforced() and gate_cfg().get("video_requires_q9", True)):
        return True
    it = next((i for i in (pf or {}).get("items") or [] if i.get("id") == "Q9"), None)
    return bool(it) and it.get("status") in ("pass", "na")


def ref_files(asset_refs: dict) -> list[str]:
    """asset_refs 记录（ns_agent.asset_refs.record）→ 参考图绝对路径（槽位顺序，去重）"""
    out = []
    for sl in (asset_refs or {}).get("slots") or []:
        f = sl.get("file") if isinstance(sl, dict) else None
        if not f:
            continue
        for c in (Path(f), ROOT / f, ROOT / "data" / f):
            if c.is_absolute() and c.exists():
                if str(c) not in out:
                    out.append(str(c))
                break
    return out
