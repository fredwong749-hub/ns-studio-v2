"""本机证据：真 uvicorn API（临时库、测试密钥）+ 真 enqueue_episode CLI + 真 step_script.shot_rows 入队，同输入跑两遍"""
import json, os, sqlite3, subprocess, sys, time, urllib.request
from pathlib import Path
repo = Path(sys.argv[1]); tmp = Path(sys.argv[2]); md = Path(sys.argv[3])
db = tmp / "q.db"
if db.exists(): db.unlink()
env = dict(os.environ, NS2_DB=str(db), NS2_ROOT=str(tmp), NS2_KEY="local-evidence-key", PYTHONUTF8="1")
api = subprocess.Popen([sys.executable, "-m", "uvicorn", "ns2.api:app", "--host", "127.0.0.1", "--port", "18977", "--log-level", "warning"], cwd=repo, env=env)
try:
    for _ in range(50):
        try: urllib.request.urlopen("http://127.0.0.1:18977/api/health", timeout=2); break
        except Exception: time.sleep(0.3)
    def rows():
        c = sqlite3.connect(db); n = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]; ids = [r[0] for r in c.execute("SELECT id FROM jobs ORDER BY id")]; c.close(); return n, ids
    sys.path.insert(0, str(repo)); os.environ.update(NS2_KEY="local-evidence-key", NS2_API="http://127.0.0.1:18977")
    from ns2 import step_script as SS
    from ns2.enqueue_video import post
    import hashlib
    sha = hashlib.sha256(md.read_text(encoding="utf-8").encode()).hexdigest()[:16]
    for k in (1, 2):
        out = subprocess.run([sys.executable, "-m", "ns2.enqueue_episode", "--api", "http://127.0.0.1:18977", "--run", "series10_v2", "--ep", "5", "--md", str(md), "--version", "v3"],
                             cwd=repo, env=env, capture_output=True, text=True)
        print(f"== 第 {k} 次 enqueue_episode（剧本 sha {sha}）"); print(out.stdout.strip(), out.stderr.strip()[-300:])
        n = {"new": 0, "existing": 0}
        for r in SS.shot_rows("series10_v2", 5, list(range(1, 14)), sha):
            n["new" if post("http://127.0.0.1:18977", "/api/jobs", r)["created"] else "existing"] += 1
        cnt, ids = rows()
        print(f"   镜头行入队（step_script.shot_rows，13 镜 × 4 步 + assets 1 行）：{n}；库里总行数 {cnt}；id 范围 {ids[0]}–{ids[-1]}")
finally:
    api.terminate()
