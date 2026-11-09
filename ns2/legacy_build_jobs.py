"""旧 build_jobs（视频变体编译，纯编译不提交）的 ns2 入口：step_video.compile_payload 在旧部署目录（cwd = 旧 repo）按路径跑本文件。

两处修正（只改本进程里的引用，不改旧文件）：
1. 尽力版读视图（best_effort_view）：takes.json 里带 best_effort 标记、仍是当前版的首 / 尾帧，按“过 + 待复核（review_needed）”给旧代码看
   （项目方 11-09“尽力版一律自动用”；Q9 / 人数 anomaly 也不再单独挡这一版——它就是因为这些项没过才成为尽力版的）。
2. sop_gate 视频闸门 → 放行：ns2 下“能不能进视频”由队列前置决定（视频行要等首 / 尾帧 done / na / best_effort，db.FRAME_PASSED），
   剧本导入 / 分镜也是 ns2 自己的步骤；旧闸门要的“导入闸门 autoflow 记录 / 提示词探针证据”是旧派单器的账，ns2 新导入的集（ep05 起）没有，
   照旧判 = 新集视频永远 0 变体（11-09 恢复 ep05 实测 S2/S3/S6/S9/S12/S13 全被挡）。首尾帧步本来就不走 sop_gate，这里与之对齐。
"""
from __future__ import annotations

import json as _json
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent


def json_view_module():
    """build_jobs 里的 json 换成同名模块：loads 结果过一遍尽力版读视图（只影响读，dumps 不变）"""
    import best_effort_view as BV
    m = types.ModuleType("json")
    m.__dict__.update({k: getattr(_json, k) for k in dir(_json) if not k.startswith("__")})
    m.loads = lambda s, *a, **k: BV.view_doc(_json.loads(s, *a, **k))
    return m


def patch(BJ, G) -> None:
    import best_effort_view as BV
    BJ.json = json_view_module()
    oq, oa = BJ._q9_ok, BJ.anomaly_block
    BJ._q9_ok = lambda pf: True if (pf or {}).get(BV.FLAG) else oq(pf)
    BJ.anomaly_block = lambda sid, file, pf, rows: None if (pf or {}).get(BV.FLAG) else oa(sid, file, pf, rows)
    G.filter_video_specs = lambda run, specs, root=None: specs


def main(argv: list[str]) -> int:
    root = Path.cwd()
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    for p in (root / "tools/series10_video", root):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    sys.argv = [str(root / "tools/series10_video/build_jobs.py")] + argv
    import build_jobs as BJ
    from tools.master import sop_gate as G
    patch(BJ, G)
    sys.stdout.reconfigure(encoding="utf-8")
    BJ.set_run(BJ.RC.resolve())
    if "--validate" in argv:
        raise SystemExit("--validate 只给 legacy 剧，ns2 不用")
    print(_json.dumps(BJ.build_all(False), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
