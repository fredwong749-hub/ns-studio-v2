"""R4 质量 2 离线 A/B：出图后“只修脸”能不能把 Q1（脸不像）救回来。不写 takes.json、不碰队列，只在空闲的出图道上跑。

做法（只用生产已有节点：Qwen-Image 2.1 编辑 + 生产预设，不装新东西）：
  1. 挑 Q1 没过的首 / 尾帧（takes.json 原始 postflight），取每张脸的 bbox 和“应是谁”（per_face.as）
  2. 以脸为中心裁正方形（bbox 放大 CROP 倍），缩到 1024
  3. qwen_edit：图1 = 该角色身份脸（与 Q1 打分同一张，remote_score.face_refs），图2 = 裁出的脸，init = 裁图，denoise = D（网格）
  4. 结果缩回原尺寸、椭圆羽化贴回原图
  5. 原图 / 修后各用生产打分（remote_score.score，同一身份脸）量相似度；另量脸区清晰度（拉普拉斯方差，防“修成塑料脸”）、
     脸外区域改动（应≈0）
过线（写进报告再决定上不上生产）：修后 Q1 过线（≥ 0.6，近中景）的比例比原来高 20 个点以上，脸区清晰度不低于原图的 80%。

在服务器旧部署 repo 目录下跑（cwd = /root/work/fredw/offline_20261101/repo，旧 venv）：
  OFFLINE_SELF=gpu33 venv/bin/python /root/work/fredw/ns2/code/experiments/r4_face_fix.py --run series10_v2 --ep 5 \
      --lane http://10.0.0.33:8188 --denoise 0.35,0.5,0.65 --max 12 --out /root/work/fredw/ns2/rep/r4_face_fix
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO = Path.cwd()
for p in ("services/render_router", "services/agent", "packages/schema", "packages/qc", "tools/gen_contract", "."):
    if str(REPO / p) not in sys.path:
        sys.path.insert(0, str(REPO / p))

CROP = 2.4          # bbox 放大倍数（含头发、下巴，给模型上下文）
SIDE = 1024
PROMPT = ("把图2中人物的脸改成图1这个人：五官、脸型、眼睛、眉毛、鼻子、嘴唇、年龄感与图1一致。"
          "保持图2的表情、视线方向、头部角度、光线方向、肤色与皮肤纹理、发型、妆容和画面构图完全不变，只改脸。真实摄影质感，不磨皮。")


def q1_fails(run: str, ep: int, limit: int) -> list[dict]:
    """Q1 没过的版本：[{sid, stage, n, file, faces: [{as, bbox, sim}]}]（每镜每阶段最多取最新 2 版）"""
    out = []
    for tj in sorted((REPO / "data/renders" / run / f"ep{ep:02d}").glob("S*/takes.json"), key=lambda p: int(p.parent.name[1:])):
        d = json.loads(tj.read_text(encoding="utf-8"))
        per = {}
        for t in sorted(d.get("takes") or [], key=lambda t: -(t.get("n") or 0)):
            pf = t.get("postflight")
            if t.get("stage") not in ("image", "last") or not isinstance(pf, dict):
                continue
            q1 = next((i for i in pf.get("items") or [] if i.get("id") == "Q1"), None)
            if not q1 or q1.get("status") != "fail":
                continue
            faces = [{"as": f.get("as"), "bbox": f.get("bbox"), "sim": f.get("sim")} for f in (q1.get("evidence") or {}).get("per_face") or []
                     if f.get("as") and f.get("bbox") and (f.get("verdict") in ("不像", "复核带"))]
            k = (t["stage"],)
            if not faces or per.get(k, 0) >= 2:
                continue
            per[k] = per.get(k, 0) + 1
            f = t.get("file") or ""
            out.append({"sid": f"ep{ep:02d}-{tj.parent.name}", "stage": t["stage"], "n": t["n"],
                        "file": str(REPO / "data" / f) if not f.startswith("data/") else str(REPO / f), "faces": faces})
    return out[:limit]


def square(bbox, w, h, k=CROP):
    x0, y0, x1, y1 = bbox
    cx, cy, s = (x0 + x1) / 2, (y0 + y1) / 2, max(x1 - x0, y1 - y0) * k
    s = min(s, w, h)
    l, t = int(max(0, min(w - s, cx - s / 2))), int(max(0, min(h - s, cy - s / 2)))
    return l, t, int(s)


def lap_var(img) -> float:
    import cv2
    import numpy as np
    g = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2GRAY)
    return float(cv2.Laplacian(g, cv2.CV_64F).var())


def paste(base, patch, box):
    """椭圆羽化贴回（只改脸附近，边缘渐变）"""
    from PIL import Image, ImageDraw, ImageFilter
    l, t, s = box
    patch = patch.resize((s, s), Image.LANCZOS)
    m = Image.new("L", (s, s), 0)
    ImageDraw.Draw(m).ellipse([s * 0.18, s * 0.12, s * 0.82, s * 0.9], fill=255)
    m = m.filter(ImageFilter.GaussianBlur(s * 0.06))
    out = base.copy()
    out.paste(patch, (l, t), m)
    return out


def fix_one(client, W, IP, eff, img_path: str, face: dict, ref_path: str, denoise: float, seed: int, tmp: Path):
    from PIL import Image
    base = Image.open(img_path).convert("RGB")
    box = square(face["bbox"], *base.size)
    l, t, s = box
    crop = base.crop((l, t, l + s, t + s)).resize((SIDE, SIDE), Image.LANCZOS)
    cp = tmp / f"crop_{seed}.png"
    crop.save(cp)
    names = [client.upload_image(Path(ref_path), subfolder="ns_ab/r4face"), client.upload_image(cp, subfolder="ns_ab/r4face")]
    wf = W.qwen_edit(PROMPT, "", names, SIDE, SIDE, seed, f"ns_ab/r4face/{seed}", m=W.QwenModel(), loras=[],
                     ref_resolution=0, crop=False, init_image=names[1], denoise=denoise)
    wf = IP.apply(wf, eff, set())
    entry, secs = client.run(wf, timeout_s=900)
    item = next(f for f in client.output_files(entry) if f["filename"].endswith(".png"))
    op = tmp / f"out_{seed}.png"
    client.download(item, op)
    fixed = paste(base, Image.open(op).convert("RGB"), box)
    return fixed, box, secs


def face_sims(host: str, paths: dict[str, str]) -> dict:
    import remote_score as RS
    m = RS.score(host, paths)
    return {k: v.get("faces") or [] for k, v in (m.get("items") or {}).items()}


def best_sim(faces: list, cid: str, bbox) -> float | None:
    """与原 bbox 重叠最大那张脸对 cid 的相似度"""
    def iou(a, b):
        ix = max(0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
        u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - ix * iy
        return ix * iy / u if u else 0
    cand = [f for f in faces if f.get("bbox")]
    if not cand:
        return None
    f = max(cand, key=lambda f: iou(f["bbox"], bbox))
    sims = f.get("sims") or {}
    return sims.get(cid)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="series10_v2")
    ap.add_argument("--ep", type=int, default=5)
    ap.add_argument("--lane", default="http://10.0.0.33:8188")
    ap.add_argument("--denoise", default="0.35,0.5,0.65")
    ap.add_argument("--max", type=int, default=12)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    from PIL import Image
    from ns_render import workflows as W, image_params as IP
    from ns_render.comfy_client import ComfyClient
    from ns_render import unified_frame as UF
    import remote_score as RS
    host = os.environ.get("OFFLINE_SELF") or "gpu33"
    out = Path(a.out); tmp = out / "tmp"; tmp.mkdir(parents=True, exist_ok=True)
    eff = IP.resolve(UF.preset_id(), {"style_suffix": ""})
    refs = RS.face_refs(host)
    client = ComfyClient(a.lane)
    cases = q1_fails(a.run, a.ep, a.max)
    print(f"cases {len(cases)}", flush=True)
    rows = []
    for ci, c in enumerate(cases):
        f0 = c["faces"][0]
        cid = f0["as"]
        if cid not in refs:
            rows.append({**c, "skip": f"没有 {cid} 的身份脸"}); continue
        base = Image.open(c["file"]).convert("RGB")
        x0, y0, x1, y1 = [int(v) for v in f0["bbox"]]
        lap0 = lap_var(base.crop((x0, y0, x1, y1)))
        paths = {"orig": c["file"]}
        arms = {}
        for d in [float(x) for x in a.denoise.split(",")]:
            seed = 1000 + ci * 10 + int(d * 100)
            try:
                fixed, box, secs = fix_one(client, W, IP, eff, c["file"], f0, refs[cid], d, seed, tmp)
            except Exception as e:  # noqa: BLE001  实验：一格失败记下来继续
                arms[str(d)] = {"error": repr(e)[:300]}; continue
            p = out / f"{c['sid']}_{c['stage']}{c['n']}_d{int(d*100)}.png"
            fixed.save(p)
            paths[str(d)] = str(p)
            import numpy as np
            diff = np.abs(np.asarray(fixed, dtype=np.int16) - np.asarray(base, dtype=np.int16))
            l, t, s = box; diff[t:t + s, l:l + s] = 0
            arms[str(d)] = {"file": str(p), "secs": round(secs, 1), "lap": round(lap_var(fixed.crop((x0, y0, x1, y1))), 1),
                            "outside_change": round(float(diff.mean()), 3)}
        sims = face_sims(host, paths)
        r = {"sid": c["sid"], "stage": c["stage"], "n": c["n"], "who": cid, "orig_sim_pf": f0["sim"],
             "orig_sim": best_sim(sims.get("orig", []), cid, f0["bbox"]), "orig_lap": round(lap0, 1), "arms": arms}
        for k in arms:
            if "file" in arms[k]:
                arms[k]["sim"] = best_sim(sims.get(k, []), cid, f0["bbox"])
        rows.append(r)
        print(json.dumps(r, ensure_ascii=False), flush=True)
        (out / "rows.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    # 小结
    summ = {}
    for d in a.denoise.split(","):
        ok0 = ok1 = n = sharp = 0
        for r in rows:
            arm = (r.get("arms") or {}).get(str(float(d)))
            if not arm or arm.get("sim") is None or r.get("orig_sim") is None:
                continue
            n += 1; ok0 += r["orig_sim"] >= 0.6; ok1 += arm["sim"] >= 0.6
            sharp += arm["lap"] >= 0.8 * r["orig_lap"]
        summ[d] = {"n": n, "orig_pass": ok0, "fixed_pass": ok1, "sharp_ok": sharp}
    (out / "summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=1), encoding="utf-8")
    print("SUMMARY", json.dumps(summ, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
