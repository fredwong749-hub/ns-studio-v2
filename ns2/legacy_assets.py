"""P6 R1a 资产执行器（在旧部署目录 cwd = $OFF/repo 里跑）。两种模式：

    plan：{"mode": "plan", "run", "ep", "host"} —— 查本集缺口 → 排旧资产作业 → 不用 GPU 的登记（造型骨架 / 本集排期 / 名字）当场写；
          要出图的资产（身份脸 / 场景底图 / 设定图 / 单品图）原样报回，由 ns2 每个资产排一行 asset（各道并行领，镜只等自己缺的）
    one： {"mode": "one", "run", "ep", "host", "lane", "asset"} —— 出一个资产：出到 1 张过检就按分数自动选用登记；
          2 张都不过 → 停（blocked，写清根因，不盲试：项目方止损律）；看图服务没出结论 → infra（回队不计次）
输出最后一行：@@NS2@@{...}

复用旧“阶段 A 资产作业”（tools/pipeline/asset_jobs.py + asset_worker.py + tools/master/episode_ready.py），只换掉笔记本那部分：
ComfyUI 走本机端口；人数检查本机；一律按分数自动选用（项目方 11-09：默认通过、其他人物全合成全自动）；不推送（服务器即唯一来源）。
11-09 实测为什么换成这样（reports/p6/r1a_assets_speed.md）：旧做法一条道串行出全集资产、每个资产凑 2 张过检、同一原因盲试 5 次，
ep05 资产跑了 65 分钟、4 条道空转、13 镜全在等。
"""
from __future__ import annotations

import json
import re
import sys
import traceback
from pathlib import Path

ROOT = Path.cwd()
for p in ("", "tools/autopilot", "tools/pipeline", "tools/master", "tools/factory", "tools/vqa", "services/agent", "services/render_router",
          "packages/schema", "services/recipes", "services/post", "services/api"):
    if str(ROOT / p) not in sys.path:
        sys.path.insert(0, str(ROOT / p))
sys.path.insert(0, str(Path(__file__).resolve().parent))

BY = "NS2-ASSETS（P6 服务器全自动）"
RENDER_ORDER = {"face": 1, "scene": 2, "sheet": 3, "item": 4}


def patch(host: str):
    """把旧资产线里只能在笔记本跑的几处换成服务器本机（只改本进程里的函数引用，不改旧文件）"""
    import hosts as H
    import asset_jobs as AJ
    import asset_worker as AW
    _lane_url = H.lane_url
    H.lane_url = lambda lane, from_host=None, d=None: _lane_url(lane, from_host=host, d=d)
    import anomaly as A
    _check = A.check
    A.check = lambda img, cast=None, host_=None, **k: _check(img, cast, host=host, **{x: v for x, v in k.items() if x != "host"})
    AJ.delegate_active = lambda root: (True, "P6 服务器全自动（项目方 11-09：默认通过）")
    AJ.push_enabled = lambda root: False
    AJ.push_files = lambda root, rels, hosts=None, pusher=None: {"skipped": "P6：服务器即唯一来源，不推"}
    _cfg = AW.config
    AW.config = lambda root=ROOT: {**_cfg(root), "candidates": {k: 1 for k in RENDER_ORDER}}   # 自动选用只用 1 张：过 1 张就收
    import legacy_rules
    legacy_rules.apply()
    return AJ, AW


def _gaps(res: dict) -> list[str]:
    return [f"[{g.get('kind')}] {g.get('who')}：{g.get('what')}"[:240] for g in res.get("gaps") or []]


def plan(a: dict) -> dict:
    run, ep = a["run"], int(a["ep"])
    AJ, AW = patch(a["host"])
    check = AJ._checker()
    meta_log = []
    for _ in range(2):                                   # 登记类写完再查一次（排期写进去后造型缺口才消）
        res = check(run, ep)
        AJ.generate(run, ep, ROOT, res=res)
        metas = [j for j in AJ.list_jobs(ROOT, run) if j.get("kind") == "meta" and j.get("state") in ("pending", "failed")
                 and ep in (j.get("episodes") or [j.get("episode")])]
        if not metas:
            break
        for j in metas:
            # 旧 meta 作业跨集复用时 job["episode"] 还是建作业那集（ep05 的祁念排期曾被写成 ep02）→ 按当前集写
            r = AW.apply_meta(ROOT, {**j, "episode": ep}, push=False)
            jj = AJ.load_job(AJ.job_path(ROOT, run, j["id"]))
            jj.update(state="done", result=r, done_at=AJ._now(), error=None)
            AJ.log(jj, f"{BY} 写入（第 {ep} 集）：{json.dumps(r, ensure_ascii=False)[:300]}")
            AJ.save_job(ROOT, jj)
            meta_log.append({"job": j["id"], "result": r})
    res = check(run, ep)
    AJ.generate(run, ep, ROOT, res=res)
    jobs = [j for j in AJ.list_jobs(ROOT, run) if j.get("kind") in RENDER_ORDER and ep in (j.get("episodes") or [j.get("episode")])
            and j.get("state") in ("pending", "awaiting_pick", "failed", "running")]
    faces = {j["target"]: j["id"] for j in jobs if j["kind"] == "face"}
    out = []
    for j in sorted(jobs, key=lambda j: (RENDER_ORDER[j["kind"]], j["id"])):
        cid = str(j.get("target") or "").split(":")[0]
        after = [faces[cid]] if j["kind"] in ("sheet", "item") and cid in faces else []
        # 只挡缺口清单里写了的镜；没写镜号的（如设定图登记后旧代码顺手排的单品裁图）不在任何镜的缺口里 → 不挡分镜（11-09：曾默认 "*" 把 13 镜全挡住）
        out.append({"asset": j["id"], "kind": j["kind"], "shots": [f"ep{ep:02d}-S{n}" for n in j.get("shots") or []], "after": after})
    writer = [g for g in res.get("gaps") or [] if not any(str(g.get("kind")) == k for k in ("face", "scene", "look", "item", "name", "schedule"))]
    return {"outcome": "ok", "assets": out, "meta": meta_log, "gaps": _gaps(res)[:40], "other_gaps": _gaps({"gaps": writer})[:20]}


TEXTY_ZH = re.compile(r"印着|印有|号码|编号|字样|字母|文字|标志|标识|logo|LOGO|品牌|牌子|商标")
TEXTY_EN = re.compile(r"\b(numbered|number|lettered|letters?|logo|brand(ed)?|printed|text|label(l)?ed|monogram)\b", re.I)


def sanitize_spec(spec: dict) -> list[str]:
    """设定图规格里去掉带字 / 带标志的部件和括号里的剧情备注（P6 11-09 根因：唐瑶造型 B 写“印着替补号码的黑色衣袋”+“（卓琳统一配）”，
    模型照画号码和字母，读字检查连毙 7 张——规格自相矛盾，换种子永远过不了）。原地改 spec，返回去掉了什么"""
    dropped: list[str] = []
    slots = spec.get("slots") or {}
    for k, v in list(slots.items()):
        if not isinstance(v, str):
            continue
        parts = [x for x in re.split(r"[、，,]", v) if x.strip()]
        keep = []
        for x in parts:
            if TEXTY_ZH.search(x):
                dropped.append(x.strip())
                continue
            y = re.sub(r"（[^）]*）|\([^)]*\)", "", x)
            if y != x:
                dropped.append(x.strip())
            keep.append(y)
        new = "、".join(keep)
        if new != v:
            if spec.get("img") and v in spec["img"]:
                spec["img"] = spec["img"].replace(v, new)
            slots[k] = new
    for key in ("img",):
        if spec.get(key):
            spec[key] = re.sub(r"（[^）]*统一配[^）]*）", "", spec[key])
    if spec.get("en"):
        eparts = [x for x in re.split(r",\s*", spec["en"]) if x.strip()]
        ekeep = []
        for x in eparts:
            if TEXTY_EN.search(x):
                if " with " in x and not TEXTY_EN.search(x.split(" with ")[0]):
                    ekeep.append(x.split(" with ")[0]); dropped.append(x.split(" with ", 1)[1])
                else:
                    dropped.append(x)
                continue
            ekeep.append(x)
        spec["en"] = ", ".join(ekeep)
    return dropped


def one(a: dict) -> dict:
    run, lane, aid = a["run"], a["lane"], a["asset"]
    AJ, AW = patch(a["host"])
    jp = AJ.job_path(ROOT, run, aid)
    j = AJ.load_job(jp)
    if j.get("state") == "done":
        return {"outcome": "ok", "asset": aid, "note": "已登记过"}
    if j.get("state") == "awaiting_pick":
        got = AW.auto_pick_waiting(ROOT, j)
        j = AJ.load_job(jp)
        if j.get("state") == "done" or got:
            return {"outcome": "ok", "asset": aid, "picked": got}
    if j.get("state") == "failed":                        # 上次 ns2 之外的失败：重新给一次机会（种子会换新的）
        j.update(state="pending", final=False)
        AJ.save_job(ROOT, j)
    if j.get("kind") in ("sheet", "face"):
        why = sanitize_spec(j.get("spec") or {})
        if why:
            AJ.log(j, f"{BY} 出图前清理规格（设定图不画带字道具 / 剧情备注；道具由镜头剧本体现）：去掉 {why}")
            AJ.save_job(ROOT, j)
    if AW.is_crop_job(j) and not (j.get("spec") or {}).get("crop_failed"):
        j = AW.run_crop(jp, ROOT)
    else:
        j = AW.run_job(jp, lane, ROOT, renderer=lambda job, _l, seed, r: AW.render_one(job, lane, seed, r))
    j = AJ.load_job(jp)
    if j.get("state") == "awaiting_pick":                 # 代选登记被拒 → 再按分数选一次
        AW.auto_pick_waiting(ROOT, j)
        j = AJ.load_job(jp)
    # 单品图从设定图裁：设定图登记后旧代码会排裁图作业，顺手在本行跑掉（纯裁剪 + 看图，不占 ComfyUI）
    crops = []
    if j.get("state") == "done" and j.get("kind") == "sheet":
        for c in AJ.list_jobs(ROOT, run):
            if c.get("state") == "pending" and AW.is_crop_job(c) and str(c.get("target") or "").startswith(str(j.get("target") or "~")):
                cc = AW.run_crop(AJ.job_path(ROOT, run, c["id"]), ROOT)
                crops.append({"job": c["id"], "state": cc.get("state")})
    cands = j.get("candidates") or []
    info = {"asset": aid, "kind": j.get("kind"), "state": j.get("state"), "candidates": len(cands),
            "passed": sum(1 for c in cands if c.get("passed")), "crops": crops, "log": (j.get("log") or [])[-3:], "has_output": True}
    if j.get("state") == "done":
        return {**info, "outcome": "ok"}
    err = str(j.get("error") or "")
    if j.get("state") == "pending" and ("不算失败" in err or "没测出" in err or "超时" in err):
        return {**info, "outcome": "infra", "error": err[:600]}
    return {**info, "outcome": "blocked", "error": f"资产 {aid} 两张候选都没过检，停下不盲试（根因见候选拒因）：{err[:700]}"}


if __name__ == "__main__":
    try:
        args = json.loads(sys.argv[1])
        import os
        os.environ.setdefault("NS_RUN", args["run"])
        r = plan(args) if args.get("mode") == "plan" else one(args)
    except Exception as e:  # noqa: BLE001
        r = {"outcome": "infra", "error": f"{type(e).__name__}: {e}"[:600], "tb": traceback.format_exc()[-2000:]}
    print("@@NS2@@" + json.dumps(r, ensure_ascii=False, default=str), flush=True)
