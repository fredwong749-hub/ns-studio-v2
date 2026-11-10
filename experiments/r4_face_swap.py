"""R4 质量 2b 离线 A/B（项目方 11-10 选“2：装专门的换脸 / 身份节点试”）：inswapper 换脸 (+ 可选 GFPGAN / CodeFormer 修复) 能不能救 Q1。
不进生产、不碰 ComfyUI、不写 takes / 队列；模型放 /root/work/fredw/facelab/models（hf-dl 下的 facefusion/models-3.0.0 onnx），
用现成 venv_idm（insightface 2.1 + onnxruntime CPU，只读用、不往里装包）。

臂：swap（只换脸）/ swap+gfpgan / swap+codeformer（修复结果按 blend 混回）。
每张图：Q1 没过的那张脸（per_face.as / bbox）→ 用 insightface 在原图里找 IoU 最大的脸 → inswapper 换成该角色身份脸（与 Q1 同一张，
remote_score.face_refs）→ 打分用生产 remote_score（另一个进程，同一身份脸）。另量脸区清晰度（拉普拉斯方差）、脸外改动。
许可证：inswapper / buffalo_l 都是“非商用研究”——这只是 A/B，结果好也要项目方定许可证再进生产。

两段跑（打分要旧 venv，换脸要 venv_idm）：
  1) cd 旧 repo; /root/work/fredw/venv_idm/bin/python r4_face_swap.py swap --cases <cases.json> --refs <refs.json> --out D
  2) cd 旧 repo; OFFLINE_SELF=gpu33 venv/bin/python r4_face_swap.py prep|score ...
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

MODELS = Path(os.environ.get("FACELAB_MODELS", "/root/work/fredw/facelab/models"))
REPO = Path.cwd()


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - ix * iy
    return ix * iy / u if u else 0


def prep(a) -> int:
    """旧 venv：挑 Q1 没过的帧 + 身份脸路径 → cases.json / refs.json"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import r4_face_fix as FF
    for p in ("tools/gen_contract", "."):
        sys.path.insert(0, str(REPO / p))
    import remote_score as RS
    cases = FF.q1_fails(a.run, a.ep, a.max)
    refs = RS.face_refs(os.environ.get("OFFLINE_SELF") or "gpu33")
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    (out / "cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "refs.json").write_text(json.dumps(refs, ensure_ascii=False, indent=1), encoding="utf-8")
    print("cases", len(cases), "refs", len(refs))
    return 0


def _restore(sess, img, face, blend):
    """GFPGAN / CodeFormer onnx：按 5 点对齐到 512 → 修复 → 逆变换羽化贴回"""
    import cv2
    import numpy as np
    tmpl = np.array([[192.98138, 239.94708], [318.90277, 240.1936], [256.63416, 314.01935],
                     [201.26117, 371.41043], [313.08905, 371.15118]], dtype=np.float32)
    M, _ = cv2.estimateAffinePartial2D(face.kps.astype(np.float32), tmpl, method=cv2.LMEDS)
    crop = cv2.warpAffine(img, M, (512, 512), borderMode=cv2.BORDER_REFLECT)
    x = crop[:, :, ::-1].astype(np.float32) / 255.0
    x = ((x - 0.5) / 0.5).transpose(2, 0, 1)[None]
    feeds = {}
    for inp in sess.get_inputs():
        if len(inp.shape) == 4:
            feeds[inp.name] = x
        else:                                      # CodeFormer 的 fidelity 权重
            feeds[inp.name] = np.array([0.7], dtype=np.float64 if "double" in inp.type else np.float32)
    y = sess.run(None, feeds)[0][0].transpose(1, 2, 0)
    y = ((np.clip(y, -1, 1) + 1) / 2 * 255)[:, :, ::-1]
    y = (blend * y + (1 - blend) * crop).astype(np.uint8)
    Mi = cv2.invertAffineTransform(M)
    h, w = img.shape[:2]
    back = cv2.warpAffine(y, Mi, (w, h))
    m = np.zeros((512, 512), np.float32); m[40:472, 40:472] = 1; m = cv2.GaussianBlur(m, (0, 0), 16)
    mb = cv2.warpAffine(m, Mi, (w, h))[:, :, None]
    return (mb * back + (1 - mb) * img).astype(np.uint8)


def swap(a) -> int:
    """venv_idm：每张图出 swap / swap_gfpgan / swap_codeformer 三张"""
    import cv2
    import numpy as np
    import onnxruntime as ort
    import insightface
    from insightface.app import FaceAnalysis
    ort.set_default_logger_severity(3)
    so = ort.SessionOptions(); so.intra_op_num_threads = 8
    fa = FaceAnalysis(name="buffalo_l", root=os.environ.get("FACELAB_IF_ROOT", "/root/work/fredw/idm_models"),
                      allowed_modules=["detection", "recognition"], providers=["CPUExecutionProvider"])
    fa.prepare(ctx_id=-1, det_size=(640, 640))
    sw = insightface.model_zoo.get_model(str(MODELS / "inswapper_128.onnx"), providers=["CPUExecutionProvider"])
    rest = {k: ort.InferenceSession(str(MODELS / f), so, providers=["CPUExecutionProvider"])
            for k, f in (("gfpgan", "gfpgan_1.4.onnx"), ("codeformer", "codeformer.onnx")) if (MODELS / f).is_file()}
    out = Path(a.out)
    cases = json.loads(Path(a.cases).read_text(encoding="utf-8"))
    refs = json.loads(Path(a.refs).read_text(encoding="utf-8"))
    src_cache, rows = {}, []
    for c in cases:
        f0 = c["faces"][0]; cid = f0["as"]
        row = {"sid": c["sid"], "stage": c["stage"], "n": c["n"], "who": cid, "file": c["file"], "bbox": f0["bbox"], "arms": {}}
        rp = refs.get(cid)
        if not rp:
            row["skip"] = "没有身份脸"; rows.append(row); continue
        if cid not in src_cache:
            ri = cv2.imread(rp if Path(rp).is_absolute() else str(REPO / rp))
            fs = fa.get(ri) if ri is not None else []
            src_cache[cid] = max(fs, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1])) if fs else None
        src = src_cache[cid]
        img = cv2.imread(c["file"])
        faces = fa.get(img) if img is not None else []
        if src is None or not faces:
            row["skip"] = "检测不到脸（身份脸或原图）"; rows.append(row); continue
        tgt = max(faces, key=lambda f: iou(list(f.bbox), f0["bbox"]))
        row["det_iou"] = round(float(iou([float(v) for v in tgt.bbox], f0["bbox"])), 3)
        sw_img = sw.get(img.copy(), tgt, src, paste_back=True)
        arms = {"swap": sw_img}
        swf = max(fa.get(sw_img) or [tgt], key=lambda f: iou(list(f.bbox), f0["bbox"]))
        for k, sess in rest.items():
            try:
                arms[f"swap_{k}"] = _restore(sess, sw_img, swf, a.blend)
            except Exception as e:  # noqa: BLE001  实验：一臂失败记下来
                row["arms"][f"swap_{k}"] = {"error": repr(e)[:300]}
        if "codeformer" in rest:
            # 对焦匹配：修复图（blend=1）与只换脸图线性混合，选脸区清晰度最接近原图的比例——
            # 原图在焦外的脸保持焦外（好莱坞景深），在焦内的脸不被 128px 换脸糊掉
            def lap(im):
                x0, y0, x1, y1 = [int(v) for v in f0["bbox"]]
                return float(cv2.Laplacian(cv2.cvtColor(im[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
            full = _restore(rest["codeformer"], sw_img, swf, 1.0)
            want = lap(img)
            best = min((abs(lap(m) - want), b, m) for b in (0.0, 0.15, 0.3, 0.45, 0.6, 0.75, 0.9, 1.0)
                       for m in [(b * full.astype(np.float32) + (1 - b) * sw_img.astype(np.float32)).astype(np.uint8)])
            arms["swap_cf_match"] = best[2]
            row["cf_match_blend"] = best[1]
        for k, im in arms.items():
            p = out / f"{c['sid']}_{c['stage']}{c['n']}_{k}.png"
            cv2.imwrite(str(p), im)
            diff = np.abs(im.astype(np.int16) - img.astype(np.int16)).mean(axis=2)
            x0, y0, x1, y1 = [int(v) for v in f0["bbox"]]
            pad = int(max(x1 - x0, y1 - y0) * 0.6)
            diff[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad] = 0
            row["arms"][k] = {"file": str(p), "outside_change": round(float(diff.mean()), 3)}
        rows.append(row)
        print(json.dumps({k: v for k, v in row.items() if k != "arms"}, ensure_ascii=False), flush=True)
    (out / "swapped.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def score(a) -> int:
    """旧 venv：生产打分（同一身份脸）+ 脸区清晰度"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import r4_face_fix as FF
    from PIL import Image
    out = Path(a.out)
    rows = json.loads((out / "swapped.json").read_text(encoding="utf-8"))
    host = os.environ.get("OFFLINE_SELF") or "gpu33"
    for r in rows:
        if r.get("skip"):
            continue
        paths = {"orig": r["file"], **{k: v["file"] for k, v in r["arms"].items() if v.get("file")}}
        sims = FF.face_sims(host, paths)
        bb = r["bbox"]; box = tuple(int(v) for v in bb)
        r["orig_sim"] = FF.best_sim(sims.get("orig", []), r["who"], bb)
        r["orig_lap"] = round(FF.lap_var(Image.open(r["file"]).crop(box)), 1)
        for k, v in r["arms"].items():
            if v.get("file"):
                v["sim"] = FF.best_sim(sims.get(k, []), r["who"], bb)
                v["lap"] = round(FF.lap_var(Image.open(v["file"]).crop(box)), 1)
        print(json.dumps({"sid": r["sid"], "stage": r["stage"], "n": r["n"], "orig": r["orig_sim"],
                          **{k: v.get("sim") for k, v in r["arms"].items()}}, ensure_ascii=False), flush=True)
    (out / "rows.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    summ = {}
    for k in sorted({k for r in rows for k in r.get("arms") or {}}):
        xs = [(r["orig_sim"], r["arms"][k]["sim"], r["orig_lap"], r["arms"][k]["lap"]) for r in rows
              if r.get("orig_sim") is not None and (r.get("arms") or {}).get(k, {}).get("sim") is not None]
        summ[k] = {"n": len(xs), "orig_pass": sum(o >= 0.6 for o, *_ in xs), "fixed_pass": sum(f >= 0.6 for _, f, *_ in xs),
                   "mean_delta": round(sum(f - o for o, f, *_ in xs) / len(xs), 3) if xs else None,
                   "sharp_ok": sum(l1 >= 0.8 * l0 for *_, l0, l1 in xs)}
    (out / "summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=1), encoding="utf-8")
    print("SUMMARY", json.dumps(summ, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prep", "swap", "score"])
    ap.add_argument("--run", default="series10_v2")
    ap.add_argument("--ep", type=int, default=5)
    ap.add_argument("--max", type=int, default=12)
    ap.add_argument("--cases"); ap.add_argument("--refs")
    ap.add_argument("--blend", type=float, default=0.6)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    return {"prep": prep, "swap": swap, "score": score}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
