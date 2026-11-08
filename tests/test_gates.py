from ns2 import gates as G


class VQ:
    def __init__(self, ok=True, boom=False):
        self.ok, self.boom = ok, boom

    def q9_passed(self, pf):
        if self.boom:
            raise RuntimeError("坏分数")
        return self.ok


def test_frame_passed_paths(monkeypatch):
    """gates 全路径（审查：gates.py 零测试）；M11：看图模块载不进 / 异常 = infra 原因，仍判不过"""
    t = {"postflight": {"ok": True}}
    monkeypatch.setattr(G, "_vq", lambda: VQ(True))
    assert G.frame_passed("image", t) == (True, "过")
    assert G.frame_passed("image", None)[0] is False
    assert G.frame_passed("image", {**t, "master_reject": {"x": 1}}) == (False, "项目方驳回")
    assert G.frame_passed("image", {"postflight": {"ok": False}})[1] == "postflight 未过"
    assert G.frame_passed("last", {"postflight": {"ok": False}}, locked=True) == (True, "定稿锁")
    monkeypatch.setattr(G, "_vq", lambda: VQ(False))
    assert "Q9 看图未过" in G.frame_passed("image", t)[1]
    monkeypatch.setattr(G, "_vq", lambda: None)
    assert G.frame_passed("image", t) == (False, "Q9 无法判定（看图模块载不进，infra）")
    monkeypatch.setattr(G, "_vq", lambda: VQ(boom=True))
    ok, why = G.frame_passed("image", t)
    assert ok is False and "infra" in why
