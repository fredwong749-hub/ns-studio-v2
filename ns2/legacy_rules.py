"""P6：对旧规则的统一修正（只改本进程里的函数引用，不改旧文件）。board / first·last / assets 三个旧执行器启动时都调 apply()，口径一致。

1. K-REN-708 口径对齐（P6 R0 ep05-S8 发现）：编译用 prompt_template_v2.hidden_people 判“在画但不露脸”（只以…入画 / 只露手 / 被遮），
   给提示词加“不露脸”；预检用 asset_refs.resolve_shot，只认背影 / 过肩前景为不露脸 → 同一个人一边写“不露脸”一边按露脸挂身份脸，I2 判矛盾卡死。
   修：asset_refs.back_view 也认 hidden_people 里写明了露出部位的人（=不送正脸），两边同一判定。
2. K-OPS-F23 派生参考图（拼脸板 / 设定图裁片）在服务器上直接生成并记账：旧规矩只许笔记本生成再按 sha 推送，前提“笔记本为源”P6 已不成立
   （资产在服务器本机生成，P6 R1 一份数据）。不然新剧本每镜都卡 DerivedRefMissing。
3. K-OPS-F03 refs 表核对（producer.refs_guard）：防的是“笔记本生成、推服务器”两边不一致，前提同上已不成立 → 一律放行（board 与首尾帧同口径）。
4. K-REN-950 × K-REN-708（ep05-S7/S8 卓琳“局部｜手”）：局部人物在以人为主的景别里，K-REN-950 为防陌生脸按“可能露脸”兜底送身份脸
   （face_described），编译同时给她写“只露出手，不露脸”；I2 矛盾检查把两者判成自相矛盾卡死。两者其实一致（要求不露脸 + 万一露了也是本人）
   → 只为兜底送脸的人（face_conflict）不算“露脸人物”参与矛盾检查；送脸照旧。
5. 参考板不印字（_no_text_boards）；6. 尽力版当前不被重打分清空（_best_effort_rule）；
7. 中景不补看不见的鞋（_mid_no_feet，NS2_MID_NO_FEET=1，默认关）；8. 排排站几何判不算两人转向相反（_lineup_facing，NS2_LINEUP_FACING=1，默认关）。
"""
from __future__ import annotations

_APPLIED = False
BOARD_ORDER_NOTE = "（板上不写字，格子按从左到右、从上到下的顺序对应上面的编号；画面里不要出现任何文字、编号或标签）"


def board_no_text(AR, project: str, tiles: list) -> str:
    """无字参考板（P6 R4，规则 5）：同旧 make_board 的网格 / 缩放 / 缓存，只是不画底部标签，文件名加 nt_ 与旧板区分"""
    import hashlib
    import json
    from PIL import Image
    bc = AR._cfg().get("board") or {}
    T = int(bc.get("tile", 512))
    key = hashlib.sha1(json.dumps([[f, l, AR.sha_file(AR.ROOT / f)] for f, l in tiles], ensure_ascii=False).encode()).hexdigest()[:12]
    sub = AR.project_cfg(project).get("board_dir", "data/refboards")
    out, _w = AR.derived_target(sub, f"board_nt_{key}.png")
    if out.is_file():
        return AR.rel(out)
    n = len(tiles)
    cols = n if n <= 2 else (2 if n <= 4 else 3)
    rows = (n + cols - 1) // cols
    board = Image.new("RGB", (cols * T, rows * T), "white")
    for i, (f, _label) in enumerate(tiles):
        im = Image.open(AR.ROOT / f).convert("RGB")
        im.thumbnail((T - 12, T - 12))
        x, y = (i % cols) * T, (i // cols) * T
        board.paste(im, (x + (T - im.width) // 2, y + (T - im.height) // 2))
    out.parent.mkdir(parents=True, exist_ok=True)
    board.save(out)
    AR.record_derived(out, project)
    return AR.rel(out)


def _best_effort_rule() -> None:
    """6. auto_current 规则 4（项目方 11-09“尽力版一律自动用”，审查 3 §5b 对账）：当前版是 ns2 打过 best_effort 标记的尽力版时，
    这一版被重打分仍不过 → 不清空当前（原规则 2 会“清空当前”，把已自动用的尽力版悄悄撤掉）。有过契约的版本时照常换上 / 退回（更好的版本优先）"""
    try:
        from ns_render import auto_current as AC
    except ImportError:
        return
    if getattr(AC.decide, "_ns2_rule4", False):
        return
    orig = AC.decide

    def decide(data, stage, n):
        action, target, why = orig(data, stage, n)
        be = (data.get("best_effort") or {}).get(stage) or {}
        active = (data.get("active") or {}).get(stage)
        if action == "clear" and be.get("n") is not None and be.get("n") == active:
            return "keep", active, why + f"；但当前 v{active} 是尽力版自动用（项目方 11-09，规则 4），不清空"
        return action, target, why

    decide._ns2_rule4 = True
    AC.decide = decide


MID_SIZES = ("中景",)


def _mid_no_feet(FR, on: bool | None = None) -> bool:
    """7. 中景不补看不见的鞋（R4 质量 4，11-10；开关 NS2_MID_NO_FEET=1，默认关）：
    旧 frame_job.outer_items 的“本镜造型要穿全：…（正文没写也必须入画）”只对特写 / 近景 / 中近景（framing.CLOSE_NO_LOWER，K-REN-707）
    裁掉景别外的单品；中景（膝盖以上，framing.HIDE 已把脚列为看不见）没裁，一体件“抹胸短裙 + 丝袜 + 透明尖头细高跟”整句补进去，
    模型为画出鞋把中景拉成全身。两台历史 takes 回放：中景带鞋句 146 张远两档以上 22%、不带 322 张 3%（按同框人数分层仍成立）。
    修：CLOSE_NO_LOWER 加上中景——同一套 K-REN-707 裁剪（visible_garment / visible_items 按 HIDE[中景] = 脚，只删鞋、留裙与丝袜）。
    旧代码里 CLOSE_NO_LOWER 只有 outer_items 用。返回是否生效"""
    if on is None:
        import os
        on = os.environ.get("NS2_MID_NO_FEET") == "1"
    cur = tuple(getattr(FR, "CLOSE_NO_LOWER", ()) or ()) if FR is not None else ()
    if not on or not cur:
        return False
    FR.CLOSE_NO_LOWER = cur + tuple(s for s in MID_SIZES if s not in cur)
    return True


FACING_DYAW = 0.5


def facing_pair(ya, yb, dyaw: float = FACING_DYAW) -> bool:
    """两张脸头部朝向一左一右（yaw_n 异号）且相差 ≥ dyaw → 互相看 / 各看一边，不是“正面并排合影”"""
    if ya is None or yb is None:
        return False
    ya, yb = float(ya), float(yb)
    return ya * yb < 0 and abs(ya - yb) >= dyaw


def lineup_drop_facing(res: dict, rows: list) -> dict:
    """lineup_geo 结果去掉转向相反的对，人数 / 结论重算（min_faces 用原阈值）"""
    pairs = [p for p in res.get("pairs") or []
             if not (max(p) < len(rows) and facing_pair((rows[p[0]] or {}).get("yaw_n"), (rows[p[1]] or {}).get("yaw_n")))]
    if len(pairs) == len(res.get("pairs") or []):
        return res
    members = {i for p in pairs for i in p}
    mn = int((res.get("thr") or {}).get("min_faces", 2))
    return {**res, "pairs": pairs, "n": len(members), "lineup": bool(pairs) and len(members) >= mn,
            "facing_dropped": [p for p in res.get("pairs") or [] if p not in pairs]}


def _lineup_facing(GZ=None, on: bool | None = None) -> bool:
    """8. 排排站几何判去掉“两人转向相反”的对（R4 质量 5，11-10；开关 NS2_LINEUP_FACING=1，默认关）：
    ns_qc.gaze.LINEUP 的 yaw 上限 0.6（|yaw_n| ≥ 0.25 已是侧脸），只看绝对值——两人互相看（左人朝右、右人朝左）也被判成
    “正面并排合影”，G1 硬项挡掉。两台历史 104 张被判排排站的图逐张看：20 张 yaw 异号且相差 ≥ 0.5，20/20 是对视 / 一人低头看物 /
    各看一边，不是合影；其余 84 张（同号小 yaw）保持原判。只改排排站，看镜头（gaze_x）照旧判"""
    if on is None:
        import os
        on = os.environ.get("NS2_LINEUP_FACING") == "1"
    if not on:
        return False
    if GZ is None:
        try:
            import sys
            from pathlib import Path
            qc = str(Path.cwd() / "packages" / "qc")
            if qc not in sys.path:
                sys.path.insert(0, qc)
            from ns_qc import gaze as GZ
        except Exception:  # noqa: BLE001  旧树没有 ns_qc → 不动
            return False
    if getattr(GZ.lineup_geo, "_ns2_facing", False):
        return True
    orig = GZ.lineup_geo

    def lineup_geo(rows, W, H, thr=None):
        return lineup_drop_facing(orig(rows, W, H, thr), list(rows or []))

    lineup_geo._ns2_facing = True
    GZ.lineup_geo = lineup_geo
    return True


def _no_text_boards(AR) -> None:
    """5. 参考板标签进画面（P6 R1a ep05-S5/S7/S11 Q6：“①祁念；层勘”“③服装：日常全壹装…”被模型照画）：
    旧参考板每格底下印中文标签，出图模型把字当画面内容抄进去。修：参考板不印字；对应关系改在提示词里按格子顺序说明"""
    orig_board, orig_line = AR.make_board, AR._line

    def make_board(project, tiles):
        try:
            return board_no_text(AR, project, tiles)
        except Exception as e:  # noqa: BLE001  旧树里辅助函数对不上 → 退回旧板（带字），打印出来
            import sys
            print(f"[legacy_rules] 无字参考板失败，退回旧板：{type(e).__name__}: {e}", file=sys.stderr)
            return orig_board(project, tiles)

    def _line(project, s):
        line = orig_line(project, s)
        if getattr(s, "kind", None) in ("board", "faceboard") and "board_nt_" in str(getattr(s, "file", "")):
            line = line.replace("——", BOARD_ORDER_NOTE + "——", 1)
        return line

    AR.make_board, AR._line = make_board, _line


def apply() -> None:
    global _APPLIED
    if _APPLIED:
        return
    from ns_agent import asset_refs as AR
    from ns_agent import prompt_template_v2 as PT
    orig = AR.back_view
    busy = {"on": False}

    def back_view(shot, name):
        if orig(shot, name):
            return True
        if busy["on"]:                     # hidden_people 内部也调 back_view：防递归，内部按原判定
            return False
        busy["on"] = True
        try:
            part = PT.hidden_people(shot).get(name)
        except Exception:  # noqa: BLE001  判不出来 = 按原规则
            part = None
        finally:
            busy["on"] = False
        return bool(part) and part != "肩与背"     # 只认写明了露出部位的（只露手 / 只以右肩入画…），不扩大到“没写脸”的一般情况

    AR.back_view = back_view
    AR._on_server = lambda: False

    orig_rs = AR.resolve_shot

    def resolve_shot(*a, **k):
        r = orig_rs(*a, **k)
        for c in getattr(r, "characters", None) or []:
            if isinstance(c, dict) and c.get("face_conflict") and c.get("presence") == "局部" and c.get("face_described"):
                c["face_described"] = False          # 身份脸已在 refs 里（兜底送脸照旧），只是不当“露脸人物”判矛盾 / 要脸
                c["face_guard_only"] = True
        return r

    AR.resolve_shot = resolve_shot
    _no_text_boards(AR)
    _best_effort_rule()
    _mid_no_feet(getattr(AR, "FR", None))
    _lineup_facing()
    try:
        import producer as PR                       # 旧派单器模块（board / 首尾帧执行器都会 import 它）
        PR.refs_guard = lambda *a, **k: []
    except ImportError:
        pass
    _APPLIED = True
