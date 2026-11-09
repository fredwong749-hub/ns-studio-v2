"""审片史每日导出（审查 3 §9.5，项目方 11-09 定“加每日导出”）：reviews + triggers 表 → JSON，留最近 14 份。

    python -m ns2.export_reviews --db /root/work/fredw/ns2/data/queue.db --out /root/work/fredw/ns2/backup/reviews [--keep 14]
定时（gpu33 root crontab，每天 05:10 JST = 服务器 15:10 EST）：见 ns2/install_review_export_cron.sh。导出目录在服务器工作目录，不进公开仓库。
恢复：python -m ns2.export_reviews --restore <json> --db <queue.db>（只补缺的行，不覆盖库里已有的定稿 / 驳回）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path


def export(db: str, out_dir: str, keep: int = 14, now: str | None = None) -> Path:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    doc = {"at": now or time.strftime("%Y-%m-%dT%H:%M:%S%z"), "db": db,
           "reviews": [dict(r) for r in c.execute("SELECT * FROM reviews ORDER BY run_id, shot")],
           "triggers": [dict(r) for r in c.execute("SELECT * FROM triggers ORDER BY run_id, shot")]}
    c.close()
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"reviews_{(now or time.strftime('%Y%m%d-%H%M%S')).replace(':', '').replace('-', '')[:15]}.json"
    tmp = f.with_suffix(".part")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(f)
    for old in sorted(d.glob("reviews_*.json"))[:-keep] if keep > 0 else []:
        old.unlink()
    return f


def restore(db: str, src: str) -> dict:
    """只补缺的行（INSERT OR IGNORE），不覆盖库里已有的状态"""
    doc = json.loads(Path(src).read_text(encoding="utf-8"))
    c = sqlite3.connect(db)
    n = {"reviews": 0, "triggers": 0}
    for r in doc.get("reviews") or []:
        n["reviews"] += c.execute("INSERT OR IGNORE INTO reviews(run_id, shot, state, reason, at) VALUES (?,?,?,?,?)",
                                  (r["run_id"], r["shot"], r["state"], r.get("reason"), r["at"])).rowcount
    for r in doc.get("triggers") or []:
        n["triggers"] += c.execute("INSERT OR IGNORE INTO triggers(run_id, shot, at, by) VALUES (?,?,?,?)",
                                   (r["run_id"], r["shot"], r["at"], r.get("by"))).rowcount
    c.commit()
    c.close()
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/root/work/fredw/ns2/data/queue.db")
    ap.add_argument("--out", default="/root/work/fredw/ns2/backup/reviews")
    ap.add_argument("--keep", type=int, default=14)
    ap.add_argument("--restore")
    a = ap.parse_args(argv)
    if a.restore:
        print(json.dumps(restore(a.db, a.restore)))
    else:
        print(export(a.db, a.out, a.keep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
