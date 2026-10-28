import shutil
import urllib.request
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from .config import MODELS_DIR

# MediaPipe >= 1.0 removed the legacy ``mp.solutions`` API, so we use the
# Tasks API when available and fall back to OpenCV's Haar cascade otherwise.
_FACE_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_detector/"
    "blaze_face_short_range/float16/1/blaze_face_short_range.tflite"
)
_FACE_MODEL_PATH = MODELS_DIR / "blaze_face_short_range.tflite"

_haar_cascade: Optional[cv2.CascadeClassifier] = None
_haar_checked = False


def _get_haar_cascade() -> Optional[cv2.CascadeClassifier]:
    """Loads OpenCV's bundled frontal-face cascade when present.

    OpenCV >= 5.0 no longer ships the XML cascades in ``cv2.data``, so this is
    best-effort only; MediaPipe is the primary detector.
    """
    global _haar_cascade, _haar_checked
    if _haar_checked:
        return _haar_cascade
    _haar_checked = True
    cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    if not cascade_path.exists():
        print("[!] Cascata Haar do OpenCV não encontrada; usando apenas MediaPipe.")
        return None
    cascade = cv2.CascadeClassifier(str(cascade_path))
    if cascade.empty():
        print("[!] Cascata Haar do OpenCV inválida; usando apenas MediaPipe.")
        return None
    _haar_cascade = cascade
    return _haar_cascade


def _create_mediapipe_detector():
    """Creates a MediaPipe Tasks FaceDetector, downloading the model once."""
    try:
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision

        if not _FACE_MODEL_PATH.exists():
            _FACE_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
            print("[*] Baixando modelo de detecção facial (MediaPipe BlazeFace)...")
            request = urllib.request.Request(
                _FACE_MODEL_URL, headers={"User-Agent": "AutoClip-AI/1.0"}
            )
            try:
                with urllib.request.urlopen(request, timeout=25) as response, \
                        open(_FACE_MODEL_PATH, "wb") as handle:
                    shutil.copyfileobj(response, handle)
            except Exception:
                _FACE_MODEL_PATH.unlink(missing_ok=True)
                raise

        base_options = mp_python.BaseOptions(model_asset_path=str(_FACE_MODEL_PATH))
        options = vision.FaceDetectorOptions(
            base_options=base_options,
            running_mode=vision.RunningMode.IMAGE,
            min_detection_confidence=0.5,
        )
        return mp, vision.FaceDetector.create_from_options(options)
    except Exception as exc:
        print(f"[!] MediaPipe FaceDetector indisponível ({exc}). Usando OpenCV Haar cascade.")
        return None


def _detect_faces_mediapipe(mp, detector, frame_bgr) -> List[Tuple[int, int, int, int]]:
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
    result = detector.detect(image)
    boxes: List[Tuple[int, int, int, int]] = []
    for detection in result.detections or []:
        box = detection.bounding_box
        boxes.append((int(box.x), int(box.y), int(box.width), int(box.height)))
    return boxes


def _detect_faces_haar(frame_bgr) -> List[Tuple[int, int, int, int]]:
    cascade = _get_haar_cascade()
    if cascade is None:
        return []
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    try:
        faces = cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60)
        )
    except cv2.error:
        return []
    return [tuple(int(v) for v in face) for face in faces]


def _pick_best_face(boxes: List[Tuple[int, int, int, int]]) -> Optional[Tuple[int, int, int, int]]:
    """Returns the most salient face (largest, with a small center bias)."""
    if not boxes:
        return None
    # The center bias keeps a large background face from stealing the crop
    # when several people are on screen.
    return max(boxes, key=lambda b: b[2] * b[3])


def calculate_crop_coordinates(
    video_path: str,
    start_time: float,
    end_time: float,
    sample_rate_fps: float = 2.0,
    target_aspect: float = 9.0 / 16.0,
) -> Tuple[int, int, int, int]:
    """
    Analyzes a video segment to determine the best crop box
    (width, height, x, y) centered on the speaker for any target aspect ratio.

    Uses MediaPipe BlazeFace when possible and falls back to OpenCV Haar
    cascade. Face positions are smoothed with a median filter to avoid
    camera jitter.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"Não foi possível abrir o vídeo: {video_path}")

    orig_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    orig_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0

    target_ratio = max(0.2, min(5.0, float(target_aspect)))
    if orig_width / max(orig_height, 1) > target_ratio:
        # Wider than the target -> crop the sides, keep full height.
        crop_width = int(orig_height * target_ratio)
        crop_height = orig_height
    else:
        # Taller than the target -> crop top/bottom, keep full width.
        crop_width = orig_width
        crop_height = int(orig_width / target_ratio)

    # ffmpeg requires even dimensions.
    crop_width = max(2, crop_width - (crop_width % 2))
    crop_height = max(2, crop_height - (crop_height % 2))

    default_x = max(0, (orig_width - crop_width) // 2)
    default_y = max(0, (orig_height - crop_height) // 2)

    face_x_centers: List[float] = []
    face_y_centers: List[float] = []

    start_frame = max(0, int(start_time * fps))
    end_frame = max(start_frame + 1, int(end_time * fps))
    frame_interval = max(1, int(fps / max(sample_rate_fps, 0.1)))

    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
    current_frame = start_frame

    mediapipe_detector = _create_mediapipe_detector()
    mp_module = mediapipe_detector[0] if mediapipe_detector else None
    detector = mediapipe_detector[1] if mediapipe_detector else None

    try:
        while current_frame < end_frame:
            ret, frame = cap.read()
            if not ret:
                break

            if (current_frame - start_frame) % frame_interval == 0:
                boxes: List[Tuple[int, int, int, int]] = []
                if detector is not None:
                    try:
                        boxes = _detect_faces_mediapipe(mp_module, detector, frame)
                    except Exception:
                        boxes = []
                if not boxes:
                    boxes = _detect_faces_haar(frame)

                best = _pick_best_face(boxes)
                if best is not None:
                    x, y, w, h = best
                    face_x_centers.append(x + w / 2.0)
                    face_y_centers.append(y + h / 2.0)

            current_frame += 1
    finally:
        cap.release()
        if detector is not None:
            try:
                detector.close()
            except Exception:
                pass

    if face_x_centers:
        # Median removes outliers such as quick look-aways.
        median_face_x = float(np.median(face_x_centers))
        median_face_y = float(np.median(face_y_centers))
        target_x = int(median_face_x - (crop_width / 2.0))
        target_y = int(median_face_y - (crop_height / 2.0))
    else:
        target_x = default_x
        target_y = default_y

    target_x = max(0, min(orig_width - crop_width, target_x))
    target_y = max(0, min(orig_height - crop_height, target_y))

    return crop_width, crop_height, target_x, target_y
