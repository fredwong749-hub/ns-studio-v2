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


def test_p6_sanitize_sheet_spec_drops_text_props():
    """P6 11-09 根因：造型写“印着替补号码的衣袋”“（卓琳统一配）”→ 设定图画出号码字母，读字检查连毙 7 张。出设定图前去掉带字部件和剧情备注"""
    import importlib.util as u
    from pathlib import Path
    sp = u.spec_from_file_location("legacy_assets_t", Path(__file__).resolve().parents[1] / "ns2" / "legacy_assets.py")
    m = u.module_from_spec(sp)
    sp.loader.exec_module(m)
    spec = {"slots": {"服装": "白色缎面吊带长裙、印着替补号码的黑色衣袋挂在肩上", "鞋": "黑色漆皮细带露趾高跟凉鞋（卓琳统一配）", "饰品": "银色细手链"},
            "en": "a white satin slip gown with a black numbered garment bag over one shoulder, black patent leather strappy open-toe heeled sandals",
            "img": "穿白色缎面吊带长裙、印着替补号码的黑色衣袋挂在肩上、黑色漆皮细带露趾高跟凉鞋（卓琳统一配）、银色细手链，无文字无标识"}
    dropped = m.sanitize_spec(spec)
    assert spec["slots"] == {"服装": "白色缎面吊带长裙", "鞋": "黑色漆皮细带露趾高跟凉鞋", "饰品": "银色细手链"}
    assert "号码" not in spec["img"] and "统一配" not in spec["img"] and "numbered" not in spec["en"] and "slip gown" in spec["en"]
    assert len(dropped) == 3
    clean = {"slots": {"服装": "黑色修身西装"}, "en": "a black slim suit with a white shirt, a textured tie", "img": "穿黑色修身西装"}
    assert m.sanitize_spec(clean) == [] and clean["en"] == "a black slim suit with a white shirt, a textured tie"


def test_p6_board_no_text_and_order_note(tmp_path):
    """参考板不印字（Q6 根因）；提示词补格子顺序说明"""
    import types
    from PIL import Image
    from ns2 import legacy_rules as LR
    for i, c in enumerate(("red", "blue", "green")):
        Image.new("RGB", (300, 400), c).save(tmp_path / f"t{i}.png")
    rec = []
    AR = types.SimpleNamespace(
        ROOT=tmp_path, _cfg=lambda: {"board": {"tile": 128}}, sha_file=lambda p: p.name,
        project_cfg=lambda p: {"board_dir": "boards"},
        derived_target=lambda sub, name: (tmp_path / sub / name, True),
        record_derived=lambda out, project=None: rec.append(out.name), rel=lambda p: str(p.relative_to(tmp_path)).replace("\\", "/"))
    AR.make_board = lambda project, tiles: "old"
    AR._line = lambda project, s: "图1是脸部参考板：①甲②乙——每一格是对应人物的脸。"
    LR._no_text_boards(AR)
    f = AR.make_board("p", [(f"t{i}.png", f"标签{i}") for i in range(3)])
    assert f.startswith("boards/board_nt_") and rec
    im = Image.open(tmp_path / f)
    assert im.size == (256, 256)                         # 2×2 网格、没有标签条
    assert im.getpixel((128 + 64, 128 + 120)) == (255, 255, 255)   # 空格子纯白（没印字）
    s = types.SimpleNamespace(kind="faceboard", file=f)
    assert LR.BOARD_ORDER_NOTE in AR._line("p", s)


def test_mid_no_feet_rule(monkeypatch):
    """R4 质量 4：NS2_MID_NO_FEET=1 → 旧 framing.CLOSE_NO_LOWER 加上中景（outer_items 按 K-REN-707 裁掉看不见的鞋）；默认关不动"""
    import types
    from ns2 import legacy_rules as LR
    base = ("极近特写", "大特写", "特写", "中近景", "近景")
    FR = types.SimpleNamespace(CLOSE_NO_LOWER=base)
    monkeypatch.delenv("NS2_MID_NO_FEET", raising=False)
    assert LR._mid_no_feet(FR) is False and FR.CLOSE_NO_LOWER == base
    monkeypatch.setenv("NS2_MID_NO_FEET", "1")
    assert LR._mid_no_feet(FR) is True and FR.CLOSE_NO_LOWER == base + ("中景",)
    assert LR._mid_no_feet(FR) is True and FR.CLOSE_NO_LOWER.count("中景") == 1      # 重复调用不重复加
    assert LR._mid_no_feet(None) is False                                            # 旧树没有 framing → 不动


def test_lineup_facing_rule(monkeypatch):
    """R4 质量 5：NS2_LINEUP_FACING=1 → 排排站几何判去掉 yaw 异号且相差 ≥ 0.5 的对（对视），同号小 yaw 保持；默认关不动"""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "qc"))
    import types
    from ns_qc import gaze as real
    from ns2 import legacy_rules as LR
    GZ = types.SimpleNamespace(lineup_geo=real.lineup_geo)

    def row(x, yaw):
        return {"box": [x, 300, 100, 120], "iod": 40, "yaw_n": yaw, "status": "视线离轴"}
    facing = [row(200, 0.34), row(700, -0.45)]           # 左人朝右、右人朝左：对视（ep05-S5 v1 同款）
    side = [row(200, 0.13), row(700, 0.09)]              # 并排都朝镜头（ep05-S4 同款）
    assert real.lineup_geo(facing, 1000, 1800)["lineup"] and real.lineup_geo(side, 1000, 1800)["lineup"]
    monkeypatch.delenv("NS2_LINEUP_FACING", raising=False)
    assert LR._lineup_facing(GZ) is False and GZ.lineup_geo is real.lineup_geo
    monkeypatch.setenv("NS2_LINEUP_FACING", "1")
    assert LR._lineup_facing(GZ) is True
    r = GZ.lineup_geo(facing, 1000, 1800)
    assert r["lineup"] is False and r["facing_dropped"] == [[0, 1]]
    assert GZ.lineup_geo(side, 1000, 1800)["lineup"] is True
    assert LR._lineup_facing(GZ) is True and GZ.lineup_geo._ns2_facing        # 不重复包
    three = [row(100, -0.33), row(450, 0.78), row(800, 0.39)]                 # 三人：一对异号去掉，同号那对仍是排排站
    assert real.lineup_geo(three, 1000, 1800, {"yaw": 0.9})["lineup"] and GZ.lineup_geo(three, 1000, 1800, {"yaw": 0.9})["lineup"]
    assert LR.facing_pair(0.2, -0.2) is False and LR.facing_pair(None, 0.5) is False


def test_previz_fix_rules(monkeypatch):
    """R4 质量 6：摆位图宽度距离封顶（景别距离 × 1.15）；默认朝向只对多脸、没写明朝向的人改；开关默认关"""
    import types
    from ns2 import previz_fix as PF
    assert PF.subject_distance(1.94, 3.70) == round(1.94 * 1.15, 2)      # ep05-S1 中景 50mm 三张脸：3.70 → 2.23
    assert PF.subject_distance(1.94, 0) == 1.94 and PF.subject_distance(1.94, 2.0) == 2.0
    assert PF.explicit_stance("侧右站在镜台前") and not PF.explicit_stance("站在画面右侧前景")
    AB = types.SimpleNamespace(auto_blocking=lambda *a: "old", RENDER_VERSION=3)
    monkeypatch.delenv("NS2_PREVIZ_FIT", raising=False)
    assert PF.apply(AB) is False and AB.auto_blocking() == "old" and AB.RENDER_VERSION == 3
    assert PF.apply(AB, on=True) is True and AB.auto_blocking._ns2_fit and AB.RENDER_VERSION == "3+" + PF.RENDER_TAG
    assert PF.apply(AB, on=True) is True and AB.RENDER_VERSION == "3+" + PF.RENDER_TAG          # 不重复包
