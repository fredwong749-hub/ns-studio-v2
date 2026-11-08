"""P6：对旧规则的统一修正（只改本进程里的函数引用，不改旧文件）。board / first·last / assets 三个旧执行器启动时都调 apply()，口径一致。

1. K-REN-708 口径对齐（P6 R0 ep05-S8 发现）：编译用 prompt_template_v2.hidden_people 判“在画但不露脸”（只以…入画 / 只露手 / 被遮），
   给提示词加“不露脸”；预检用 asset_refs.resolve_shot，只认背影 / 过肩前景为不露脸 → 同一个人一边写“不露脸”一边按露脸挂身份脸，I2 判矛盾卡死。
   修：asset_refs.back_view 也认 hidden_people 里写明了露出部位的人（=不送正脸），两边同一判定。
2. K-OPS-F23 派生参考图（拼脸板 / 设定图裁片）在服务器上直接生成并记账：旧规矩只许笔记本生成再按 sha 推送，前提“笔记本为源”P6 已不成立
   （资产在服务器本机生成，P6 R1 一份数据）。不然新剧本每镜都卡 DerivedRefMissing。
"""
from __future__ import annotations

_APPLIED = False


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
    _APPLIED = True
