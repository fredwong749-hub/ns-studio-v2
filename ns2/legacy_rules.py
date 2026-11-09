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
    try:
        import producer as PR                       # 旧派单器模块（board / 首尾帧执行器都会 import 它）
        PR.refs_guard = lambda *a, **k: []
    except ImportError:
        pass
    _APPLIED = True
