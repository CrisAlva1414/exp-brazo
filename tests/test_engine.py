"""Pruebas del motor MediaPipe Tasks API (sin cámara real).

Los tests puros (resolución de modelos y perfil de pose) corren siempre. El
test de integración usa ``pytest.importorskip("mediapipe")`` y se saltea si
MediaPipe no está instalado en el intérprete que ejecuta la suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exp_brazo.contracts import CameraConfig, Gesture, GestureSettings, PerceptionConfig
from exp_brazo.perception.engine import (
    MediaPipeEngine,
    NullEngine,
    pose_model_filename,
    resolve_model_path,
)
from exp_brazo.perception.landmarks import FrameLandmarks


def _config(**overrides) -> PerceptionConfig:
    gestures = {gesture: GestureSettings(enabled=False) for gesture in Gesture}
    return PerceptionConfig(gestures=gestures, **overrides)


def test_null_engine_returns_empty_landmarks() -> None:
    landmarks = NullEngine().process(object(), ts=5.0)

    assert landmarks.pose is None
    assert landmarks.hands == []
    assert landmarks.ts == 5.0


def test_resolve_model_path_present(tmp_path: Path) -> None:
    name = "pose_landmarker_lite.task"
    (tmp_path / name).write_bytes(b"model")

    assert resolve_model_path(name, tmp_path) == tmp_path / name


def test_resolve_model_path_missing(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="modelo MediaPipe ausente"):
        resolve_model_path("no-existe.task", tmp_path)


@pytest.mark.parametrize(
    ("complexity", "expected"),
    [
        (0, "pose_landmarker_lite.task"),
        (1, "pose_landmarker_full.task"),
    ],
)
def test_pose_model_filename(complexity: int, expected: str) -> None:
    assert pose_model_filename(complexity) == expected


@pytest.mark.parametrize("complexity", [-1, 2, 3])
def test_pose_model_filename_rejects_invalid(complexity: int) -> None:
    with pytest.raises(ValueError):
        pose_model_filename(complexity)


def test_engine_processes_black_frame_with_real_mediapipe() -> None:
    pytest.importorskip("mediapipe")
    np = pytest.importorskip("numpy")

    engine = MediaPipeEngine(
        CameraConfig(),
        _config(process_max_width=320),
        model_dir=Path("assets/models"),
    )
    try:
        result = engine.process(np.zeros((480, 640, 3), np.uint8), ts=1.0)
    finally:
        engine.close()

    assert isinstance(result, FrameLandmarks)
    # Un frame totalmente negro no contiene persona ni manos.
    assert result.pose is None
    assert result.hands == []
    # Los landmarks son normalizados: se reportan las dimensiones originales.
    assert (result.width, result.height) == (640, 480)
