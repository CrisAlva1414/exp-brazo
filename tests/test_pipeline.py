"""Pruebas del pipeline de cámara sin cv2 ni mediapipe reales."""

from __future__ import annotations

import sys
import time

import pytest

import exp_brazo.perception.pipeline as pipeline_module
from exp_brazo.contracts import CameraConfig, Gesture, GestureSettings, PerceptionConfig
from exp_brazo.perception.engine import NullEngine, mediapipe_available
from exp_brazo.perception.pipeline import CameraPipeline


def _inactive_config() -> PerceptionConfig:
    gestures = {gesture: GestureSettings(enabled=False) for gesture in Gesture}
    return PerceptionConfig(gestures=gestures)


class FakeCapture:
    """Captura falsa con frames numpy o lecturas fallidas."""

    def __init__(self, np_module, *, fail: bool = False) -> None:
        self._np = np_module
        self.fail = fail
        self.reads = 0
        self.released = False

    def isOpened(self) -> bool:
        return True

    def read(self):
        if self.fail:
            return False, None
        self.reads += 1
        return True, self._np.zeros((48, 64, 3), dtype=self._np.uint8)

    def release(self) -> None:
        self.released = True


def test_null_engine_returns_empty_frame() -> None:
    landmarks = NullEngine().process(object(), ts=123.0)
    assert landmarks.pose is None
    assert landmarks.hands == []
    assert landmarks.ts == 123.0


def test_mediapipe_available_returns_bool() -> None:
    assert isinstance(mediapipe_available(), bool)


def test_pipeline_processes_frames_without_cv2(monkeypatch) -> None:
    np = pytest.importorskip("numpy")
    # Simula explícitamente la ausencia de cv2, aunque esté instalado.
    monkeypatch.setitem(sys.modules, "cv2", None)
    created: list[FakeCapture] = []

    def factory(camera):
        capture = FakeCapture(np)
        created.append(capture)
        return capture

    pipeline = CameraPipeline(
        CameraConfig(),
        _inactive_config(),
        lambda event: None,
        engine=NullEngine(),
        capture_factory=factory,
    )
    pipeline.start()
    try:
        assert pipeline.wait_first_frame(2.0) is True
        status = pipeline.status()
        deadline = time.time() + 2.0
        while time.time() < deadline and status["frames"] < 2:
            time.sleep(0.01)
            status = pipeline.status()

        assert status["camera_ok"] is True
        assert status["running"] is True
        assert status["frames"] >= 2
        assert status["last_error"] is None
        assert status["fps"] >= 0.0
        assert status["process_fps"] >= 0.0
        assert status["detection_fps"] == 15
        assert status["latency_ms"] >= 0.0
        assert "gestures" in status
        # Con capture_factory no hay driver real que reporte resolución/FPS.
        assert "camera_actual" in status
        assert status["camera_actual"] is None
        assert pipeline.snapshot() is not None
        # Sin cv2 no debe haber JPEG, y eso no debe romper el pipeline.
        assert pipeline.get_jpeg() is None
    finally:
        pipeline.stop()

    assert pipeline.status()["running"] is False
    assert created and created[0].released is True


def test_pipeline_survives_failing_camera() -> None:
    np = pytest.importorskip("numpy")

    def factory(camera):
        return FakeCapture(np, fail=True)

    pipeline = CameraPipeline(
        CameraConfig(),
        _inactive_config(),
        lambda event: None,
        engine=NullEngine(),
        capture_factory=factory,
    )
    pipeline.start()
    try:
        deadline = time.time() + 2.0
        status = pipeline.status()
        while time.time() < deadline:
            status = pipeline.status()
            if status["camera_ok"] is False and status["last_error"]:
                break
            time.sleep(0.01)

        assert status["camera_ok"] is False
        assert status["last_error"] == "cámara no disponible"
        # El hilo sigue vivo tras el fallo de cámara.
        assert status["running"] is True
        assert pipeline.wait_first_frame(0.05) is False
    finally:
        pipeline.stop()

    assert pipeline.status()["running"] is False


def test_pipeline_stop_is_idempotent() -> None:
    np = pytest.importorskip("numpy")

    pipeline = CameraPipeline(
        CameraConfig(),
        _inactive_config(),
        lambda event: None,
        engine=NullEngine(),
        capture_factory=lambda camera: FakeCapture(np),
    )
    pipeline.stop()
    pipeline.stop()
    assert pipeline.status()["running"] is False


class FakeClock:
    """Reloj monotónico controlado por el test."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class CountingEngine(NullEngine):
    """NullEngine que cuenta cuántas veces se ejecutó la inferencia."""

    def __init__(self) -> None:
        self.calls = 0

    def process(self, frame_bgr, ts: float | None = None):
        self.calls += 1
        return super().process(frame_bgr, ts)


def test_pipeline_throttles_detection_and_reports_process_fps() -> None:
    np = pytest.importorskip("numpy")
    clock = FakeClock()
    engine = CountingEngine()
    config = _inactive_config().model_copy(update={"detection_fps": 10})
    pipeline = CameraPipeline(
        CameraConfig(),
        config,
        lambda event: None,
        engine=engine,
        clock=clock,
    )
    frame = np.zeros((48, 64, 3), dtype=np.uint8)

    for moment in (0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3):
        clock.now = moment
        pipeline._handle_frame(frame, engine)

    status = pipeline.status()
    assert status["detection_fps"] == 10
    # Todos los frames se capturan; solo 4 pasan el throttle de 10 fps.
    assert status["frames"] == 7
    assert status["processed_frames"] == 4
    assert engine.calls == 4
    assert status["process_fps"] > 0.0
    # En frames salteados la vista sigue teniendo el frame crudo.
    assert pipeline.snapshot() is not None


class CloseCountingEngine(NullEngine):
    """NullEngine que registra instancias y si fueron cerradas."""

    instances: list[CloseCountingEngine] = []

    def __init__(self, *args, **kwargs) -> None:
        self.closed = False
        type(self).instances.append(self)

    def close(self) -> None:
        self.closed = True


def _wait_and_stop(pipeline: CameraPipeline) -> None:
    try:
        assert pipeline.wait_first_frame(2.0) is True
    finally:
        pipeline.stop()


def test_stop_closes_and_recreates_owned_engine(monkeypatch) -> None:
    np = pytest.importorskip("numpy")
    CloseCountingEngine.instances = []
    monkeypatch.setattr(pipeline_module, "MediaPipeEngine", CloseCountingEngine)

    pipeline = CameraPipeline(
        CameraConfig(),
        _inactive_config(),
        lambda event: None,
        engine=None,
        capture_factory=lambda camera: FakeCapture(np),
    )

    pipeline.start()
    _wait_and_stop(pipeline)

    assert len(CloseCountingEngine.instances) == 1
    assert CloseCountingEngine.instances[0].closed is True
    assert pipeline._engine is None

    pipeline.start()
    _wait_and_stop(pipeline)

    assert len(CloseCountingEngine.instances) == 2
    assert CloseCountingEngine.instances[1].closed is True


def test_stop_does_not_close_injected_engine() -> None:
    np = pytest.importorskip("numpy")
    engine = CloseCountingEngine()
    pipeline = CameraPipeline(
        CameraConfig(),
        _inactive_config(),
        lambda event: None,
        engine=engine,
        capture_factory=lambda camera: FakeCapture(np),
    )

    pipeline.start()
    _wait_and_stop(pipeline)

    assert engine.closed is False
    assert pipeline._engine is engine
