"""共用验收（PRODMERGE-O 11-03，PLAN_V2_MASTER v4 1-5，L-151）：golden_v2 的出图后判定并进契约 postflight，producer 出图 /
定稿档（final_tier.render_final）/ 以后任何出图入口都用这一份，不再各写一套。

一张已出的图（postflight 已打完分）→ evaluate()：
  ① Q9 本地看图复核：远端打分没带（untested）→ ensure_q9 在笔记本补跑一次（图只进自有机器，K-REV-V01）；排队超时再要一次
  ② anomaly 人数 / 头数 / 手数（tools/vqa/anomaly.check）
  ③ 合并看图 bundle（tools/pipeline/asset_checks.bundle_check）：本镜核对项（check_items：人数 + 人物 + 造型单品 + 场景 + 景别 + 分镜描述，
     全部从本镜编译结果动态生成，不写死服装 / 场景）+ 年龄观感 + OCR，一次问完
  ④ judge_bundle → verdict 三态：pass / reject（任何一项判了 fail，逐项写原因）/ retry（没有 fail、只是有项没拿到结论——
     Q9 排队超时、合并看图没答、Q1 没打出分：**不判 fail**，同一张图重新复核，不重出、不换种子、不算一次尝试）
apply() 把结论写回 postflight：加一项 QB；reject → ok=False、blocking 加 QB；retry → accept_retry=True + score_infra=True
（producer 已有的“只重打分、不计次”通道）。

开关：config/pipeline/producer.json accept.enabled；只对资产包模式的剧（runs.json 有 assets_manifest、不是 legacy）生效——
V1 的 Q 口径不变（REVIEW_GLOBAL_OPUS F.1）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
CFG = "config/pipeline/producer.json"
DEFAULT = {"enabled": True, "scope": "asset_bundle_runs", "max_rechecks": 2}
_MODS: dict = {}


def _load(name: str, rel: str, root: Path = ROOT):
    key = (name, str(root))
    if key not in _MODS:
        src = root / rel if (root / rel).is_file() else ROOT / rel      # 临时根（测试 / 演练）没有工具文件 → 用本仓库的
        if str(src.parent) not in sys.path:
            sys.path.insert(0, str(src.parent))
        sp = importlib.util.spec_from_file_location(f"{name}_{abs(hash(str(src)))}", src)
        m = importlib.util.module_from_spec(sp)
        sp.loader.exec_module(m)
        _MODS[key] = m
    return _MODS[key]


def config(root: Path = ROOT) -> dict:
    try:
        d = json.loads((root / CFG).read_text(encoding="utf-8")).get("accept") or {}
    except (OSError, ValueError):   # arch-ok: 没有配置 = 默认（开、只对资产包剧）
        d = {}
    return {**DEFAULT, **d}


def enabled(run: str | None, root: Path = ROOT) -> bool:
    """资产包模式的剧才走共用验收（V1 legacy / 没登记清单的剧 = False，口径不变）"""
    if not run or not config(root).get("enabled"):
        return False
    try:
        R = _load("runs_for_accept", "tools/autopilot/runs.py", root)
        return not R.is_legacy(run, root) and R.asset_manifest(run, root) is not None
    except Exception:  # noqa: BLE001  # arch-ok: 登记表读不到 = 不加判（postflight 原口径照旧，失败不放宽）
        return False


# ------------------------------------------------------------------ Q9
def q9_timeout(post: dict) -> bool:
    """Q9 没拿到看图结论（排队超时 / 没跑：evidence 里没有 model）= 不是图的问题 → retry，不判 fail（asset_worker.q9_queue_timeout 同口径）"""
    q9 = next((i for i in (post or {}).get("items") or [] if i.get("id") == "Q9"), None)
    return bool(q9 is None or (q9.get("status") in ("untested", None) and not (q9.get("evidence") or {}).get("model")))


def ensure_q9(post: dict, img: str, job: dict) -> dict:
    """远端打分不带本地看图复核（Q9 untested）时，在笔记本补跑一次（图只进本地 Qwen3-VL，K-REV-V01），替换 Q9 项（原 golden_v2.ensure_q9）"""
    from . import postflight as PO
    items = (post or {}).get("items") or []
    q9 = next((i for i in items if i.get("id") == "Q9"), None)
    if q9 and q9.get("status") not in ("untested", None):
        return post
    VQ = PO._vqa()
    if VQ is None:
        return post
    req = {k: job.get(k) for k in ("cast", "asset_refs", "prompt", "refs")}
    v = VQ.cached_for(img) or PO._q9_live(VQ, img, req)
    if v is None:                                   # _q9_live 只认 data/renders/epNN/SN 下的图；别处的图直接调本地看图
        from ..vqa import client as VC
        v = VC.inspect_image(img, job.get("prompt") or "", list(job.get("refs") or []), wait_s=900)
    if v and not v.get("error"):
        VQ.sanitize(v)
        VQ.reask_eye(v, img)                        # 11-04：看镜头判词自相矛盾 → 只问视线重问一次（尾帧也走这里）
    st, d, ev = PO._q9_item(VQ, VQ.q9(v))
    new = {"id": "Q9", "title": (q9 or {}).get("title") or "本地看图复核", "status": st, "detail": d, "evidence": ev}
    post = dict(post or {})
    post["items"] = [i for i in items if i.get("id") != "Q9"] + [new]
    return post


# ------------------------------------------------------------------ 本镜核对项（原 golden_v2.check_items）
def _g(o, k, d=None):
    return o.get(k, d) if isinstance(o, dict) else getattr(o, k, d)


DESC_PREFIX = "画面内容与下面这段分镜描述一致（姿势、视线、手里的物件）："


def q10_count(post: dict | None) -> dict | None:
    """L-183 c（W43 第 6 轮：last:S6 t9 同一张图 Q10 count pass、QB count fail）：人数统一以 Q10 出图后验收的 count 为准
    （tools/vqa/accept.count_check：按剧本 cast 的在画 / 方位，经常驻检测服务），QB 不再用通用 cast 另跑一遍 anomaly。
    Q10 有 pass / fail 结论 → 返回 anomaly 形状的结果；Q10 没跑 / count 未测 / 推迟 → None（调用方照旧自己测）"""
    q10 = next((i for i in (post or {}).get("items") or [] if i.get("id") == "Q10"), None)
    c = ((q10 or {}).get("evidence") or {}).get("count") or {}
    if c.get("status") in ("pass", "fail"):
        return {"status": c["status"], "source": "Q10 count", "reasons": [c.get("why")] if c.get("why") else [], "mode": "q10"}
    return None


# L-184 行标签：check_rows 每条核对项带一个标签（来自编译结果的字段，不从文字猜）
#   count 人数 / outfit 主服装（含非点选的鞋、身份记号）/ owner_pick 项目方点选单品（owner_inputs：O1–O4、SH1/SH2、J1/J2）/
#   accessory 非点选配饰（slot=accessory / nails）/ scene 场景 id / props 本镜场景陈设子句（scene_detail，compiler.scene_segment
#   的“本镜场景”段）/ size 景别 / desc 分镜描述 / text 文字 / hands 手指
RECORD_ONLY_TAGS = ("desc", "props", "accessory")          # 首尾帧都只记录
RECORD_ONLY_LAST_TAGS = ("owner_pick",)                    # 只在尾帧只记录（首帧照样阻断）


def record_only_rows(tags: list[str] | None, items: list[str], stage: str | None = None) -> list[int]:
    """哪几条（1 起）只记录。没有标签（旧调用）= 只认“与分镜描述一致”那条（L-183 原口径）"""
    if tags is None or len(tags) != len(items):
        return [i + 1 for i, t in enumerate(items) if str(t).startswith(DESC_PREFIX)]
    ro = RECORD_ONLY_TAGS + (RECORD_ONLY_LAST_TAGS if stage == "last" else ())
    return [i + 1 for i, t in enumerate(tags) if t in ro]


def description_record_only(bundle: dict, items: list[str], tags: list[str] | None = None, stage: str | None = None) -> dict:
    """L-183（总调度 11-04 04:2x）+ L-184（05:0x）：共用验收逐条里这些行只记录、不阻断——
    ① “与分镜描述一致”（道具 / 方位 / 手势细节）首尾帧；② 场景行的道具子句（本镜场景陈设）首尾帧；
    ③ 造型行里非项目方点选的配饰 / 指甲子句首尾帧；④ 项目方点选的 O1–O4 / SH1–SH2（J1/J2）尾帧（首帧照样阻断）。
    首帧的整体提示词核对仍由 Q10 prompt 阻断；尾帧不是成片输入（L-088 Pika 只给首帧）。
    该行照记（record_only=True，审片可见），不进 status / 阻断原因；身份 / 主服装 / 人数 / 场景 / 景别 / 文字 / 手指
    与年龄、OCR 照样判。回答缺项（untested）不改"""
    idx = record_only_rows(tags, items, stage)
    if not idx or bundle.get("status") == "untested":
        return bundle
    rows = [dict(r, record_only=True) if r.get("n") in idx else r for r in bundle.get("items") or []]
    st = "pass" if all(r.get("ok") or r.get("record_only") for r in rows) else "fail"
    return {**bundle, "items": rows, "status": st, "record_only_rows": idx}


def shot_description(shot, stage: str | None = None) -> str:
    """要核对的分镜描述：首帧 = img_prompt；尾帧（stage=last）= 编译器同一函数 compiler.end_state(motion_prompt) 取的结束状态
    （尾帧提示词“只把画面改为这个镜头动作结束时的瞬间：…”就是它）。W38 11-04：原来尾帧也拿首帧描述核 → ep01 S11 尾帧
    （左手拎相机包、拳头伸进包口）对着“左手撑横杆、从卡槽抽卡”判不符，三次耗尽 best_effort。取不到结束状态 = 退回 img_prompt"""
    img = _g(shot, "img_prompt") or ""
    if stage != "last":
        return img
    try:
        from .. import compiler as C
        end = C.end_state(_g(shot, "motion_prompt") or "")
    except Exception:  # noqa: BLE001  # arch-ok: 编译器载不进来 = 退回首帧描述（原口径）
        end = ""
    return f"（镜头结束时的画面）{end}" if end else img


def owner_picks(run: str | None) -> set:
    """项目方点选单品（assets_v2/owner_inputs.json，经清单 owner_inputs 指过去）→ {(角色, 造型 id / 单品 id)}：
    outfit 条目给 outfit_id（O1–O4），item 条目给 item_id（SH1/SH2/J1/J2）。读不到 = 空集（= 没有点选，按普通造型判）"""
    if not run:
        return set()
    try:
        from .. import asset_refs as AR
        rel = (AR.manifest(run) or {}).get("owner_inputs")
        d = json.loads((ROOT / rel).read_text(encoding="utf-8")) if rel else {}
    except Exception:  # noqa: BLE001  # arch-ok: 没有点选登记 = 无点选单品（主服装照样阻断，不放宽）
        return set()
    out = set()
    for it in d.get("items") or []:
        cid = it.get("character")
        for k in ("outfit_id", "item_id"):
            if cid and it.get(k):
                out.add((cid, it[k]))
    return out


def check_rows(job: dict, shot, looks: dict | None = None, run: str | None = None,
               picks: set | None = None) -> list[tuple[str, str]]:
    """→ [(核对项, 行标签)]（标签见 RECORD_ONLY_TAGS 上方说明）。
    人数 + 人物 + 造型单品 ← job.asset_refs.characters + 造型表（asset_refs.load_looks）；单品按 LookPart.slot 分行：
    点选（owner_inputs）一行、主服装（outfit / shoes / marker）一行、非点选配饰（accessory / nails）一行；
    场景 ← 本镜 scene_id（场景）+ scene_detail（本镜陈设子句，另起一行）；景别 ← asset_refs.size；分镜描述 ← 本镜 img_prompt。
    造型缺单品登记时如实写，不拿别的顶替（ISS-1103-003851-4541）"""
    ar = job.get("asset_refs") or {}
    on = [c for c in ar.get("characters") or [] if (c.get("presence") or "在画") == "在画"]
    if looks is None:
        from .. import asset_refs as AR
        looks = AR.load_looks(run)
    if picks is None:
        picks = owner_picks(run)
    names = "、".join(c.get("name") or c.get("character") or "?" for c in on) or "无人"
    rows = [(f"画面里恰好 {len(on)} 个人（{names}），每个人都是成年人", "count")]
    zones = set(ar.get("zones") or [])        # 编译器判定的可见部位：鞋 / 配饰 / 美甲不在画就不核，服装本体总是核
    seen = "（只核对画面里看得到的部分）"
    for c in on:
        cid = c.get("character")
        name, lid = c.get("name") or cid, c.get("look")
        lk = (looks.get(cid) or {}).get(lid) if lid else None
        parts = [p for p in (lk.parts if lk else []) if not getattr(p, "extra", False)
                 and (not zones or getattr(p, "slot", "outfit") == "outfit" or getattr(p, "zone", None) in zones)]
        picked = (cid, lid) in picks
        own = [p for p in parts if getattr(p, "slot", "outfit") in ("outfit", "shoes") and picked
               or (cid, getattr(p, "item_id", None)) in picks]
        acc = [p for p in parts if p not in own and getattr(p, "slot", "outfit") in ("accessory", "nails")]
        main = [p for p in parts if p not in own and p not in acc]
        if not parts:
            rows.append((f"{name}的造型 {lid or '未定'} 在造型表里没有单品登记：只核对穿着与分镜描述一致", "outfit"))
            continue
        if own:
            rows.append((f"{name}的穿着与项目方指定造型 {lid} 一致{seen}：{'；'.join(p.label for p in own)}", "owner_pick"))
        if main or not own:
            zh = str(getattr(lk, "zh", "") or "")
            body = "；".join(p.label for p in main) or (zh.split("=", 1)[1] if zh.startswith("服装=") else "")
            if body:
                rows.append((f"{name}的穿着与造型 {lid} 一致{seen}：{body}", "outfit"))
        if acc:
            rows.append((f"{name}的配饰（造型 {lid}）：{'；'.join(p.label for p in acc)}", "accessory"))
    sid, sdet = _g(shot, "scene_id"), _g(shot, "scene_detail")
    rows.append((f"场景是 {sid or '未登记场景'}", "scene"))
    if sdet:
        rows.append((f"本镜场景陈设（道具 / 方位）：{sdet}", "props"))
    if ar.get("size"):
        rows.append((f"景别是{ar['size']}", "size"))
    rows.append((DESC_PREFIX + shot_description(shot, job.get("stage")), "desc"))
    rows += [("画面里没有文字、水印、logo", "text"), ("手指数量正常、没有多出来的手", "hands")]
    return rows


def check_items(job: dict, shot, looks: dict | None = None, run: str | None = None, picks: set | None = None) -> list[str]:
    """核对项文字（check_rows 去掉标签）"""
    return [t for t, _ in check_rows(job, shot, looks, run, picks)]


# ------------------------------------------------------------------ 判定（原 golden_v2.judge_bundle / verdict）
def judge_bundle(post: dict, an: dict, bundle: dict) -> tuple[dict, dict]:
    """identity = Q1；outfit = Q2（判了就用它；Q2=na 用合并看图第 2 条 = 第一个在画人物的造型）；prompt = 合并看图逐条核对；
    count = anomaly；vlm_review = Q9（na = 只记录不阻断，算过）；age / ocr = 合并看图的年龄观感 / 读字"""
    items = {i.get("id"): i for i in (post or {}).get("items") or []}
    q2 = (items.get("Q2") or {}).get("status", "untested")
    rows = bundle.get("items") or []
    b2 = ("pass" if rows[1].get("ok") or rows[1].get("record_only") else "fail") \
        if len(rows) >= 2 and bundle.get("status") != "untested" else "untested"
    q9 = str((items.get("Q9") or {}).get("status"))
    lab = {"identity": (items.get("Q1") or {}).get("status", "untested"),
           "outfit": q2 if q2 in ("pass", "fail") else b2,
           "prompt": bundle.get("status") or "untested",
           "count": {"pass": "pass", "fail": "fail", "na": "na"}.get(str(an.get("status")), "untested"),
           "vlm_review": {"pass": "pass", "na": "pass", "fail": "fail"}.get(q9, "untested"),
           "age": (bundle.get("age") or {}).get("status") or "untested",
           "ocr": (bundle.get("ocr") or {}).get("status") or "untested"}
    src = {"outfit": "Q2" if q2 in ("pass", "fail") else "合并看图第 2 条（Q2=na：清单造型没有设定图，单品行不可比）"}
    return lab, src


def verdict(post: dict, lab: dict, bundle: dict) -> tuple[str, list[str]]:
    """→ (pass | reject | retry, 原因)。retry = 没有一项 fail、只是有项没拿到结论（不判 fail，重新复核同一张图）"""
    reasons = [f"{k}={v}" for k, v in lab.items() if v not in ("pass", "na")]
    by_id = {i.get("id"): i for i in (post or {}).get("items") or []}
    for qid in ("Q1", "Q9"):
        q = by_id.get(qid)
        if q and q.get("status") not in ("pass", "na"):
            reasons.append(f"{qid}：{str(q.get('detail') or q.get('status'))[:100]}")
    for it in bundle.get("items") or []:
        if not it.get("ok") and not it.get("record_only"):
            reasons.append(f"看图第{it.get('n')}条：{str(it.get('text') or '')[:80]}")
    if (bundle.get("age") or {}).get("status") == "fail":
        reasons.append(f"年龄：{str(bundle['age'].get('text'))[:60]}")
    if (bundle.get("ocr") or {}).get("status") == "fail":
        reasons.append(f"读到文字：{str(bundle['ocr'].get('text'))[:60]}")
    if bundle.get("error"):
        reasons.append(f"合并看图出错：{str(bundle['error'])[:80]}")
    if all(v in ("pass", "na") for v in lab.values()):
        return "pass", []
    if not any(v == "fail" for v in lab.values()):
        return "retry", ["没拿到结论（看图 / 打分服务排队或超时），重试复核：" + "、".join(reasons)]
    return "reject", reasons


def anomaly_host(root: Path = ROOT) -> str:
    """人数检测走哪：服务器上（OFFLINE_SELF 有值）两台之间没有 ssh 互信，host="gpu36" 一律 rc=255 → count 永远 untested →
    QB retry → score_infra 循环（W38 11-04 实测 gpu33 ep01）。服务器上改走 Q10 同一个常驻检测服务 HTTP 入口
    （config/gen_contract.json image_thresholds.Q10_accept.count_host，10.0.0.36:8893，只放行 gpu33）；笔记本照旧 "gpu36"（经常驻主连接）"""
    import os
    if os.environ.get("OFFLINE_SELF"):
        local = review_local_anomaly(root)     # L-206：审图位主机本机常驻检测（CPU / 审图卡）活着 → 用它，不发到出图位的卡
        if local:
            return local
        try:
            gc = json.loads((Path(root) / "config/gen_contract.json").read_text(encoding="utf-8"))
            ch = (((gc.get("image_thresholds") or {}).get("Q10_accept") or {}).get("count_host") or "")   # 与 vqa.contract.accept_gate 同一处
        except (OSError, ValueError):   # arch-ok: 配置读不到 = 原口径 gpu36（结果照旧 untested，不放宽）
            ch = ""
        if ch.startswith(("http://", "https://")):
            return ch
    return "gpu36"


REVIEW_ANOMALY_PORT = 8893          # 与 tools/autopilot/resident_sync.ANOMALY_PORT / tools/vqa/anomaly_server.py 默认一致
_REVIEW_ANOMALY = {"at": 0.0, "url": None}
LAPTOP_ANOMALY_TUNNEL_PORT = 18893  # 笔记本隧道 127.0.0.1:18893 → gpu36 127.0.0.1:8893（线3 #9，11-08）


def review_local_anomaly(root: Path = ROOT, probe=None, ttl_s: float = 60.0) -> str | None:
    """L-206：lane_roles 开着且本机（OFFLINE_SELF）= 审图位主机（config/pipeline/lane_roles.json review.lane 的主机）且本机
    127.0.0.1:8893 常驻人数 / 身份检测 /health 通 → 返回该地址（人数检测留在审图位 CPU / 审图卡，永不发到出图位 gpu36:1）；
    否则 None（退回 count_host 原口径，不让检测断掉）。60 s 缓存探测结果"""
    import os
    import time
    import urllib.request
    if not os.environ.get("OFFLINE_SELF"):
        # 线3 / G4 #9（11-08）：笔记本不在机房内网，count_host 10.0.0.36:8893 连不上（WinError 10060 → score_infra，A13 证据）。
        # 本机隧道 127.0.0.1:18893 → gpu36 127.0.0.1:8893（同一常驻检测服务）通 → 用它；不通 → None（原口径不变）
        url = f"http://127.0.0.1:{LAPTOP_ANOMALY_TUNNEL_PORT}"
    else:
        try:
            lr = json.loads((Path(root) / "config/pipeline/lane_roles.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):   # arch-ok: 配置缺 = 旧口径
            return None
        if not isinstance(lr, dict) or not lr.get("enabled"):
            return None
        host = str((lr.get("review") or {}).get("lane") or "").split(":", 1)[0]
        if not host or os.environ.get("OFFLINE_SELF") != host:
            return None
        url = f"http://127.0.0.1:{REVIEW_ANOMALY_PORT}"
    now = time.time()
    if probe is None and now - _REVIEW_ANOMALY["at"] < ttl_s:
        return _REVIEW_ANOMALY["url"]

    def _probe(u: str) -> bool:
        try:
            with urllib.request.urlopen(u + "/health", timeout=3) as r:
                return r.status == 200
        except (OSError, ValueError):   # arch-ok: 不通 = 不用本机（退回原口径）
            return False
    up = bool((probe or _probe)(url))
    if probe is None:
        _REVIEW_ANOMALY.update(at=now, url=url if up else None)
    return url if up else None


def bundle_cast_ok(fn) -> bool:
    """bundle_fn 接不接 cast=（测试里注入的旧签名 lambda 不接）"""
    import inspect
    try:
        ps = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "cast" in ps or any(p.kind is p.VAR_KEYWORD for p in ps.values())


def evaluate(img: str, post: dict, job: dict, shot, run: str, *, q9_fn=None, anomaly_fn=None, bundle_fn=None,
             looks: dict | None = None, root: Path = ROOT) -> dict:
    """一张已打完 postflight 的图过共用验收 → {status, label, label_source, reasons, check_items, anomaly, bundle, post}。
    各外部服务可注入（测试）；默认 = 本地看图 / gpu36 anomaly / 本地合并看图"""
    q9_fn = q9_fn or ensure_q9
    post = q9_fn(post, img, job)
    if q9_timeout(post):
        post = q9_fn(post, img, job)
    on = [c for c in (job.get("asset_refs") or {}).get("characters") or [] if (c.get("presence") or "在画") == "在画"]
    q10c = q10_count(post)
    if on and q10c:
        an = q10c                                    # L-183 c：人数只认一个来源（Q10 出图后验收的 count），不再各跑一遍
    elif on:
        if anomaly_fn is None:
            anomaly_fn = _load("anomaly_for_accept", "tools/vqa/anomaly.py", root).check
        an = anomaly_fn(img, [{"name": c.get("name"), "presence": "在画", "faced": True, "x": c.get("x") or "中"}
                              for c in job.get("cast") or [] if (c.get("presence") or "在画") == "在画"], host=anomaly_host(root))
    else:
        an = {"status": "na", "note": "空镜 / 物件：不判人数"}
    crow = check_rows(job, shot, looks, run)
    items, tags = [t for t, _ in crow], [g for _, g in crow]
    if bundle_fn is None:
        bundle_fn = _load("asset_checks_for_accept", "tools/pipeline/asset_checks.py", root).bundle_check
    bundle = bundle_fn(img, items, bool(on), cast=job.get("cast")) if bundle_cast_ok(bundle_fn) else bundle_fn(img, items, bool(on))
    bundle = description_record_only(bundle, items, tags, job.get("stage"))
    lab, src = judge_bundle(post, an, bundle)
    status, reasons = verdict(post, lab, bundle)
    return {"status": status, "label": lab, "label_source": src, "reasons": reasons, "check_items": items, "check_tags": tags, "anomaly": an,
            "bundle": {k: bundle.get(k) for k in ("status", "text", "items", "age", "ocr", "error", "summary")}, "post": post}


def apply(pf: dict, acc: dict) -> dict:
    """把共用验收结论写回 postflight（同一份 dict，原地改并返回）：Q9 用补跑后的；加 QB；reject 不过；retry 走只重打分"""
    post = acc.get("post") or {}
    q9 = next((i for i in post.get("items") or [] if i.get("id") == "Q9"), None)
    items = [i for i in (pf.get("items") or []) if i.get("id") not in ("QB",) and not (q9 and i.get("id") == "Q9")]
    if q9:
        items.append(q9)
    st = {"pass": "pass", "reject": "fail", "retry": "untested"}[acc["status"]]
    items.append({"id": "QB", "title": "共用验收（看图逐条 + 年龄 + OCR + 人数，PRODMERGE-O）", "status": st,
                  "detail": "；".join(acc.get("reasons") or [])[:400],
                  "evidence": {k: acc.get(k) for k in ("label", "label_source", "check_items", "check_tags", "anomaly", "bundle")}})
    pf["items"] = items
    pf["accept"] = {"status": acc["status"], "label": acc.get("label"), "reasons": acc.get("reasons")}
    if acc["status"] == "reject":
        pf["ok"] = False
        pf["blocking"] = sorted(set(pf.get("blocking") or []) | {"QB"})
        pf.setdefault("reasons", []).extend(acc.get("reasons") or [])
    elif acc["status"] == "retry":
        pf["accept_retry"] = True
        pf["score_infra"] = True          # producer：图已入库，只重打分、不重出、不计次
        pf["infra_error"] = ("QB 共用验收 retry：" + "；".join(str(r) for r in acc.get("reasons") or [])[:300]) \
            if acc.get("reasons") else "QB 共用验收 retry（无原因字段）"   # W-SCORE 11-07：score_infra 永远带原因
    return pf


def accept(file_rel: str, job: dict, host: str | None, shot, run: str, *, post: dict | None = None, data_dir: Path | None = None,
           **kw) -> dict:
    """完整一步（定稿档等没有自己打分的入口用）：postflight（unified_frame._postflight）→ evaluate → apply → 返回 postflight"""
    if post is None:
        from ns_render import unified_frame as UF
        post = UF._postflight(file_rel, job, host)
    if data_dir is None:
        from ns_render import pipeline as P
        data_dir = P.DATA
    pf = dict(post or {})
    if not enabled(run, kw.get("root", ROOT)):
        return pf
    acc = evaluate(str(Path(data_dir) / file_rel), pf, job, shot, run, **kw)
    return apply(pf, acc)


__all__ = ["accept", "apply", "check_items", "check_rows", "description_record_only", "owner_picks", "config", "enabled", "ensure_q9", "evaluate", "judge_bundle", "q9_timeout", "verdict"]
