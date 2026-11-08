"""P6 R1a assets 步的一次调用：在旧部署目录（cwd = $OFF/repo）里，把一集新剧本出首帧前缺的前期资产全部在服务器本机补齐。

    cd /root/work/fredw/offline_20261101/repo && python <本文件> '<json 参数>'
参数：{"run": "series10_v2", "ep": 5, "lane": "gpu33:0", "host": "gpu33", "max_passes": 3}
输出最后一行：@@NS2@@{...}

复用旧“阶段 A 资产作业”（tools/pipeline/asset_jobs.py + asset_worker.py + tools/master/episode_ready.py），只换掉笔记本那部分：
- ComfyUI：本机端口（旧代码写死 lane_url(from_host="laptop") 走笔记本隧道）
- 人数检查：本机（旧代码写死 host="gpu36"；服务器之间不互连）
- 选图：一律按分数自动选用（项目方 11-09：默认通过、其他人物全合成全自动），不投待办等人
- 推送：不推（服务器就是唯一来源；P6 R1 一份数据）
- 派生参考图（refboards / refcrops）：服务器上直接生成并记账（旧规矩 K-OPS-F23 只许笔记本生成，前提是笔记本为源，P6 已不成立）
每轮：episode_ready 查缺口 → asset_jobs.generate 排作业 → 按 meta → face → scene → sheet → item 顺序跑 → 每镜编译一次生成派生参考图；
缺口清零或没有进展就停。写手 / 总调度的缺口（不是资产线能补的）原样报回，由 board 预检写清。
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

ROOT = Path.cwd()
for p in ("", "tools/autopilot", "tools/pipeline", "tools/master", "tools/factory", "tools/vqa", "services/agent", "services/render_router",
          "packages/schema", "services/recipes", "services/post", "services/api"):
    if str(ROOT / p) not in sys.path:
        sys.path.insert(0, str(ROOT / p))

BY = "NS2-ASSETS（P6 服务器全自动）"
ORDER = {"meta": 0, "face": 1, "scene": 2, "sheet": 3, "item": 4}


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
    from ns_agent import asset_refs as AR
    AR._on_server = lambda: False          # 派生参考图在服务器上直接生成（make_board / sheet_crop + record_derived）
    return AJ, AW, AR


def run_jobs(AJ, AW, run: str, ep: int, lane: str, log: list) -> int:
    """跑本集所有待办资产作业，返回本轮完成数"""
    root = ROOT
    jobs = [j for j in AJ.list_jobs(root, run)
            if ep in (j.get("episodes") or [j.get("episode")]) and j.get("kind") in ORDER
            and (j.get("state") in ("pending", "awaiting_pick") or (j.get("state") == "failed" and not j.get("final")))]
    jobs.sort(key=lambda j: (ORDER[j["kind"]], j["id"]))
    done = 0
    for j in jobs:
        jp = AJ.job_path(root, run, j["id"])
        try:
            if j["state"] == "awaiting_pick":
                got = AW.auto_pick_waiting(root, j)
                ok = bool(got)
            elif j["kind"] == "meta":
                # P6 R1a 修：旧 meta 作业跨集复用时 job["episode"] 还是建作业那集（ep05 的祁念排期被写成 ep02，缺口永远在）→ 按当前集写
                res = AW.apply_meta(root, {**j, "episode": ep}, push=False)
                j = AJ.load_job(jp)
                j.update(state="done", result=res, done_at=AJ._now(), error=None)
                AJ.log(j, f"{BY} 写入：{json.dumps(res, ensure_ascii=False)[:300]}")
                AJ.save_job(root, j)
                ok = True
            elif AW.is_crop_job(j) and not (j.get("spec") or {}).get("crop_failed"):
                j = AW.run_crop(jp, root)
                ok = j.get("state") == "done"
            else:
                j = AW.run_job(jp, lane, root, renderer=lambda job, _l, seed, r: AW.render_one(job, lane, seed, r))
                if j.get("state") == "awaiting_pick":            # 代选登记被拒等 → 再按分数选一次
                    j2 = AJ.load_job(jp)
                    ok = bool(AW.auto_pick_waiting(root, j2)) or AJ.load_job(jp).get("state") == "done"
                else:
                    ok = AJ.load_job(jp).get("state") == "done"
            st = AJ.load_job(jp).get("state")
            log.append({"job": j["id"], "kind": j.get("kind"), "state": st, "error": (AJ.load_job(jp).get("error") or "")[:300]})
            done += bool(ok or st == "done")
        except Exception as e:  # noqa: BLE001  一个作业出错不挡别的作业
            log.append({"job": j.get("id"), "kind": j.get("kind"), "state": "error", "error": f"{type(e).__name__}: {e}"[:300]})
    return done


def derive_refs(run: str, ep: int, lane: str, port: int, log: list) -> dict:
    """每镜编译一次首帧作业：asset_refs 在服务器上生成派生参考图（拼脸板 / 设定图裁片）并记账"""
    import producer as PR
    from ns_render import pipeline as P
    from ns_render import unified_frame as UF
    from ns_render.comfy_client import ComfyClient
    from legacy_frame import _Stub
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    script = P.load_script()
    e = next(x for x in script.episodes if int(x.number) == ep)
    loras = P.available_loras(ComfyClient(f"http://127.0.0.1:{port}"))
    paths = PR.Paths(ROOT, run_id=run)
    st = _Stub(PR, paths, script, [])
    project = UF.project_for(script) or run
    out = {"ok": 0, "err": {}}
    for s in e.shots:
        sid = f"ep{ep:02d}-S{s.index}"
        try:
            P.compile_with_edits(script, sid, loras)
            o = {"id": f"first:{sid}", "shot": sid, "kind": "first"}
            rung = st.rung_ab(o, 1) or st.rung(o, 1)
            _, shot = P.find_shot(script, sid)
            PR.first_job_for_rung(project, e, shot, script.globals, rung, "first", run)
            out["ok"] += 1
        except Exception as ex:  # noqa: BLE001
            out["err"][sid] = f"{type(ex).__name__}: {ex}"[:200]
    log.append({"derive_refs": out})
    return out


def main(a: dict) -> dict:
    import os
    os.environ.setdefault("NS_RUN", a["run"])
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    run, ep, lane = a["run"], int(a["ep"]), a["lane"]
    AJ, AW, AR = patch(a["host"])
    import legacy_rules
    legacy_rules.apply()                                        # P6：旧规则统一修正（与 board 预检同口径）
    check = AJ._checker()
    log: list = []
    passes = []
    last_gaps = None
    for n in range(int(a.get("max_passes") or 3)):
        res = check(run, ep)
        gen = AJ.generate(run, ep, ROOT, res=res)
        gaps = [f"[{g.get('kind')}] {g.get('who')}：{g.get('what')}"[:200] for g in res.get("gaps") or []]
        passes.append({"pass": n + 1, "gaps": len(gaps), "created": gen.get("created"), "skipped": gen.get("skipped")})
        if not gaps or gaps == last_gaps and n > 0:
            break
        last_gaps = gaps
        done = run_jobs(AJ, AW, run, ep, lane, log)
        derive_refs(run, ep, lane, int(a["port"]), log)
        if not done and n > 0:
            break
    res = check(run, ep)
    gaps = res.get("gaps") or []
    asset_kinds = {"face", "scene", "look", "item", "name", "schedule"}
    left_assets = [g for g in gaps if g.get("kind") in asset_kinds and "写手" not in str(g.get("owner") or "")]
    out = {"passes": passes, "jobs": log[-60:], "gaps_left": [f"[{g.get('kind')}] {g.get('who')}：{g.get('what')}"[:240] for g in gaps][:40],
           "has_output": True}
    if left_assets:
        return {**out, "outcome": "blocked", "error": "资产没补齐：" + "；".join(out["gaps_left"][:8])[:900]}
    return {**out, "outcome": "ok"}


if __name__ == "__main__":
    try:
        r = main(json.loads(sys.argv[1]))
    except Exception as e:  # noqa: BLE001
        r = {"outcome": "infra", "error": f"{type(e).__name__}: {e}"[:600], "tb": traceback.format_exc()[-2000:]}
    print("@@NS2@@" + json.dumps(r, ensure_ascii=False, default=str), flush=True)
