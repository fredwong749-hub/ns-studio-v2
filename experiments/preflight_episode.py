"""入队前整集预检（R4，11-11）：把一集新剧本导入到临时副本，逐镜编首帧 + 尾帧作业并跑生成契约 preflight，不出图、不写 live 数据。

ep07 第一次入队后，live 才报出三类问题，每类都要整集重入队：
  ① 过肩镜背影人物写“在画”（I2 缺身份脸）；
  ② “X在画、Y只以背影入画”同一小句 → 在画的人被判成背影；
  ③ 尾帧落幅里“视线投向…X的方向，…长发 / 刘海”→ 局部的人被判露脸（K-REN-708 矛盾）。
这些在出图前就能全部查出来。用法（gpu33，旧部署 repo 目录下）：

    T=/root/work/fredw/ns2/rep/epNN_pf; mkdir -p $T; cp data/import/runs/<run>/script_v2.json $T/base.json
    PYTHONPATH=services/agent:services/api:packages/schema:services/render_router:services/recipes:services/post:packages/qc:tools:. \
      $OFF/venv/bin/python tools/import_script.py --script P4_epNN.md -o $T/script_v2.json --merge-base $T/base.json --no-timeline
    $OFF/venv/bin/python /root/work/fredw/ns2/code/experiments/preflight_episode.py $T/script_v2.json <集号> [--run series10_v2]

输出每镜 first / last：OK 或 FAIL + 原因。尾帧用同集一张已有的图当“首帧占位”，所以尾帧的 I11（占位图不在资产清单）不算问题；
新场景还没生成底图时的 I5 也不算（入队后资产步会先出底图）。其余 FAIL 都要在入队前修掉。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

IGNORE = (re.compile(r"I11 资产包清单[^；]*"),)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("script_json")
    ap.add_argument("ep", type=int)
    ap.add_argument("--run", default="series10_v2")
    ap.add_argument("--placeholder", help="尾帧首帧占位图（data/ 下相对路径）；默认取本剧任一张已有首帧")
    a = ap.parse_args()
    root = Path.cwd()
    for p in ("", "tools/autopilot", "services/agent", "services/render_router", "packages/schema", "services/recipes", "services/post"):
        sys.path.insert(0, str(root / p))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ns2"))
    os.environ.setdefault("NS_RUN", a.run)
    import legacy_rules
    legacy_rules.apply()
    from ns_agent import frame_job as FJ
    from ns_render import pipeline as P
    from ns_render import unified_frame as UF
    P.SCRIPT_JSON = Path(a.script_json)
    s = P.load_script()
    proj = UF.project_for(s)
    ph = a.placeholder
    if not ph:
        for d in sorted((root / "data/renders" / a.run).glob("ep*/S*/takes")):
            imgs = sorted(d.glob("image_*_raw_post.png"))
            if imgs:
                ph = str(imgs[0].relative_to(root / "data"))
                break
    ep = next(e for e in s.episodes if e.number == a.ep)
    bad = 0
    for sh in ep.shots:
        for stage in ("first", "last"):
            try:
                e, shot = P.find_shot(s, sh.id)
                fj = FJ.build_first(proj, e, shot, s.globals)
                job = fj if stage == "first" else FJ.build_last_compose(proj, e, shot, s.globals, fj, ph)
                pf = UF._contract(job)
                why = "；".join(pf.reasons())
                for rx in IGNORE:
                    why = rx.sub("", why)
                why = why.strip("；")
                ok = pf.ok or not why
            except Exception as ex:  # noqa: BLE001
                ok, why = False, f"{type(ex).__name__}: {ex}"
            bad += not ok
            print(sh.id, stage, "OK" if ok else "FAIL", why[:400], flush=True)
    print("FAIL 数", bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
