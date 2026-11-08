"""生成契约（docs/GEN_CONTRACT.md 总调度定稿 v1）的代码实现。

    preflight.check(req)        → ContractResult（I1–I10 逐项：pass / fail / na + 证据），任何 fail = 拒绝提交
    postflight.evaluate(...)    → ContractResult（Q1–Q7 逐项量化；没测到的项 = untested，不算通过）
    report.markdown(...) / report.contact_sheet(...)  → “契约逐项结果表” + 接触印样
    style.image_suffix()        → 当前默认出图预设（徕卡 S★）的系统后缀（I7）

参数：config/gen_contract.json；规则：knowledge/rules/gen_contract.yaml（K-REV-230 起）。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
CFG = ROOT / "config" / "gen_contract.json"


def cfg() -> dict:
    try:
        return json.loads(CFG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@dataclass
class Item:
    id: str
    title: str
    status: str                  # pass | fail | na | untested
    detail: str = ""
    evidence: dict = field(default_factory=dict)


@dataclass
class ContractResult:
    stage: str                   # preflight | postflight
    subject: str                 # shot id / image path
    items: list[Item]
    contract_version: str = ""

    @property
    def ok(self) -> bool:
        return all(i.status in ("pass", "na") for i in self.items)

    @property
    def blocking(self) -> list[Item]:
        return [i for i in self.items if i.status not in ("pass", "na")]

    def reasons(self) -> list[str]:
        return [f"{i.id} {i.title}：{i.detail}" for i in self.blocking]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ok"] = self.ok
        d["blocking"] = [i.id for i in self.blocking]
        return d


class ContractViolation(RuntimeError):
    """preflight 不通过：拒绝提交"""

    def __init__(self, result: ContractResult):
        self.result = result
        super().__init__(f"{result.subject} 生成契约 preflight 不通过：" + "；".join(result.reasons()))


# 统一签名：from ns_agent.gen_contract import preflight, postflight; preflight("image", payload) → dict
# （子模块本身可调用：preflight.check / postflight.evaluate 照常可用）
from . import postflight, preflight  # noqa: E402,F401
