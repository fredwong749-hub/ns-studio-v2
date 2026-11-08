import json
import os

import pytest

os.environ.setdefault("NS2_KEY", "test-key")      # B1：测试用密钥（真机读 /root/work/fredw/ns2/secret.key）


def write_script(root, run, adult_shots=(), eps=((1, 3), (2, 3), (3, 3))):
    """B2 测试夹具：NS2_ROOT 下放一份导入稿，adult_shots 里的镜标成人"""
    f = root / "data/import/runs" / run / "script_v2.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"episodes": [{"number": ep, "shots": [{"index": i, "adult": f"ep{ep:02d}-S{i}" in adult_shots}
                                                                   for i in range(1, n + 1)]} for ep, n in eps]}), encoding="utf-8")
    return f


@pytest.fixture
def api_mod(tmp_path, monkeypatch):
    """每个用例一份全新 ns2.api（独立 DB + 独立 NS2_ROOT）"""
    import importlib
    monkeypatch.setenv("NS2_DB", str(tmp_path / "q.db"))
    monkeypatch.setenv("NS2_ROOT", str(tmp_path))
    import ns2.api as A
    importlib.reload(A)
    return A
