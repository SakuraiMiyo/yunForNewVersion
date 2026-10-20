# -*- coding: utf-8 -*-
"""跑 APK 自带的 RetinaFace 模型，得到人脸框与 5 点地标，再套质量门。

为什么自己写：原代码要求手工标注。模型随仓库放在 `yunrun/models/retinaface_mv1_0.25.onnx`
（取自云运动 App 3.6.6 的资源），装上 onnxruntime 就能直接跑出真实的框和地标，
比肉眼读数精确得多。

模型规格（来自 FaceRetinaManager.java）：
  · 输入 1x3xHxW，320x320，归一化 (rgb - [104,117,123])（b() 的 z=true 分支）
  · 输出 loc(1,N,4) / conf(1,N,2) / landmarks(1,N,10)，variance [0.1, 0.2]
  · anchor：steps 8/16/32，每点 2 个，min_sizes 16/32、64/128、256/512（MV1 约定）

    python scripts/retina_detect.py 照片.png
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "yunrun" / "models" / "retinaface_mv1_0.25.onnx"
INPUT_SIZE = 320
VARIANCE = (0.1, 0.2)
STEPS = (8, 16, 32)
MIN_SIZES = ((16, 32), (64, 128), (256, 512))
CONF_THRESHOLD = 0.6          # APK 的拍照硬门 t>=0.6

# 质量门（yun_face.check_framing 的等价判据）
MIN_RATIO, MAX_RATIO = 0.40, 0.65


def generate_anchors(size: int = INPUT_SIZE):
    anchors = []
    for step, min_sizes in zip(STEPS, MIN_SIZES):
        fm = size // step
        for y in range(fm):
            for x in range(fm):
                for ms in min_sizes:
                    anchors.append([x * step, y * step,
                                    x * step + ms - 1, y * step + ms - 1])
    return np.asarray(anchors, dtype=np.float32)


def softmax(logits: np.ndarray) -> np.ndarray:
    e = np.exp(logits - logits.max(axis=1, keepdims=True))
    return e / e.sum(axis=1, keepdims=True)


def detect(image: Image.Image):
    """返回按置信度降序的检测列表：[(score, box(x1,y1,x2,y2), 5点), ...]（原图坐标）"""
    W, H = image.size
    rgb = np.asarray(image.convert("RGB"), dtype=np.float32)
    scaled = np.asarray(image.convert("RGB").resize((INPUT_SIZE, INPUT_SIZE),
                                                    Image.BILINEAR), dtype=np.float32)
    # 归一化：(rgb - [104,117,123])，通道序 RGB（b() z=true 分支）
    blob = (scaled - np.array([104.0, 117.0, 123.0], dtype=np.float32))
    blob = blob.transpose(2, 0, 1)[None, ...].astype(np.float32)

    sess = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])
    loc, conf, land = sess.run(None, {sess.get_inputs()[0].name: blob})
    loc, conf, land = loc[0], conf[0], land[0]

    scores = softmax(conf)[:, 1]
    keep = np.where(scores >= CONF_THRESHOLD)[0]
    if keep.size == 0:
        return [], scores.max() if scores.size else 0.0

    anchors = generate_anchors(INPUT_SIZE)
    loc_k, land_k, anch = loc[keep], land[keep], anchors[keep]

    # 基准点 = **anchor 左上角**（不是 RetinaFace 常见的 anchor 中心）。
    # 这是实测确定的（scripts/solve_anchor_order.py + 叠图核对）：
    #   · 中心基准 -> 5 点落在领带上（左眼 (744,987)）
    #   · 左上角基准 -> 5 点正好压在眼镜/鼻/嘴上（左眼 (488,622)）；两种基准瞳距都是 209px，
    #     差别只是把 anchor 半宽/半高（64px 模型空间）重复加了一次。
    ax = anch[:, 0]
    ay = anch[:, 1]
    aw = anch[:, 2] - anch[:, 0] + 1.0
    ah = anch[:, 3] - anch[:, 1] + 1.0
    cx = ax + loc_k[:, 0] * VARIANCE[0] * aw
    cy = ay + loc_k[:, 1] * VARIANCE[0] * ah
    bw = aw * np.exp(loc_k[:, 2] * VARIANCE[1])
    bh = ah * np.exp(loc_k[:, 3] * VARIANCE[1])

    sx, sy = W / INPUT_SIZE, H / INPUT_SIZE
    boxes = np.stack([(cx - bw / 2) * sx, (cy - bh / 2) * sy,
                      (cx + bw / 2) * sx, (cy + bh / 2) * sy], axis=1)

    # 关键点：同样以 anchor 左上角为基准
    pts = np.empty((land_k.shape[0], 5, 2), dtype=np.float32)
    for i in range(5):
        pts[:, i, 0] = (ax + land_k[:, 2 * i] * VARIANCE[0] * aw) * sx
        pts[:, i, 1] = (ay + land_k[:, 2 * i + 1] * VARIANCE[0] * ah) * sy

    order = np.argsort(-scores[keep])
    out = []
    for idx in order[:20]:      # 只取前 20 个，够了
        out.append((float(scores[keep][idx]), boxes[idx].tolist(), pts[idx].tolist()))
    return out, float(scores[keep][order[0]])


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"文件不存在: {path}")
        return 1
    image = Image.open(path)
    W, H = image.size
    print(f"图片: {path.name}  {W}x{H}")

    dets, best = detect(image)
    print(f"检测到 {len(dets)} 个候选（置信度 ≥{CONF_THRESHOLD}），最高 {best:.3f}")
    if not dets:
        print("\n**没有检出人脸**（置信度全低于 0.6）—— 质量门必然失败")
        return 3

    score, box, pts = dets[0]
    x1, y1, x2, y2 = box
    bw = x2 - x1
    ratio = bw / W
    print(f"\n最佳人脸：score={score:.3f}")
    print(f"  框 = ({x1:.0f},{y1:.0f}) -> ({x2:.0f},{y2:.0f})  宽{bw:.0f} 高{y2-y1:.0f}")
    print(f"  5点 = " + "  ".join(f"({px:.0f},{py:.0f})" for px, py in pts))

    # 质量门逐条
    ey = (pts[0][1] + pts[1][1]) / 2.0
    yaw = abs(pts[2][0] - (pts[0][0] + pts[1][0]) / 2.0) / max(1e-6, pts[1][0] - pts[0][0])
    mouth_y = (pts[3][1] + pts[4][1]) / 2.0
    pitch = (pts[2][1] - ey) / max(1e-6, mouth_y - ey)
    import math
    roll = abs(math.degrees(math.atan2(pts[1][1] - pts[0][1], pts[1][0] - pts[0][0])))

    checks = [
        ("框完整在画面内", x1 >= 0 and x2 <= W and y1 >= 0 and y2 <= H,
         f"x {x1:.0f}~{x2:.0f} (W={W})  y {y1:.0f}~{y2:.0f} (H={H})"),
        (f"占宽 {MIN_RATIO:.2f}~{MAX_RATIO:.2f}", MIN_RATIO <= ratio <= MAX_RATIO,
         f"{ratio:.3f}"),
        ("top >= 0.20H", y1 >= 0.2 * H, f"{y1/H:.3f}"),
        ("bottom <= 0.80H", y2 <= 0.8 * H, f"{y2/H:.3f}"),
        ("left >= 0.10W", x1 >= 0.1 * W, f"{x1/W:.3f}"),
        ("right <= 0.90W", x2 <= 0.9 * W, f"{x2/W:.3f}"),
        ("|鼻偏|/瞳距 <= 0.15", yaw <= 0.15, f"{yaw:.3f}"),
        ("俯仰比 0.30~0.80", 0.3 <= pitch <= 0.8, f"{pitch:.3f}"),
        ("眼线倾角 <= 15°", roll <= 15.0, f"{roll:.2f}°"),
        ("置信度 >= 0.60", score >= 0.6, f"{score:.3f}"),
    ]
    print("\n=== 质量门逐条 ===")
    fails = 0
    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<22} [{detail}]")
        if not ok:
            fails += 1
    print()
    if fails == 0:
        print("✅ **全部通过** —— 这张照片能过 APK 的取景质量门")
    else:
        print(f"❌ {fails} 项未通过")
    return 0 if fails == 0 else 4


if __name__ == "__main__":
    raise SystemExit(main())
