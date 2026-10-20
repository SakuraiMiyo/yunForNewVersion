# -*- coding: utf-8 -*-
"""用真检测器为照片生成 --face-detection 标注 JSON（免手工点选）。

yun_face 的图像源要求标注里带 5 点地标（RetinaFace 未移植时只能人工给），
本脚本用 APK 自带的模型自动产出，并写入 sha256 内容绑定——这样
`verify_photo_binding` 才会通过。

输出格式（load_detection_bundle 兼容）：
    {"box":[x1,y1,x2,y2], "points":[[..]x5], "score":..,
     "source_sha256":"..", "bind_apply":{"after_exif":true,"mirrored":false},
     "space":"image"}

    python scripts/make_face_annotation.py 照片.jpg 输出.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from retina_detect import detect  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("photo")
    ap.add_argument("out")
    args = ap.parse_args()

    photo = Path(args.photo)
    if not photo.is_file():
        print(f"照片不存在: {photo}")
        return 1
    img = Image.open(photo)
    dets, best = detect(img)
    if not dets:
        print("未检出人脸，无法生成标注")
        return 3

    score, box, pts = dets[0]
    digest = hashlib.sha256(photo.read_bytes()).hexdigest()
    payload = {
        "box": [round(v, 2) for v in box],
        "points": [[round(x, 2), round(y, 2)] for x, y in pts],
        "score": round(float(score), 4),
        "space": "image",
        "source_sha256": digest,
        "bind_apply": {"after_exif": True, "mirrored": False},
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"照片 {photo.name}  {img.width}x{img.height}")
    print(f"  置信度 {score:.4f}")
    print(f"  框 {[round(v) for v in box]}")
    print(f"  5点 {[[round(x), round(y)] for x, y in pts]}")
    print(f"  sha256 {digest[:16]}…")
    print(f"已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
