"""通用入队：python -m ns2.enqueue --api URL --run smoke_v2 --shots ep01-S1,ep01-S2 --steps first,last --host gpu33 [--tag m1c]
同 (run, shot, step, tag) 幂等；视频步用 ns2.enqueue_video（要带编译好的变体）。"""
from __future__ import annotations

import argparse

from ns2.enqueue_video import post


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", required=True); ap.add_argument("--run", required=True)
    ap.add_argument("--shots", required=True); ap.add_argument("--steps", required=True)
    ap.add_argument("--host"); ap.add_argument("--tag", default="")
    a = ap.parse_args(argv)
    for sid in a.shots.split(","):
        for step in a.steps.split(","):
            out = post(a.api, "/api/jobs", {"run_id": a.run, "ep": int(sid[2:4]), "shot": sid, "step": step,
                                             "want_host": a.host, "sig": {"tag": a.tag}})
            print(sid, step, out["job"]["state"], "id", out["job"]["id"], "new" if out["created"] else "existing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
