"""一集开工（M2）：把一集剧本交给新系统。之后全自动：剧本导入（两台）→ 每镜分镜预检 → 首帧 → 尾帧 → 视频 → 整集合成。

    python -m ns2.enqueue_episode --api http://10.0.0.33:8900 --run series10_v2 --ep 5 --md P4_ep05.md [--version v3] [--dry]

剧本正文放进 payload（与机器无关），每台各一行 script（两台 data 各一份导入稿）；镜头行由出图机的剧本步导入后自动入队（ns2/step_script.py）。
成人镜：board 判出后本镜首 / 尾 / 视频标 owner_trigger，只由项目方在前端点“用这版 / 触发”放行。
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from ns2.enqueue_video import post
from ns2.migrate import PARTITION


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", required=True); ap.add_argument("--run", required=True)
    ap.add_argument("--ep", type=int, required=True); ap.add_argument("--md", required=True)
    ap.add_argument("--version"); ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    text = Path(a.md).read_text(encoding="utf-8")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    for host in sorted({h for kind in PARTITION.values() for h in kind}):
        r = {"run_id": a.run, "ep": a.ep, "shot": f"ep{a.ep:02d}", "step": "script", "want_host": host, "max_tries": 1,
             "sig": {"script": sha}, "payload": {"md_text": text, "name": Path(a.md).name, "version": a.version,
                                                 "enqueue_shots": host == next(h for h, eps in PARTITION["images"].items() if a.ep in eps)}}
        if a.dry:
            print("script", host, sha, "enqueue_shots" if r["payload"]["enqueue_shots"] else "")
            continue
        out = post(a.api, "/api/jobs", r)
        print("script", host, out["job"]["id"], "new" if out["created"] else "existing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
