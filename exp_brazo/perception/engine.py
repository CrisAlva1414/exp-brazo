"""Motores de percepción: MediaPipe Tasks API (perezoso) y nulo para tests.

``mediapipe`` y ``cv2`` se importan dentro de los métodos para que el resto
del sistema (y la suite de tests) funcione sin esas dependencias instaladas.

La migración a **MediaPipe Tasks API** (``mediapipe.tasks.python.vision``)
reemplaza a las ``mp.solutions`` legacy. Los modelos ``.task`` están
vendorizados en ``assets/models/`` y se usan **offline**: en runtime no se
descarga nada.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Protocol

from ..contracts import CameraConfig, PerceptionConfig
from .landmarks import POSE_INDEX, FrameLandmarks, Hand, Landmark, Pose

_LOGGER = logging.getLogger(__name__)

POSE_LITE = "pose_landmarker_lite.task"
POSE_FULL = "pose_landmarker_full.task"
HAND_MODEL = "hand_landmarker.task"
REQUIRED_MODELS = (POSE_LITE, POSE_FULL, HAND_MODEL)


def models_dir() -> Path:
    """Ubica el directorio de modelos vendorizados.

    Prefiere ``assets/models`` relativo al directorio de trabajo (útil si el
    operador arranca desde la raíz del repo) y cae a la ruta relativa al
    paquete para que funcione aunque se ejecute desde otra carpeta.
    """

    cwd_models = Path.cwd() / "assets/models"
    if cwd_models.is_dir():
        return cwd_models
    return Path(__file__).resolve().parents[2] / "assets/models"


def resolve_model_path(name: str, model_dir: Path | None = None) -> Path:
    """Devuelve la ruta al modelo ``.task`` o falla con un mensaje accionable."""

    directory = Path(model_dir) if model_dir is not None else models_dir()
    path = directory / name
    if not path.is_file():
        raise RuntimeError(
            f"modelo MediaPipe ausente: {path}; ejecutá ./install.sh"
        )
    return path


def pose_model_filename(complexity: int) -> str:
    """Mapea ``pose_model_complexity`` (0=lite, 1=full) al archivo ``.task``."""

    if int(complexity) == 0:
        return POSE_LITE
    if int(complexity) == 1:
        return POSE_FULL
    raise ValueError(f"pose_model_complexity inválido: {complexity!r} (esperado 0 o 1)")


class PerceptionEngine(Protocol):
    """Contrato mínimo de un motor de extracción de landmarks."""

    def process(self, frame_bgr, ts: float | None = None) -> FrameLandmarks:
        """Procesa un frame BGR y devuelve sus landmarks."""

    def close(self) -> None:
        """Libera recursos del motor."""


def mediapipe_available() -> bool:
    """Indica si ``mediapipe`` y ``mediapipe.tasks`` pueden importarse.

    No importa ``cv2``: la disponibilidad del motor de percepción y la de
    OpenCV se reportan por separado.
    """

    try:
        import mediapipe  # noqa: F401
        import mediapipe.tasks  # noqa: F401
    except Exception:
        return False
    return True


class NullEngine:
    """Motor sin detección: siempre devuelve un frame vacío."""

    def process(self, frame_bgr, ts: float | None = None) -> FrameLandmarks:
        moment = time.monotonic() if ts is None else float(ts)
        height, width = 0, 0
        shape = getattr(frame_bgr, "shape", None)
        if shape is not None and len(shape) >= 2:
            height, width = int(shape[0]), int(shape[1])
        return FrameLandmarks(None, [], moment, width, height)

    def close(self) -> None:
        return None


def _bbox_area(landmarks) -> float:
    """Área del bounding box normalizado de una lista de landmarks."""

    if not landmarks:
        return 0.0
    xs = [float(getattr(point, "x", 0.0)) for point in landmarks]
    ys = [float(getattr(point, "y", 0.0)) for point in landmarks]
    return max(0.0, max(xs) - min(xs)) * max(0.0, max(ys) - min(ys))


def _largest_pose(pose_landmarks: list) -> list | None:
    """Elige la pose con mayor bounding box (``num_poses=1`` normalmente)."""

    if not pose_landmarks:
        return None
    return max(pose_landmarks, key=_bbox_area)


class MediaPipeEngine:
    """Motor basado en MediaPipe Tasks API (Pose + Hands) con modelos locales."""

    def __init__(
        self,
        camera: CameraConfig,
        perception: PerceptionConfig,
        *,
        model_dir: Path | None = None,
    ) -> None:
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision

        self._mp = mp
        self._camera = camera
        self._perception = perception

        pose_path = resolve_model_path(
            pose_model_filename(perception.pose_model_complexity), model_dir
        )
        hand_path = resolve_model_path(HAND_MODEL, model_dir)

        pose_options = vision.PoseLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=str(pose_path)),
            running_mode=vision.RunningMode.VIDEO,
            num_poses=1,
            min_pose_detection_confidence=perception.min_detection_confidence,
            min_pose_presence_confidence=perception.min_detection_confidence,
            min_tracking_confidence=perception.min_tracking_confidence,
        )
        hand_options = vision.HandLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=str(hand_path)),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=perception.max_hands,
            min_hand_detection_confidence=perception.min_detection_confidence,
            min_hand_presence_confidence=perception.min_detection_confidence,
            min_tracking_confidence=perception.min_tracking_confidence,
        )

        self._pose = vision.PoseLandmarker.create_from_options(pose_options)
        self._hands = vision.HandLandmarker.create_from_options(hand_options)
        self._last_ts_ms = 0

        # Compatibilidad con el estado expuesto en el dashboard/diagnóstico.
        # Ya no hay fallback a modelos legacy: si falta un modelo vendorizado,
        # ``resolve_model_path`` falla con un error claro.
        self.model_fallback: str | None = None
        self.hand_model_fallback: str | None = None

    def process(self, frame_bgr, ts: float | None = None) -> FrameLandmarks:
        import cv2

        if frame_bgr is None:
            moment = time.monotonic() if ts is None else float(ts)
            return FrameLandmarks(None, [], moment)

        height, width = frame_bgr.shape[:2]
        process_frame = frame_bgr
        max_width = int(self._perception.process_max_width)
        # Los landmarks de MediaPipe son normalizados: se puede inferir sobre
        # una versión reducida y dibujar el overlay sobre el frame original.
        if width > max_width > 0:
            process_height = max(1, int(round(height * max_width / width)))
            process_frame = cv2.resize(
                frame_bgr,
                (max_width, process_height),
                interpolation=cv2.INTER_AREA,
            )
        rgb = cv2.cvtColor(process_frame, cv2.COLOR_BGR2RGB)

        ts_ms = int((ts if ts is not None else time.monotonic()) * 1000)
        if ts_ms <= self._last_ts_ms:
            ts_ms = self._last_ts_ms + 1
        self._last_ts_ms = ts_ms

        mp_image = self._mp.Image(
            image_format=self._mp.ImageFormat.SRGB,
            data=rgb,
        )

        pose_result = self._pose.detect_for_video(mp_image, ts_ms)
        hand_result = self._hands.detect_for_video(mp_image, ts_ms)

        pose: Pose | None = None
        chosen = _largest_pose(getattr(pose_result, "pose_landmarks", None) or [])
        if chosen:
            points: dict[str, Landmark] = {}
            for name, index in POSE_INDEX.items():
                if index >= len(chosen):
                    continue
                point = chosen[index]
                points[name] = Landmark(
                    x=float(point.x),
                    y=float(point.y),
                    z=float(point.z),
                    visibility=float(getattr(point, "visibility", 1.0)),
                )
            pose = Pose(points)

        hands: list[Hand] = []
        hand_landmarks = getattr(hand_result, "hand_landmarks", None) or []
        handedness = getattr(hand_result, "handedness", None) or []
        for index, landmarks_raw in enumerate(hand_landmarks):
            landmarks = [
                Landmark(x=float(point.x), y=float(point.y), z=float(point.z))
                for point in landmarks_raw
            ]
            label = "unknown"
            score = 1.0
            if index < len(handedness) and handedness[index]:
                category = handedness[index][0]
                label = str(getattr(category, "category_name", None) or "unknown")
                score = float(getattr(category, "score", 1.0))
            hands.append(Hand(landmarks, label, score))

        return FrameLandmarks(
            pose,
            hands,
            time.time(),
            int(width),
            int(height),
        )

    def close(self) -> None:
        for attribute in ("_pose", "_hands"):
            resource = getattr(self, attribute, None)
            if resource is None:
                continue
            try:
                resource.close()
            except Exception:
                continue
            finally:
                setattr(self, attribute, None)
