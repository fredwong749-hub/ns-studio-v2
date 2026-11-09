"""旧出图执行器的一次调用（M1c）：在旧部署目录（cwd = $OFF/repo，sv-20261107-13）里跑，出一张首 / 尾帧 + 契约打分。

    cd /root/work/fredw/offline_20261101/repo && python <本文件> '<json 参数>'
参数：{"kind": "first|last", "sid": "ep01-S1", "run": "smoke_v2", "lane": "gpu33:0", "port": 8188, "attempt": 1, "prev_blocking": [[...], ...]}
输出最后一行：@@NS2@@{...}
复用而不重写（PLAN 原则 5）：作业编排 / 阶梯 / preflight / 成人触发 / GPU 打分 / 看图闸门 / 共用验收 / FL / 硬项政策 / 自动设当前，全是旧
producer.FrameExec + Producer.rung* 的原代码；新系统只管派单、重试计数与状态。M2–M4 萃取进 ns2 后删除本文件。
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

ROOT = Path.cwd()
for p in ("", "tools/autopilot", "services/agent", "services/render_router", "packages/schema", "services/recipes", "services/post"):
    if str(ROOT / p) not in sys.path:
        sys.path.insert(0, str(ROOT / p))


def UF_project(script, run):
    from ns_render import unified_frame as UF
    return UF.project_for(script) or run


class _Stub:
    """借用 Producer 的阶梯方法（rung / rung_ab / seed_pick / two_person）——只给它们用到的属性"""

    def __init__(self, PR, paths, script, prev_blocking):
        self.PR, self.p, self._s, self.prev = PR, paths, script, prev_blocking

    def script(self):
        return self._s

    def seed_pick(self, o, attempt, salt="", legacy_key=None):
        return self.PR.Producer.seed_pick(self, o, attempt, salt, legacy_key)

    def two_person(self, sid):
        return self.PR.Producer.two_person(self, sid)

    def fl_escalate_level(self, o, attempt):
        """同 Producer.fl_escalate_level，但历史来自新队列（本 job 前几次的 blocking），不读旧账本"""
        if o.get("kind") != "last" or attempt < 2 or not self.prev or "FL" not in (self.prev[-1] or []):
            return 0
        return min(2, sum(1 for b in self.prev if "FL" in (b or [])))

    def rung_ab(self, o, attempt):
        return self.PR.Producer.rung_ab(self, o, attempt)

    def rung(self, o, attempt):
        return self.PR.Producer.rung(self, o, attempt)


BEST_EFFORT_REASON = "P6 尽力版一律自动用（项目方 11-09）：用完次数仍没过契约，按不过项最少、同等取最新设为当前"


def i2va_fallback(P, sid: str, d: dict) -> dict:
    """同 producer.last_capped_fallback / gaze_fallback：takes.json 顶层写 last_frame_fallback（绑定当前首帧），视频侧 build_jobs 编成 I2VA"""
    import time
    if d.get("last_frame_fallback"):
        return {"promoted": None, "fallback": "i2va", "why": "已有尾帧兜底标记"}
    first_n = (d.get("active") or {}).get("image")
    if first_n is None:
        return {"promoted": None, "why": "没有当前首帧，不能退回只给首帧"}
    why = (f"尾帧用完次数，候选全部与首帧太像（L-203 不采用）→ 本镜视频只给首帧（I2VA），运镜交给视频模型；绑定首帧 v{first_n}"
           "（P6 尽力版一律自动用，同旧 L-204 D8 兜底）")
    flag = {"mode": "i2va", "first_n": first_n, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "by": "ns2-best-effort", "rule": "L-204 D8", "why": why}
    P.mutate_takes(sid, lambda x: x.__setitem__("last_frame_fallback", flag), op="last_frame_fallback", by="ns2-best-effort")
    return {"promoted": None, "fallback": "i2va", "first_n": first_n, "why": why}


BEST_EFFORT_KEY = "best_effort"        # takes.json 顶层 {stage: {n, at, by, reason, blocking}}：尽力版自动用的来源标记（auto_current 规则 4 认它）


def promote_best(P, sid: str, stage: str, adult: bool = True, triggered: bool = False) -> dict:
    """项目方 11-09“尽力版一律自动用”（范围：全部集，项目方 11-09 再确认）：这一镜这一阶段没有合格的当前版时，
    从不是坏图 / 没被驳回的版本里选不过项最少的一版设为当前，并在 takes.json 顶层 best_effort[stage] 打来源标记。
    - auto_current 规则 4（ns2/legacy_rules.py 运行时补上，vendored 规则文本同步）：当前版是带标记的尽力版时，重打分不清空当前
    - 成人镜（adult，按剧本；拿不准 = 当成人）未经项目方触发 → 拒绝提升（审查 3 §5b）
    - 项目方本人定稿锁过的不动；已有合格当前版的不动。返回 {promoted: n | None, why}"""
    import time
    from ns_render import auto_current as AC
    if adult and not triggered:
        return {"promoted": None, "refused": "adult", "why": "成人镜未经项目方触发，尽力版不自动提升（L-128）"}
    d = P.load_takes(sid)
    takes = [t for t in d.get("takes") or [] if t.get("stage") == stage]
    act = (d.get("active") or {}).get(stage)
    cur = next((t for t in takes if t.get("n") == act), None)
    if cur is not None and AC.passes(cur):
        return {"promoted": None, "why": f"已有合格当前版 v{act}"}
    if AC.owner_lock(d, stage) is not None:
        return {"promoted": None, "why": "项目方定稿锁着，不动"}
    cands = [t for t in takes if not P.ineligible_reason(t) and AC.take_pf(t)]
    if not cands:
        return {"promoted": None, "why": "没有可用版本（全是坏图 / 被驳回 / 没打过分）"}
    if stage == "last":
        from ns_agent.gen_contract import first_last as FL
        ok_cands = [t for t in cands if not FL.too_similar(AC.take_pf(t))]
        if not ok_cands:              # 尾帧全都“与首帧太像”（L-203 硬规：不采用）→ 旧系统同款兜底：视频只给首帧（I2VA），运镜交给视频模型
            return i2va_fallback(P, sid, d)
        cands = ok_cands
    best = min(cands, key=lambda t: (len(AC.take_pf(t).get("blocking") or []), -int(t["n"])))
    blocking = AC.take_pf(best).get("blocking")
    if best["n"] != act:
        P.set_active(sid, stage, int(best["n"]), reason=BEST_EFFORT_REASON, actor="ns2-best-effort")
    mark = {"n": int(best["n"]), "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "by": "ns2-best-effort", "reason": BEST_EFFORT_REASON,
            "blocking": blocking}
    P.mutate_takes(sid, lambda x: x.setdefault(BEST_EFFORT_KEY, {}).__setitem__(stage, mark), op="best_effort_mark", by="ns2-best-effort")
    return {"promoted": int(best["n"]), "blocking": blocking, "why": BEST_EFFORT_REASON}


def main(a: dict) -> dict:
    os.environ.setdefault("NS_RUN", a["run"])
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import legacy_rules
    legacy_rules.apply()                                        # P6：旧规则统一修正（与 board 预检同口径）
    import producer as PR
    from ns_render import pipeline as P
    from ns_render.comfy_client import ComfyError
    paths = PR.Paths(ROOT, run_id=a["run"])
    if a.get("edit"):                 # 项目方“修改”意见：旧 FrameExec 从 paths.rejects 读 {kind:sid: {edit}} 套进作业（apply_owner_edit）
        import tempfile
        tf = Path(tempfile.mkdtemp(prefix="ns2edit_")) / "rejects.json"
        tf.write_text(json.dumps({f"{a['kind']}:{a['sid']}": {"edit": a["edit"]}}, ensure_ascii=False), encoding="utf-8")
        paths.__class__ = type("NS2EditPaths", (PR.Paths,), {"rejects": property(lambda self, _f=tf: _f)})   # rejects 是只读属性
    script = P.load_script()
    if a.get("owner_triggered"):     # B2：项目方已在新系统触发本镜 → 旧 FrameExec 的成人闸认这次触发（没触发时旧闸照挡 = 第二道防线）
        _orig = PR.owner_trigger_ids
        PR.owner_trigger_ids = lambda p, _o=_orig, _s=a["sid"], _r=a["run"]: set(_o(p)) | {f"first:{_s}", f"{_r}/first:{_s}", f"last:{_s}", f"{_r}/last:{_s}"}
    if a.get("promote_only"):        # 补选：已是尽力版的镜（规则上线前用完次数的）按同一规则选一版设当前
        return {"outcome": "promoted", **promote_best(P, a["sid"], "image" if a["kind"] == "first" else "last",
                                                      adult=a.get("adult", True), triggered=bool(a.get("owner_triggered")))}
    st = _Stub(PR, paths, script, a.get("prev_blocking") or [])
    o = {"id": f"{a['kind']}:{a['sid']}", "shot": a["sid"], "kind": a["kind"]}
    if a.get("rescore_take"):        # M2：上次图已出、只是打分 infra 出错 → 只重打分，不重出图（同旧 Producer._rescore_one）
        from ns_agent import frame_job as FJ
        stage = "image" if a["kind"] == "first" else "last"
        n = int(a["rescore_take"])
        ep, shot = P.find_shot(script, a["sid"])
        fj = FJ.build_first(UF_project(script, a["run"]), ep, shot, script.globals)
        t = next(x for x in P.load_takes(a["sid"])["takes"] if x["stage"] == stage and x["n"] == n)
        pf = PR.FrameExec(paths, script, post_workers=1)._postflight(a["sid"], stage, n, t["file"],
                                                                    {"cast": fj.get("cast"), "asset_refs": fj.get("asset_refs")}, "gpu33", None)
        out = {"take": n, "file": t["file"], "rescored": True, "ok": bool(pf.get("ok")), "blocking": pf.get("blocking"),
               "reasons": (pf.get("reasons") or [])[:6], "current": pf.get("current")}
        if pf.get("score_infra") or pf.get("ok") is None:
            return {**out, "outcome": "infra", "error": "只重打分仍 infra：" + str(pf.get("error") or "score_infra")[:300]}
        return {**out, "outcome": "pass" if pf.get("ok") else "fail"}
    attempt = int(a.get("attempt") or 1)
    rung = st.rung_ab(o, attempt) or st.rung(o, attempt)
    fx = PR.FrameExec(paths, script, post_workers=1)
    tag = {"agent": "NS2", "kind": a["kind"], "shot": a["sid"], "label": rung.get("name")}
    try:
        res = fx.render(a["kind"], a["sid"], a["lane"], f"http://127.0.0.1:{a['port']}", attempt, rung, tag)
    except ComfyError as e:
        msg = str(e)
        kind = "blocked" if ("preflight 不通过" in msg or "首帧没有过契约" in msg) else "content_bad" if " v" in msg and "bad" in msg.lower() else "infra"
        return {"outcome": kind, "error": msg[:600], "rung": rung.get("name")}
    if res.get("na"):
        return {"outcome": "na", "rung": rung.get("name")}
    try:
        pf = res["pf_future"].result(timeout=1800)
    except Exception as e:  # noqa: BLE001 — 图已入库、打分超时 = infra（旧系统同：只重打分不计次）
        return {"outcome": "infra", "take": res["take"], "file": res["file"], "error": f"契约打分超时 / 出错：{e!r}"[:400]}
    out = {"take": res["take"], "file": res["file"], "gpu_s": res.get("gpu_s"), "rung": rung.get("name"), "seed": rung.get("seed"),
           "ok": bool(pf.get("ok")), "blocking": pf.get("blocking"), "reasons": (pf.get("reasons") or [])[:6],
           "current": pf.get("current"), "review_needed": pf.get("review_needed")}
    if pf.get("score_infra"):
        return {**out, "outcome": "infra", "error": "契约打分 score_infra（gpu33 显存 / 连接 / 看图排队）"}
    if not pf.get("ok") and a.get("final_attempt"):    # 最后一次仍不过 → 尽力版自动用（项目方 11-09）
        out["best_effort"] = promote_best(P, a["sid"], "image" if a["kind"] == "first" else "last",
                                          adult=a.get("adult", True), triggered=bool(a.get("owner_triggered")))
    return {**out, "outcome": "pass" if pf.get("ok") else "fail"}


if __name__ == "__main__":
    try:
        r = main(json.loads(sys.argv[1]))
    except Exception as e:  # noqa: BLE001
        r = {"outcome": "infra", "error": f"{type(e).__name__}: {e}"[:400], "tb": traceback.format_exc()[-1500:]}
    print("@@NS2@@" + json.dumps(r, ensure_ascii=False, default=str), flush=True)
