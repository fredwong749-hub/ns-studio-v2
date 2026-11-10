"""R4 NS2_SPLIT_IMAGES：出图两台分担 + 资产包并集同步"""
import hashlib
import json

import pytest


def test_images_host_split(monkeypatch):
    from ns2 import migrate as M
    monkeypatch.delenv("NS2_SPLIT_IMAGES", raising=False)
    assert [M.images_host(7, f"ep07-S{i}") for i in (1, 2, 3)] == ["gpu33"] * 3          # 关着 = 本集出图机
    monkeypatch.setenv("NS2_SPLIT_IMAGES", "1")
    hosts = [M.images_host(7, f"ep07-S{i}") for i in range(1, 17)]
    assert hosts.count("gpu36") == 10 and hosts.count("gpu33") == 6                      # 3:2 按道数
    assert M.images_host(7) == "gpu33" and M.images_host(7, "ep07") == "gpu33"           # 集级行（资产）仍在本集出图机


def test_shot_rows_follow_split(monkeypatch):
    from ns2.step_script import shot_rows
    monkeypatch.setenv("NS2_SPLIT_IMAGES", "1")
    rows = shot_rows("r", 7, [1, 2], "sha")
    by = {(r["shot"], r["step"]): r["want_host"] for r in rows}
    assert by[("ep07", "assets")] == "gpu33"
    assert by[("ep07-S1", "board")] == by[("ep07-S1", "first")] == by[("ep07-S1", "last")] == "gpu36"
    assert by[("ep07-S2", "first")] == "gpu33" and by[("ep07-S1", "video")] is None


def _mf(tmp, data, files):
    (tmp / "assets_v2").mkdir(parents=True, exist_ok=True)
    (tmp / "assets_v2/manifest.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    for rel, b in files.items():
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b)


def test_asset_sync_union_no_overwrite(tmp_path):
    from ns2 import asset_sync as AS
    a, b, c = b"plate-a", b"plate-b", b"plate-c"
    sha = lambda x: hashlib.sha256(x).hexdigest()                         # noqa: E731
    local = {"scenes": {"s1": {"base": {"file": "assets_v2/scenes/s1.png", "sha256": sha(a)}}},
             "characters": {"c1": {"looks": {"B": {"sheet": "x"}}}}, "updated_at": "1"}
    remote = {"scenes": {"s1": {"base": {"file": "assets_v2/scenes/s1.png", "sha256": sha(a)}},
                         "s2": {"base": {"file": "assets_v2/scenes/s2.png", "sha256": sha(b)}},
                         "s3": {"base": {"file": "assets_v2/scenes/s3.png", "sha256": "bad"}}},
              "characters": {"c1": {"looks": {"B": {"sheet": "y", "items": {"i1": 1}}}}}, "updated_at": "2"}
    _mf(tmp_path, local, {"assets_v2/scenes/s1.png": a})
    store = {"manifest.json": json.dumps(remote).encode(), "scenes/s2.png": b, "scenes/s3.png": c}
    r = AS.pull(tmp_path, "gpu33", fetch=lambda rel: store[rel])
    assert r["pulled"] == ["assets_v2/scenes/s2.png"] and r["bad_sha"] == ["assets_v2/scenes/s3.png"]
    assert r["conflicts"] == ["/characters/c1/looks/B/sheet"] and r["changed"]
    m = json.loads((tmp_path / "assets_v2/manifest.json").read_text(encoding="utf-8"))
    assert m["characters"]["c1"]["looks"]["B"] == {"sheet": "x", "items": {"i1": 1}}       # 冲突保留本机，新增并进来
    assert (tmp_path / "assets_v2/scenes/s2.png").read_bytes() == b and not (tmp_path / "assets_v2/scenes/s3.png").exists()
    assert list(tmp_path.glob("assets_v2/manifest.json.bak_ns2sync_*"))
    r2 = AS.pull(tmp_path, "gpu33", fetch=lambda rel: store[rel])                           # 再同步：没新东西
    assert not r2["changed"] and r2["pulled"] == []


def test_files_port_serves_assets(tmp_path, monkeypatch):
    import threading
    import urllib.error
    import urllib.request
    from ns2 import auth as AU
    from ns2 import worker as W
    (tmp_path / "data").mkdir()
    _mf(tmp_path, {"a": 1}, {"assets_v2/scenes/p.png": b"PNG", "assets_v2/secret.key": b"k"})
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    W.serve_files("127.0.0.1", port, tmp_path)
    get = lambda p: urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}{p}", headers=AU.headers()), timeout=5).read()  # noqa: E731
    assert get("/assets/scenes/p.png") == b"PNG" and json.loads(get("/assets/manifest.json")) == {"a": 1}
    for bad, code in (("/assets/secret.key", 403), ("/assets/../data/x.png", 400)):
        with pytest.raises(urllib.error.HTTPError) as e:
            get(bad)
        assert e.value.code in (code, 404)
