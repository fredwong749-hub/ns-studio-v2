"""fredw-92/UXF1：出图结果的“坏图”检查（UX 审查 #6）。

ep02-S1 的首帧 #5 是一张 64×112、256 字节的纯色测试图（pytest 假 ComfyUI 的输出，测试结束时后台任务还在跑，
写进了真数据目录），界面却把它当成“当前首帧”。从此任何首帧 / 尾帧版本：

- 短边 < MIN_SHORT_SIDE（256 px）
- 文件 < MIN_BYTES（10 KB）
- 尺寸与这一版请求的出图尺寸差 > SIZE_TOLERANCE（10 %）
- 打不开

→ 记 take["bad"] = 原因（“坏图：…”），不设为当前、不能定稿（pipeline._add_take / set_active、frame_edit.lock）。
画布显示红色“坏图”角标。视频版本不查（尺寸 / 大小另有规则）。

测试里假 ComfyUI 输出的都是 64×112 小图：tests/conftest.py 默认把 ENABLED 关掉，测这条规则的测试自己打开。
"""
from __future__ import annotations

from pathlib import Path

ENABLED = True
MIN_SHORT_SIDE = 256
MIN_BYTES = 10 * 1024
SIZE_TOLERANCE = 0.10
IMAGE_STAGES = ("image", "last")

_cache: dict[tuple, str | None] = {}


def expected_size(take: dict) -> tuple[int, int] | None:
    """这一版请求的出图尺寸（宽, 高）：画布出图参数 effective.rendered_size / width×height，定稿档 pass2_size。
    没有记录（旧版本、上传、改图沿用父图尺寸）→ None，不比尺寸"""
    p = take.get("params") or {}
    if not isinstance(p, dict):
        return None
    eff = p.get("effective") if isinstance(p.get("effective"), dict) else {}
    # LCA：有后处理（放大出图缩回 / 锐化，image_params.postprocess）时当前文件是 *_post.png，
    # 尺寸 = post.size（交付尺寸）；effective.rendered_size 是缩回前的放大原图尺寸，不拿来比
    post = p.get("post") if isinstance(p.get("post"), dict) else None
    if post and post.get("file") and post.get("file") == take.get("file"):
        v = post.get("size")
        if isinstance(v, (list, tuple)) and len(v) == 2 and all(isinstance(x, int) and x > 0 for x in v):
            return int(v[0]), int(v[1])
    for v in (eff.get("rendered_size"), p.get("pass2_size"), p.get("rendered_size")):
        if isinstance(v, (list, tuple)) and len(v) == 2:
            try:
                w, h = int(v[0]), int(v[1])
            except (TypeError, ValueError):
                continue
            if w > 0 and h > 0:
                return w, h
    try:
        w, h = int(eff.get("width") or 0), int(eff.get("height") or 0)
    except (TypeError, ValueError):
        return None
    return (w, h) if w > 0 and h > 0 else None


def accepted_sizes(take: dict) -> list[tuple[int, int]]:
    """FIX（ep02_iter P0 ⑤，chain_audit E7）：这一版合法的成品尺寸——后处理后尺寸（post.size）、交付尺寸
    （effective.width×height）、放大出图尺寸（rendered_size / pass2_size）都算；文件与其中任一致就不是“尺寸不符”。
    （S4 image v6：lanczos 1152×2016 → 768×1344 后拿成品和请求的 1152×2016 比，被误判坏图）"""
    p = take.get("params") or {}
    if not isinstance(p, dict):
        return []
    out: list[tuple[int, int]] = []

    def add(v):
        if isinstance(v, (list, tuple)) and len(v) == 2:
            try:
                w, h = int(v[0]), int(v[1])
            except (TypeError, ValueError):
                return
            if w > 0 and h > 0 and (w, h) not in out:
                out.append((w, h))
    post = p.get("post") if isinstance(p.get("post"), dict) else {}
    eff = p.get("effective") if isinstance(p.get("effective"), dict) else {}
    add(post.get("size"))
    add([eff.get("width"), eff.get("height")])
    for v in (eff.get("rendered_size"), p.get("pass2_size"), p.get("rendered_size"), post.get("rendered_size")):
        add(v)
    return out


def check_file(path: Path, expected: tuple[int, int] | None = None) -> str | None:
    """None = 正常；否则返回“坏图：原因”"""
    path = Path(path)
    try:
        st = path.stat()
    except OSError:
        return "坏图：文件不存在"
    key = (str(path), st.st_size, st.st_mtime_ns, expected)
    if key in _cache:
        return _cache[key]
    out = _check(path, st.st_size, expected)
    if len(_cache) > 4096:
        _cache.clear()
    _cache[key] = out
    return out


def _check(path: Path, size: int, expected: tuple[int, int] | None) -> str | None:
    try:
        from PIL import Image
        with Image.open(path) as im:
            w, h = im.size
    except Exception:  # noqa: BLE001
        return "坏图：打不开"
    why = []
    if min(w, h) < MIN_SHORT_SIDE:
        why.append(f"只有 {w}×{h} px（短边要 ≥ {MIN_SHORT_SIDE}）")
    if size < MIN_BYTES:
        why.append(f"文件只有 {size} 字节（要 ≥ {MIN_BYTES // 1024} KB）")
    if expected:
        ew, eh = expected
        if abs(w - ew) > ew * SIZE_TOLERANCE or abs(h - eh) > eh * SIZE_TOLERANCE:
            why.append(f"尺寸 {w}×{h} 与请求的 {ew}×{eh} 不符")
    return ("坏图：" + "；".join(why)) if why else None


def check_take(take: dict, data_root: Path) -> str | None:
    """一版首帧 / 尾帧是不是坏图（视频不查；ENABLED=False 时一律 None）"""
    if not ENABLED or take.get("stage") not in IMAGE_STAGES or not take.get("file"):
        return None
    if take.get("quarantined"):
        return take.get("bad") or "坏图：已隔离"
    path = Path(data_root) / take["file"]
    why = check_file(path, expected_size(take))
    if why and "与请求的" in why:   # FIX P0 ⑤：和任一合法成品尺寸一致（后处理后 / 交付 / 放大出图）就不算尺寸不符
        for size in accepted_sizes(take):
            alt = check_file(path, size)
            if alt is None or "与请求的" not in alt:
                return alt
    return why
