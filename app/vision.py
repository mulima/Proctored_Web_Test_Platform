"""Server-side second opinion on an uploaded evidence snapshot.

The browser does the continuous work: it runs a cheap prefilter and, when that fires, a
small ONNX detector, several times a second, on frames that never leave the machine.
That is the only design that scales - thirty candidates means thirty laptops doing their
own inference instead of one container trying to keep up with thirty video streams.

What the server does is different in kind: exactly one inference, on the single frame
that a flag was raised about, after it has been uploaded as evidence. That costs one
model run per incident rather than four per second per candidate, and it gives a marker
something checkable: the browser said phone, and here is whether the server agreed.

If onnxruntime or a model file is missing the app runs unchanged and snapshots simply
carry a verdict of "not_checked". Detection is never a hard dependency.
"""

import os
import threading

from app.config import settings

_MODEL_NAMES = (
    "phone_detector_oiv7.onnx",
    "phone_detector_yolo11n.onnx",
    "phone_detector.onnx",
)

_lock = threading.Lock()
_session = None
_input_name = ""
_model_path = ""
_load_attempted = False
_unavailable_reason = ""


def model_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "models")


def _phone_class_ids(path: str) -> set[int]:
    # Open Images V7 calls a phone class 339; the COCO models use 67.
    return {339} if "oiv7" in os.path.basename(path).lower() else {67}


def _load() -> None:
    global _session, _input_name, _model_path, _load_attempted, _unavailable_reason
    if _load_attempted:
        return
    _load_attempted = True

    for name in _MODEL_NAMES:
        candidate = os.path.join(model_dir(), name)
        if os.path.exists(candidate):
            _model_path = candidate
            break
    if not _model_path:
        _unavailable_reason = (
            "No phone-detection model found in app/static/models. Snapshots will be "
            "stored without a server-side second opinion."
        )
        return

    try:
        import onnxruntime as ort

        _session = ort.InferenceSession(_model_path, providers=["CPUExecutionProvider"])
        _input_name = _session.get_inputs()[0].name
    except Exception as exc:
        _session = None
        _unavailable_reason = f"onnxruntime could not load {_model_path}: {exc}"


def available() -> bool:
    with _lock:
        _load()
        return _session is not None


def unavailable_reason() -> str:
    with _lock:
        _load()
        return _unavailable_reason


def check_snapshot(image_bytes: bytes) -> tuple[str, float]:
    """Return (verdict, confidence).

    verdict is one of: phone_detected, no_phone, not_checked.
    """
    if not settings.server_side_snapshot_recheck:
        return "not_checked", 0.0
    with _lock:
        _load()
        session = _session
        path = _model_path
    if session is None:
        return "not_checked", 0.0

    try:
        import cv2
        import numpy as np

        buffer = np.frombuffer(image_bytes, dtype=np.uint8)
        frame = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
        if frame is None:
            return "not_checked", 0.0

        image = cv2.resize(frame, (640, 640))
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        blob = np.transpose(image, (2, 0, 1))[None, ...]

        outputs = session.run(None, {_input_name: blob})
        data = np.squeeze(np.array(outputs[0]))
        if data.ndim != 2:
            return "not_checked", 0.0
        # Ultralytics exports come out transposed relative to what we want here.
        if data.shape[0] < data.shape[1]:
            data = data.T

        wanted = _phone_class_ids(path)
        best = 0.0
        for row in data:
            scores = row[4:]
            if scores.size == 0:
                continue
            class_id = int(np.argmax(scores))
            if class_id in wanted:
                best = max(best, float(scores[class_id]))
        return ("phone_detected" if best >= 0.35 else "no_phone"), round(best, 4)
    except Exception:
        return "not_checked", 0.0
