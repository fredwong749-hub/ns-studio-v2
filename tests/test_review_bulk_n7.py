"""审查 3 N7 / N8 / 批量成人检查"""
from pathlib import Path


def test_bulk_does_not_overwrite_rejected_and_refuses_untriggered_adult(tmp_path, api_mod):
    from conftest import write_script
    from fastapi.testclient import TestClient
    from ns2 import db as D
    write_script(tmp_path, "r", adult_shots=("ep01-S3",))
    c = TestClient(api_mod.app, headers={"X-NS2-Key": "test-key"})
    k = api_mod.conn()
    for sh in ("ep01-S1", "ep01-S2", "ep01-S3"):
        D.enqueue(k, run_id="r", ep=1, shot=sh, step="video", state="done", result={"has_output": True})
    c.put("/api/shots/ep01-S2/review", json={"run_id": "r", "state": "rejected", "reason": "手指多一根"})
    r = c.post("/api/reviews/bulk", json={"run_id": "r", "shots": ["ep01-S1", "ep01-S2", "ep01-S3"]}).json()
    assert r["done"] == ["ep01-S1"]
    assert "已驳回" in r["skipped"]["ep01-S2"] and r["rejected"] == {"ep01-S2": "手指多一根"}     # N7：不静默覆盖，列出原因
    assert "成人镜" in r["skipped"]["ep01-S3"]                                                     # 显式成人检查（视频行是 done 也不放）
    assert D.reviews(k, "r")["ep01-S2"]["state"] == "rejected"
    r2 = c.post("/api/reviews/bulk", json={"run_id": "r", "shots": ["ep01-S2"], "confirm_rejected": ["ep01-S2"]}).json()
    assert r2["done"] == ["ep01-S2"] and "覆盖了之前的驳回" in D.reviews(k, "r")["ep01-S2"]["reason"]   # 逐镜确认才改
    c.post("/api/shots/ep01-S3/trigger", json={"run_id": "r"})
    assert c.post("/api/reviews/bulk", json={"run_id": "r", "shots": ["ep01-S3"]}).json()["done"] == ["ep01-S3"]   # 触发过的成人镜可以


def test_unreview_available_and_copy_matches_ui(tmp_path, api_mod):
    """N8：确认文案承诺的“撤销定稿”按钮真的存在，服务端收 unreviewed"""
    from conftest import write_script
    from fastapi.testclient import TestClient
    write_script(tmp_path, "r")
    c = TestClient(api_mod.app, headers={"X-NS2-Key": "test-key"})
    c.put("/api/shots/ep01-S1/review", json={"run_id": "r", "state": "passed"})
    assert c.put("/api/shots/ep01-S1/review", json={"run_id": "r", "state": "unreviewed"}).json()["state"] == "unreviewed"
    root = Path(__file__).resolve().parents[1] / "frontend"
    html, rjs, ajs = ((root / f).read_text(encoding="utf-8") for f in ("review.html", "review.js", "app.js"))
    assert 'id="btn-unreview"' in html and "state: 'unreviewed'" in rjs
    assert "撤销定稿" in ajs and "点“修改”" not in ajs
    assert "shot.review !== 'rejected'" in ajs                                                     # 一键候选不含已驳回
