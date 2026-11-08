"""当前剧的人物表（OVERSIGHT-F P1-5b，PROMPTFIX 11-02 20:3x）：名字 → 角色 id、女性角色、女主——同一个事实只留一张表。

来源（按剧）：asset_refs.NAME_ID（V1 四个名字）+ 资产包清单的角色（display_name / aliases / 默认称呼，asset_refs.register_manifest_names）；
女性 = config/recipes/asset_refs.json 该项目 female 列表；女主 = 该项目 lead（缺省：female 列表第一个）。
frame_spec / gpu3_fields 不再写死 lin_wan / 林晚：V2 编译“她”的镜头解析成 lead_f。
项目 = 显式传入，或当前剧（ns_agent.run_paths.current_run：NS_RUN / runs.json primary）；当前剧没有资产项目配置 → 退回 V1 旧表
（沙箱 / 旧测试），V2（资产包模式）绝不退回。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def current_project(project: str | None = None) -> str | None:
    from . import asset_refs as AR
    from . import run_paths as RP
    p = project or RP.current_run(ROOT)
    return p if p and AR.project_cfg(p) else None


def char_ids(project: str | None = None) -> dict[str, str]:
    from . import asset_refs as AR
    p = current_project(project)
    if p and AR.manifest_rel(p):
        AR.register_manifest_names(p)
        ids = set((AR.manifest(p) or {}).get("characters") or {})
        return {n: c for n, c in AR.NAME_ID.items() if c in ids}       # 资产包模式：只认清单里的角色
    return dict(AR.NAME_ID)


def female_ids(project: str | None = None) -> set[str]:
    from . import asset_refs as AR
    p = current_project(project)
    fem = set(AR.project_cfg(p).get("female") or []) if p else set()
    return fem or ({"lin_wan"} if not (p and AR.manifest_rel(p)) else set())


def lead_id(project: str | None = None) -> str | None:
    from . import asset_refs as AR
    p = current_project(project)
    if p:
        pc = AR.project_cfg(p)
        if pc.get("lead"):
            return pc["lead"]
        if pc.get("female"):
            return pc["female"][0]
        if AR.manifest_rel(p):
            return None
    return "lin_wan"


def who_ids(project: str | None = None) -> dict[str, str]:
    """视线落点主语：名字 → id，“她” → 女主"""
    out = dict(char_ids(project))
    lead = lead_id(project)
    if lead:
        out["她"] = lead
    return out


def speaker_cast(project: str | None = None) -> dict | None:
    """配音的说话人表（STYLE-V2 11-02，总调度：V2 说话人归属统一走本文件，不另起表）。
    资产包模式的剧：本剧 run_dir/P1_storyline.json 的 characters（名字 → char_id、性别）+ 清单角色名；
    → {"names": {名字: char_id}, "male": [char_id], "female": [char_id], "lead": char_id, "source": 路径}。
    legacy / 没有资产项目 → None（调用方用 V1 的说话人表）。资产包模式但人物表缺失 → RuntimeError（不退回 V1）。"""
    import json
    from . import asset_refs as AR
    p = current_project(project)
    if not (p and AR.manifest_rel(p)):
        return None
    run_dir = AR.project_cfg(p).get("run_dir")
    src = ROOT / str(run_dir or "") / "P1_storyline.json"
    if not run_dir or not src.is_file():
        raise RuntimeError(f"{p} 是资产包模式，但没有人物表 {src}（说话人归属不退回 V1）")
    chars = json.loads(src.read_text(encoding="utf-8")).get("characters") or []
    names, male, female = {}, [], []
    for c in chars:
        cid, name = c.get("char_id"), c.get("name")
        if not cid or not name:
            continue
        names[name] = cid
        (female if str(c.get("gender")) in ("女", "female", "F") else male).append(cid)
    for n, cid in char_ids(p).items():          # 清单里登记的别名（display_name / aliases）
        names.setdefault(n, cid)
    lead = lead_id(p)
    if not lead or lead not in names.values():
        raise RuntimeError(f"{p} 的女主 {lead!r} 不在人物表 {src.relative_to(ROOT).as_posix()}")
    return {"names": names, "male": male, "female": female, "lead": lead, "source": src.relative_to(ROOT).as_posix()}
