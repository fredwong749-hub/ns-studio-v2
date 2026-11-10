"""R4 质量 6：多人同框“排排站”——站位写法 A/B（11-10）。

走旧唯一出图路径的 scope=ab（ns_render.unified_frame.render_ab）：同一套编译 / preflight / 出图 / 契约打分，
不写 takes.json、不写契约账本，图存 <旧部署 repo>/data/ab/r4_staging/<镜>/，每张一行进 takes.jsonl。

    cd <旧部署 repo> && python experiments/r4_staging_ab.py --port 8188 [--shots ep05-S1,ep05-S4,ep05-S7] [--seeds 4]
臂（在编译好的提示词“（按此左右站位，不互换）”后面追加一句，原文不改）：
  base  不改
  diag  斜线纵深：前近后远、不在同一水平线、身体朝向彼此
  talk  围成半圆交谈：身体侧向彼此，不一起面向镜头
  ots   过肩：站位写在前景的那个人背对镜头、只以后脑和肩背入画（“N 人同框，N 张脸都清晰入画”改成“前景一人背对镜头……”）
结果（11-10，3 镜 × 4 种子）：base 排排站 8/12、diag 9/12、talk 7/12——光加文字压不住，见 reports/p6/r4_staging_ab.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path.cwd()
for p in ("", "tools/autopilot", "services/agent", "services/render_router", "packages/schema", "services/recipes", "services/post"):
    if str(ROOT / p) not in sys.path:
        sys.path.insert(0, str(ROOT / p))

ANCHOR = "（按此左右站位，不互换）"
ARMS = {
    "base": None,
    "diag": "。人物沿一条斜线由近到远纵深排开：前景的人离镜头很近、画面里较大，后面的人依次变小变远，几个人的头不在同一水平线上，"
            "彼此的身体朝向对方而不是一起朝向镜头",
    "ots": None,
    "pv2": None,          # 提示词不改；进程带 NS2_PREVIZ_FIT=1（ns2/previz_fix.py：摆位图相机距离封顶 + 多脸默认侧向中心）
    "talk": "。几个人围成一个朝向彼此的半圆，像正在交谈：每个人的身体都侧向说话的人，肩膀相互错开，没有人正面朝向镜头",
}
EXP = "r4_staging"
import re as _re
FACES_RE = _re.compile(r"(两|三|四)人同框，(两|三|四)张脸都清晰入画")
OTS = "前景一人背对镜头、只以后脑和肩背入画（过肩构图，镜头越过其肩头看向其他人），其余的脸清晰入画"


def ots_edits(prompt: str) -> list | None:
    """过肩臂：把“N 人同框，N 张脸都清晰入画”换成过肩写法；没有这句 → None（该镜不跑这一臂）"""
    m = FACES_RE.search(prompt)
    return [[m.group(0), m.group(1) + "人同框，" + OTS]] if m else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8188)
    ap.add_argument("--shots", default="ep05-S1,ep05-S4,ep05-S7")
    ap.add_argument("--seeds", type=int, default=4)
    ap.add_argument("--arms", default="base,diag,talk")
    a = ap.parse_args()
    os.environ.setdefault("NS_RUN", "series10_v2")
    sys.path.insert(0, "/root/work/fredw/ns2/code/ns2")
    import legacy_rules
    legacy_rules.apply()
    from ns_render import pipeline as P
    from ns_render import unified_frame as UF
    from ns_render.comfy_client import ComfyClient
    script = P.load_script()
    client = ComfyClient(f"http://127.0.0.1:{a.port}")
    out = UF.ab_dir(EXP)
    seen = set()
    if (out / "takes.jsonl").is_file():
        for ln in (out / "takes.jsonl").read_text(encoding="utf-8").splitlines():
            r = json.loads(ln)
            seen.add((r["shot"], r["tier"], r["seed"]))
    for sid in a.shots.split(","):
        for k in range(1, a.seeds + 1):
            seed = P.seed_for(sid + "/r4staging", k)
            for arm in a.arms.split(","):
                if (sid, arm, seed) in seen:
                    continue
                if arm == "ots":
                    edits = ots_edits(UF._job_for(script, sid, UF.project_for(script), None)["prompt"])
                    if not edits:
                        print(sid, arm, "没有“N 张脸都清晰入画”，跳过", flush=True)
                        continue
                else:
                    edits = [[ANCHOR, ANCHOR + ARMS[arm]]] if ARMS[arm] else None
                t0 = time.time()
                try:
                    rec = UF.render_ab(client, script, sid, experiment=EXP, tier=arm, seed=seed, prompt_edits=edits,
                                       postflight=True, host="gpu33", tag={"agent": "NS2-R4", "kind": "ab", "shot": sid, "label": arm})
                except Exception as e:  # noqa: BLE001
                    print(sid, arm, seed, "失败：", type(e).__name__, str(e)[:300], flush=True)
                    continue
                pf = rec.get("postflight") or {}
                g1 = next((i for i in pf.get("items") or [] if i.get("id") == "G1"), {})
                print(sid, arm, seed, rec["file"], f"{time.time() - t0:.0f}s", "ok", pf.get("ok"), "blk", pf.get("blocking"),
                      "lineup", ((g1.get("evidence") or {}).get("lineup_geo") or {}).get("lineup"), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
