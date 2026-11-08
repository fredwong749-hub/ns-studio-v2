"""L-203（项目方 11-06 08:3x：“你所有的首尾帧都是一模一样的图片”）：尾帧出图后与通过契约的首帧比对，太像 = FL 硬项不过、换种子重出。

度量（packages/qc/ns_qc/media.py 已有，不另装库）：
  struct  = structure_match：24×42 模糊亮度的相关系数（色彩无关、只看大构图）。11-06 在 data/_offline_pull/gpu33 的 ep01 首尾帧 111 对上校准：
            min -0.06 / p10 0.40 / p25 0.62 / 中位 0.82 / p75 0.94 / p90 0.98 / max 1.00；项目方点名“几乎一样”的 ep01-S1 v7 = 0.858、S2 v3 = 0.828，
            真推近到更紧景别的 S8 v21 = 0.736，换人换位的 S14 v11 = 0.31 → 阈值 0.80（≥ 判太像）
  ssim    = 等比缩到宽 256 后的亮度 SSIM（对小位移敏感、区分度差：S1 v7 0.48 vs S8 v21 0.42），只作第二道：≥ 0.85 也判太像
            （同一校准集 min 0.20 / 中位 0.57 / p90 0.78 / max 0.96，只有 7 对 ≥ 0.85——全是肉眼几乎同一张）
配置：config/gen_contract.json "first_last" = {"struct_max": 0.80, "ssim_max": 0.85, "width": 256}；缺键用这里的默认。
blocking_items 里的 "FL" 使之成为 L-196 硬项（只有尾帧阶段才有 FL 项，首帧不受影响）。读不到图 = untested（失败关闭，同样挡）。
采用路径：producer.fl_bad —— 太像的尾帧不进 review_adopt / best_effort 采用（与 L-200 gaze_bad 同一待遇）。"""
from __future__ import annotations

from pathlib import Path

from . import cfg

ITEM_ID = "FL"
TITLE = "首尾帧差异（L-203：尾帧与首帧太像 = 重出）"
DEFAULTS = {"struct_max": 0.80, "ssim_max": 0.85, "width": 256}


def conf(c: dict | None = None) -> dict:
    d = (c if c is not None else cfg()).get("first_last")
    out = dict(DEFAULTS)
    if isinstance(d, dict):
        for k in DEFAULTS:
            if isinstance(d.get(k), (int, float)):
                out[k] = d[k]
    return out


def _small(rgb, width: int):
    import cv2
    h = max(1, int(round(rgb.shape[0] * width / max(1, rgb.shape[1]))))
    return cv2.resize(rgb, (width, h), interpolation=cv2.INTER_AREA)


def similarity(first: Path | str, last: Path | str, width: int = 256) -> dict:
    """→ {struct, ssim}（两张先各自等比缩到同宽；尺寸比例不同时 ssim 按较小的公共区域算）。抛 OSError / ValueError 由调用方记 untested"""
    import sys
    root = Path(__file__).resolve().parents[4]
    if str(root / "packages" / "qc") not in sys.path:
        sys.path.insert(0, str(root / "packages" / "qc"))
    from ns_qc import media as M
    a, b = M.load_rgb(Path(first)), M.load_rgb(Path(last))
    st = M.structure_match(a, b)
    a2, b2 = _small(a, width), _small(b, width)
    h = min(a2.shape[0], b2.shape[0])
    s = M.ssim(a2[:h], b2[:h])
    return {"struct": round(float(st), 4), "ssim": round(float(s), 4)}


def item(first: Path | str | None, last: Path | str, c: dict | None = None) -> dict:
    """契约项 FL：pass = 差异够大；fail = 太像（struct ≥ struct_max 或 ssim ≥ ssim_max）；untested = 没有首帧 / 读图失败"""
    t = conf(c)
    base = {"id": ITEM_ID, "title": TITLE, "blocking": True}
    if not first:
        return {**base, "status": "untested", "detail": "没有首帧文件可比（job.first_frame_file 缺）", "evidence": {"thresholds": t}}
    try:
        m = similarity(first, last, int(t["width"]))
    except Exception as e:  # noqa: BLE001  # arch-ok: 读图 / 度量失败记 untested（失败关闭，仍挡），不冒充通过
        return {**base, "status": "untested", "detail": f"首尾帧比对失败：{e!r}"[:300],
                "evidence": {"first": str(first), "thresholds": t}}
    too_like = m["struct"] >= float(t["struct_max"]) or m["ssim"] >= float(t["ssim_max"])
    ev = {**m, "first": str(first), "thresholds": t, "too_similar": too_like}
    if too_like:
        return {**base, "status": "fail", "evidence": ev,
                "detail": (f"尾帧与首帧太像（构图相关 {m['struct']:.3f} ≥ {t['struct_max']} 或 SSIM {m['ssim']:.3f} ≥ {t['ssim_max']}）："
                           "尾帧要按落幅单独构图（L-203），换种子重出")}
    return {**base, "status": "pass", "evidence": ev,
            "detail": f"首尾帧差异够大（构图相关 {m['struct']:.3f} < {t['struct_max']}，SSIM {m['ssim']:.3f} < {t['ssim_max']}）"}


def apply(pf: dict, first: Path | str | None, last: Path | str, c: dict | None = None) -> dict:
    """把 FL 项并进尾帧 postflight（原地改并返回）：不过 → ok=False、blocking 加 FL、reasons 加一条。已有 FL 项先去掉再加"""
    if not isinstance(pf, dict):
        return pf
    it = item(first, last, c)
    pf["items"] = [i for i in pf.get("items") or [] if i.get("id") != ITEM_ID] + [it]
    pf.setdefault("reasons", [])
    pf["reasons"] = [r for r in pf["reasons"] if not str(r).startswith(ITEM_ID)]
    pf["blocking"] = [b for b in pf.get("blocking") or [] if b != ITEM_ID]
    if it["status"] in ("fail", "untested"):
        pf["ok"] = False
        pf["blocking"] = sorted(set(pf["blocking"]) | {ITEM_ID})
        pf["reasons"].append(f"{ITEM_ID} {it['detail']}")
    pf["first_last"] = it.get("evidence")
    return pf


def too_similar(pf: dict | None) -> bool:
    """这一版尾帧的 FL 项判了太像（fail）——任何采用路径都不收（producer.fl_bad 调）"""
    for i in (pf or {}).get("items") or []:
        if i.get("id") == ITEM_ID and i.get("status") == "fail" and not i.get("record_only"):
            return True
    return False
