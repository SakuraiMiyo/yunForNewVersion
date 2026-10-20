# -*- coding: utf-8 -*-
"""人脸库：多张本人照片 + 各自的 RetinaFace 检测标注 + 一条激活记录。

为什么需要一个"库"而不是单张照片：跑步途中的人脸核验窗口要求一张**能过取景
质量门**的照片，而不同照片的门通过情况差异很大（框占比/俯仰/偏航都受拍摄角度
影响）。导入多张、逐张自动检测并标注门结果，激活其中一张——执行时 runner 的
build_face_runner 预检就会用激活那张（photo + detection + mirror 三元组，与
runner/main.py --face-photo/--face-detection/--face-mirror 同一语义）。

目录布局（data/face_library/）：

    <entry_id>/photo.jpg         原图原样保存（不做任何修改）
    <entry_id>/annotation.json   检测标注（scripts/make_face_annotation.py 同格式：
                                  box/points/score + source_sha256 内容绑定 + bind_apply）
    <entry_id>/status.json       检测/质量门结果 + 名称 + 导入时间
    active.json                  激活指针：{"entry","photo","detection","mirror":false}

检测模型是 App 自带的 RetinaFace ONNX（models/retinaface_mv1_0.25.onnx），推理
实现与 scripts/retina_detect.py 完全一致（anchor 按左上角为基准解码；
换成中心基准会让 5 点整体落到领带上）。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
RUNNER_DIR = PROJECT_ROOT / "runner"
LIB_ROOT = PROJECT_ROOT / "data" / "face_library"
MODEL_PATH = PROJECT_ROOT / "models" / "retinaface_mv1_0.25.onnx"
ACTIVE_PATH = LIB_ROOT / "active.json"

# 上传图片大小上限（App 比对链最终会把宽度压到 ≤720，超清原图没有意义）
MAX_PHOTO_BYTES = 20 * 1024 * 1024
SUPPORTED_SUFFIX = ".jpg"

_NEED_DEPS = (
    "人脸自动检测需要 numpy + onnxruntime（pip install numpy onnxruntime）。"
    "未安装时照片仍会入库保存，但没有标注就无法激活。"
)


def _ensure_runner_importable() -> None:
    for path in (str(RUNNER_DIR), str(RUNNER_DIR / "tools")):
        if path not in sys.path:
            sys.path.insert(0, path)


_ensure_runner_importable()


# ---------------------------------------------------------------- 检测（与 scripts/retina_detect 同实现）
_detect_fn = None
_detect_error = ""


def _load_detector():
    """按需加载 RetinaFace 推理（scripts/retina_detect.detect）。

    模型文件缺失/依赖缺失都记录原因并返回 None——库的其余功能（列出/删除/查看）
    不因此不可用。
    """
    global _detect_fn, _detect_error
    if _detect_fn is not None or _detect_error:
        return _detect_fn
    if not MODEL_PATH.is_file():
        _detect_error = f"检测模型不存在: {MODEL_PATH}"
        return None
    script = PROJECT_ROOT.parent / "scripts" / "retina_detect.py"
    if not script.is_file():
        _detect_error = f"检测脚本不存在: {script}"
        return None
    try:
        spec = importlib.util.spec_from_file_location("_face_retina_gate", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        _detect_fn = module.detect
    except ImportError as exc:
        _detect_error = _NEED_DEPS + f"（{exc}）"
        return None
    except Exception as exc:  # noqa: BLE001 - 加载失败要有可读的原因
        _detect_error = f"检测器加载失败: {type(exc).__name__}: {exc}"
        return None
    return _detect_fn


def detect_face(image):
    """返回 (score, box, points)（原图坐标）；未检出/不可用返回 None。"""
    detect = _load_detector()
    if detect is None:
        raise RuntimeError(_detect_error or "检测器不可用")
    dets, _best = detect(image)
    if not dets:
        return None
    score, box, points = dets[0]
    return score, box, points


# ---------------------------------------------------------------- 质量门（与 runner 预检同一判据）
def _gate_result(image, det_dict: dict):
    """用 yun_face 的 K() 取景门检查一次检测结果，返回 GateResult 的可序列化摘要。"""
    import yun_face

    det = yun_face.FaceDetection(
        box=tuple(det_dict["box"]),
        points=[tuple(p) for p in det_dict["points"]],
        score=float(det_dict.get("score", 0.0)),
        space="image",
    )
    result = yun_face.quality_gate(image, det)
    return {
        "ok": bool(result.ok),
        "reason": result.reason,
        "score": float(det.score),
        "yaw": None if result.yaw != result.yaw else round(result.yaw, 4),
        "pitch": None if result.pitch != result.pitch else round(result.pitch, 4),
        "roll_deg": None if result.roll_deg != result.roll_deg else round(result.roll_deg, 2),
        "width_ratio": None if result.width_ratio != result.width_ratio else round(result.width_ratio, 4),
    }


# ---------------------------------------------------------------- 库操作
def _entry_path(entry_id: str) -> Path:
    if not entry_id or entry_id != Path(entry_id).name or entry_id.startswith("."):
        raise ValueError(f"非法条目 id: {entry_id!r}")
    return LIB_ROOT / entry_id


def _load_status(entry_dir: Path) -> dict:
    try:
        return json.loads((entry_dir / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def entries() -> list[dict[str, Any]]:
    """全部条目（新导入在前）；每条带激活标记。"""
    if not LIB_ROOT.is_dir():
        return []
    active = _active_entry()
    out = []
    for entry_dir in sorted(LIB_ROOT.iterdir(), reverse=True):
        if not entry_dir.is_dir() or entry_dir.name.startswith((".", "_")):
            continue
        photo = entry_dir / "photo.jpg"
        if not photo.is_file():
            continue
        status = _load_status(entry_dir)
        out.append({
            "entry": entry_dir.name,
            "name": status.get("name") or entry_dir.name,
            "added_at": status.get("added_at"),
            "has_annotation": (entry_dir / "annotation.json").is_file(),
            "gate": status.get("gate") or {"ok": False, "reason": "尚未检测"},
            "detect_error": status.get("detect_error") or "",
            "size_bytes": photo.stat().st_size,
            "active": entry_dir.name == active,
        })
    return out


def add_photo(data: bytes, name: str = "") -> dict[str, Any]:
    """保存一张照片并自动检测 + 质量门。返回新条目摘要。

    照片原样入库；检测/门失败不影响保存——只是不能激活（runner 预检也会拒）。
    """
    if not data:
        raise ValueError("空文件")
    if len(data) > MAX_PHOTO_BYTES:
        raise ValueError(f"图片超过 {MAX_PHOTO_BYTES // (1024 * 1024)} MB 上限")
    try:
        from PIL import Image
        import io
        image = Image.open(io.BytesIO(data))
        image.load()
        if image.format not in ("JPEG", "PNG", "WEBP"):
            raise ValueError(f"不支持的图片格式: {image.format}")
    except ImportError as exc:
        raise RuntimeError("照片解析需要 pillow（pip install pillow）") from exc
    except Exception as exc:  # noqa: BLE001 - 解码失败要有可读原因
        raise ValueError(f"不是可解码的图片: {exc}") from exc

    LIB_ROOT.mkdir(parents=True, exist_ok=True)
    entry_id = time.strftime("%Y%m%d-%H%M%S") + "-" + hashlib.sha256(data).hexdigest()[:6]
    entry_dir = _entry_path(entry_id)
    entry_dir.mkdir(parents=True, exist_ok=False)
    (entry_dir / "photo.jpg").write_bytes(data)

    status: dict[str, Any] = {
        "name": (name or "").strip() or entry_id,
        "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_sha256": hashlib.sha256(data).hexdigest(),
    }
    annotation: dict[str, Any] | None = None
    try:
        det = detect_face(image)
    except RuntimeError as exc:
        status["detect_error"] = str(exc)
        det = None
    if det is not None:
        score, box, points = det
        annotation = {
            "box": [round(float(v), 2) for v in box],
            "points": [[round(float(x), 2), round(float(y), 2)] for x, y in points],
            "score": round(float(score), 4),
            "space": "image",
            "source_sha256": status["source_sha256"],
            "bind_apply": {"after_exif": True, "mirrored": False},
        }
        (entry_dir / "annotation.json").write_text(
            json.dumps(annotation, ensure_ascii=False, indent=1), encoding="utf-8")
        # 门检查用标注坐标（image 空间 == 摆正后的原图坐标系约定，与标注声明一致）
        from PIL import ImageOps
        from yun_face import exif_rotation, apply_rotation
        raw = data
        deg = exif_rotation(raw)
        gate_image = apply_rotation(image, deg) if deg else image
        status["gate"] = _gate_result(gate_image, annotation)
    else:
        status["gate"] = {"ok": False, "reason": status.get("detect_error") or "未检出人脸"}
    (entry_dir / "status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")

    summary = [e for e in entries() if e["entry"] == entry_id]
    return summary[0] if summary else {"entry": entry_id}


def remove(entry_id: str) -> dict[str, Any]:
    entry_dir = _entry_path(entry_id)
    if not entry_dir.is_dir():
        raise FileNotFoundError(f"条目不存在: {entry_id}")
    import shutil
    shutil.rmtree(entry_dir, ignore_errors=False)
    if _active_entry() == entry_id:
        ACTIVE_PATH.unlink(missing_ok=True)
    return {"ok": True, "removed": entry_id}


def _active_entry() -> str:
    try:
        data = json.loads(ACTIVE_PATH.read_text(encoding="utf-8"))
        return str(data.get("entry") or "")
    except (OSError, ValueError):
        return ""


def set_active(entry_id: str) -> dict[str, Any]:
    """激活一条（要求：有标注且质量门通过——runner 预检用的是同一判据）。"""
    entry_dir = _entry_path(entry_id)
    photo = entry_dir / "photo.jpg"
    annotation = entry_dir / "annotation.json"
    if not photo.is_file() or not annotation.is_file():
        raise ValueError("该条目没有检测标注，无法激活（先补装检测依赖或换一张照片）")
    status = _load_status(entry_dir)
    gate = status.get("gate") or {}
    if not gate.get("ok"):
        raise ValueError(f"该照片未通过取景质量门（{gate.get('reason') or '未知原因'}），"
                         "激活了也会被 runner 预检拒绝；请换一张更正脸的照片")
    LIB_ROOT.mkdir(parents=True, exist_ok=True)
    ACTIVE_PATH.write_text(json.dumps({
        "entry": entry_id,
        "photo": str(photo),
        "detection": str(annotation),
        "mirror": False,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "active": entry_id}


def active_config() -> dict[str, Any] | None:
    """给 YunLocalBridge(face_config=...) 的三元组；未激活返回 None。

    照片路径按内容存在性校验后返回——条目被删掉后指针自动失效。
    """
    try:
        data = json.loads(ACTIVE_PATH.read_text(encoding="utf-8"))
        photo = Path(data.get("photo") or "")
        detection = Path(data.get("detection") or "")
        if photo.is_file() and detection.is_file():
            return {"photo": str(photo), "detection": str(detection),
                    "mirror": bool(data.get("mirror"))}
    except (OSError, ValueError):
        pass
    return None


def summary() -> dict[str, Any]:
    """总览用摘要：条数 / 可用（门通过）数 / 激活条目 / 检测依赖状态。"""
    detector = _load_detector()
    all_entries = entries()
    usable = [e for e in all_entries if e["has_annotation"] and e["gate"].get("ok")]
    return {
        "count": len(all_entries),
        "usable": len(usable),
        "active": _active_entry(),
        "detector_ready": detector is not None,
        "detector_error": _detect_error,
        "model": str(MODEL_PATH),
    }
