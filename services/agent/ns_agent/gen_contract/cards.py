"""按剧的 Q1a 人物卡与 Q1 主角线（ISS-1103-024206-c8c0 / CARRYOVER 第 10 项，MERGE-IMG-O 11-03，L-137）。

问题：config/gen_contract.json Q1a_attr.cards 只有 V1 三个人（lin_wan / gu_chen / chen_yu），V2 四个角色没有卡 →
postflight.q1a_item 的 todo 恒空 → Q1a 恒 na（顾沉长蓝发这类错在 V2 不拦）；Q1 的“主角 0.70”写死在 lin_wan 键上，V2 主角 lead_f 退到 default。

规则：
- 旧剧（不是资产包模式）：照旧用 config 里的 cards（V1 内容只留在 V1）。
- 资产包模式的剧（V2 起）：人物卡从本剧 P1 人物表（run_dir/P1_storyline.json 的 characters：char_id / gender / zh_look）自动生成，
  只收资产包清单（manifest characters）里有的角色；config Q1a_attr.cards_by_run.<剧>.<cid> 可逐项覆盖（人工校准）。
  阈值（blue_min 0.20 / blue_max 0.45 / below_max 0.15 / min_hair_px 0.15）沿用 V1 标定（GATE_CALIBRATION §18），换剧不改口径。
- 主角线：Q1_identity.<cid> 优先；没有时，cid 是本剧主角（cast_table.lead_id，V2 = asset_refs.json lead）→ Q1_identity.lead；再没有 → default。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from . import ROOT

# 发色词 → 看图答案集合（看图答“黑 / 棕”在冷光下常互换；雾霾蓝看图可能答灰银——V1 林晚两种都收，同一口径）
COLOR_WORDS = (("蓝", ["蓝", "灰银"]), ("灰银", ["灰银"]), ("银", ["灰银"]), ("白", ["灰银"]), ("金", ["金"]), ("亚麻", ["金"]),
               ("棕", ["棕", "黑"]), ("栗", ["棕", "黑"]), ("褐", ["棕", "黑"]), ("黑", ["黑", "棕"]),
               ("红", ["其它"]), ("粉", ["其它"]), ("紫", ["其它"]), ("绿", ["其它"]))
SHORT_WORDS = ("寸头", "板寸", "平头", "短发", "光头", "短碎发")
LONG_WORDS = ("及腰", "长直发", "长卷发", "长发", "过肩")
SHOULDER_WORDS = ("齐肩", "及肩", "中长", "肩")
TIED_WORDS = ("扎", "盘", "梳到脑后", "丸子", "马尾", "背头")       # 扎起 / 梳到脑后：发长看图不可靠 → 不核发长、不按短发判


def hair_segment(zh_look: str) -> str:
    segs = [s for s in re.split(r"[｜|]", zh_look or "") if s.strip()]
    return next((s for s in segs if "发" in s or any(w in s for w in SHORT_WORDS + ("背头",))), "")


def card_from_p1(row: dict) -> dict | None:
    """P1 人物表一行 → 人物卡（与 V1 卡同一组字段：name / gender / hair_colors / hair_lengths / hair_short / below_max / blue_*）"""
    seg = hair_segment(row.get("zh_look") or "")
    if not seg:
        return None
    card: dict = {"name": row.get("name") or row.get("char_id"), "_auto": "P1 zh_look（cards.card_from_p1）"}
    if row.get("gender") in ("男", "女"):
        card["gender"] = row["gender"]
    hits = sorted((seg.find(w), w, ans) for w, ans in COLOR_WORDS if w in seg)
    if hits:
        card["hair_colors"] = list(hits[0][2])
    tied = any(w in seg for w in TIED_WORDS)
    if any(w in seg for w in SHORT_WORDS):
        card.update(hair_lengths=["短"], hair_short=True, below_max=0.15)
    elif not tied and any(w in seg for w in LONG_WORDS):
        card["hair_lengths"] = ["长", "及肩"]
    elif not tied and any(w in seg for w in SHOULDER_WORDS):
        card["hair_lengths"] = ["及肩", "长"]
    if "蓝" in (card.get("hair_colors") or []):
        card.update(blue_min=0.2, min_hair_px=0.15)
    else:
        card["blue_max"] = 0.45
    return card


def _p1_rows(project: str) -> list[dict]:
    from ns_agent import asset_refs as AR
    rd = (AR.project_cfg(project) or {}).get("run_dir")
    if not rd:
        return []
    p = ROOT / rd / "P1_storyline.json"
    if not p.is_file():
        return []                     # 还没出 P1 = 没有人物行（显式默认）
    try:
        return list((json.loads(p.read_text(encoding="utf-8")) or {}).get("characters") or [])
    except (OSError, ValueError) as e:
        print(f"[gen_contract.cards] {p} 读不出：{e} → 按无人物行", file=sys.stderr)
        return []


def q1a_cards(project: str | None, t1a: dict) -> dict:
    """本剧的 Q1a 人物卡 {cid: card}"""
    from ns_agent import asset_refs as AR
    base = dict(t1a.get("cards") or {})
    if not project or not AR.manifest_rel(project):
        return base
    allowed = set(((AR.manifest(project) or {}).get("characters") or {}).keys())
    out: dict = {}
    for row in _p1_rows(project):
        cid = row.get("char_id")
        if not cid or (allowed and cid not in allowed):
            continue
        c = card_from_p1(row)
        if c:
            out[cid] = c
    for cid, ov in ((t1a.get("cards_by_run") or {}).get(project) or {}).items():
        out[cid] = {**out.get(cid, {}), **ov}
    return out


def identity_line(cid: str, t1: dict, project: str | None = None) -> float:
    """Q1 单人身份线：Q1_identity.<cid> > 本剧主角 → Q1_identity.lead > default"""
    if cid in t1 and isinstance(t1[cid], (int, float)):
        return float(t1[cid])
    from ns_agent.cast_table import lead_id
    if "lead" in t1 and cid == lead_id(project):
        return float(t1["lead"])
    return float(t1.get("default", 0.6))


def is_lead_listed(cid: str, chars: list, project: str | None = None) -> bool:
    """yaw_relax.characters 这类名单：写 "@lead" = 本剧主角"""
    if cid in (chars or []):
        return True
    if "@lead" in (chars or []):
        from ns_agent.cast_table import lead_id
        return cid == lead_id(project)
    return False
