"""B1（外部审查 11-08）：共享密钥头 X-NS2-Key。API 除 /api/health 外全部要；worker 文件口全部要。

密钥只在机器上（/root/work/fredw/ns2/secret.key，600），笔记本桥一份（不进任何仓库）；环境变量 NS2_KEY 优先（测试用）。
读不到密钥 = 全部拒绝（fail-closed），不会退回无鉴权。
"""
from __future__ import annotations

import hmac
import os
from pathlib import Path

HEADER = "X-NS2-Key"
KEY_FILE = os.environ.get("NS2_KEY_FILE", "/root/work/fredw/ns2/secret.key")


def load_key() -> str | None:
    k = os.environ.get("NS2_KEY")
    if k:
        return k.strip()
    try:
        return Path(KEY_FILE).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def ok(given: str | None, key: str | None = None) -> bool:
    key = key if key is not None else load_key()
    return bool(key) and bool(given) and hmac.compare_digest(given.encode(), key.encode())


def headers() -> dict:
    k = load_key()
    return {HEADER: k} if k else {}
