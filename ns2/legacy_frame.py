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


def main(a: dict) -> dict:
    os.environ.setdefault("NS_RUN", a["run"])
    import producer as PR
    from ns_render import pipeline as P
    from ns_render.comfy_client import ComfyError
    paths = PR.Paths(ROOT, run_id=a["run"])
    script = P.load_script()
    st = _Stub(PR, paths, script, a.get("prev_blocking") or [])
    o = {"id": f"{a['kind']}:{a['sid']}", "shot": a["sid"], "kind": a["kind"]}
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
    return {**out, "outcome": "pass" if pf.get("ok") else "fail"}


if __name__ == "__main__":
    try:
        r = main(json.loads(sys.argv[1]))
    except Exception as e:  # noqa: BLE001
        r = {"outcome": "infra", "error": f"{type(e).__name__}: {e}"[:400], "tb": traceback.format_exc()[-1500:]}
    print("@@NS2@@" + json.dumps(r, ensure_ascii=False, default=str), flush=True)
