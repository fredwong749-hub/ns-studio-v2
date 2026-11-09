"""B1（外部审查 11-08）：共享密钥头 X-NS2-Key。API 除 /api/health 外全部要；worker 文件口全部要。

密钥只在机器上（/root/work/fredw/ns2/secret.key，600），笔记本桥一份（不进任何仓库）；环境变量 NS2_KEY 优先（测试用）。
读不到密钥 = 全部拒绝（fail-closed），不会退回无鉴权。
"""
from __future__ import annotations

import hmac
import os
from pathlib import Path

HEADER = "X-NS2-Key"
# B1 / M12：API 代理与 worker 文件口共用的白名单——只给图 / 片 / JSON，queue.db / 密钥 / 日志一律 403
MEDIA_EXT = frozenset({".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm", ".mov", ".json"})
KEY_FILE = os.environ.get("NS2_KEY_FILE", "/root/work/fredw/ns2/secret.key")


def _key_lines() -> list[tuple[str, str | None]]:
    """密钥文件每行一把：`<密钥> [到期日 YYYY-MM-DD]`。轮换（审查 3 B1）：先加新行 → 各端换新 → 删旧行；到期日过了的行自动不认"""
    k = os.environ.get("NS2_KEY")
    if k:
        return [(k.strip(), None)]
    try:
        text = Path(KEY_FILE).read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for ln in text.splitlines():
        parts = ln.split()
        if parts and not parts[0].startswith("#"):
            out.append((parts[0], parts[1] if len(parts) > 1 else None))
    return out


def valid_keys(today: str | None = None) -> list[str]:
    import time
    today = today or time.strftime("%Y-%m-%d")
    return [k for k, exp in _key_lines() if not exp or exp >= today]


def load_key() -> str | None:
    """本机发请求用的那把：第一条有效行（轮换时把新密钥放第一行）"""
    ks = valid_keys()
    return ks[0] if ks else None


def ok(given: str | None, key: str | None = None) -> bool:
    keys = [key] if key is not None else valid_keys()
    return bool(given) and any(k and hmac.compare_digest(given.encode(), k.encode()) for k in keys)


def headers() -> dict:
    k = load_key()
    return {HEADER: k} if k else {}
