"""Pruebas del diagnóstico guiado con un runtime mock sin cámara."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from exp_brazo.robot.mock import MockAdapter
from exp_brazo.runtime import Runtime
from exp_brazo.troubleshooting import Troubleshooting


@pytest.fixture
def runtime(tmp_path: Path, repo_root: Path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    behaviors_path = config_dir / "behaviors.yaml"
    shutil.copy(repo_root / "config" / "config.yaml", config_path)
    shutil.copy(repo_root / "config" / "behaviors.yaml", behaviors_path)

    instance = Runtime(
        config_path=config_path,
        behaviors_path=behaviors_path,
        force_adapter="mock",
        camera_enabled=False,
        execute_inline=True,
        adapter_factory=lambda robot_config: MockAdapter(robot_config, time_scale=0),
    )
    # Evita depender de la red del brazo durante la suite.
    instance.config.robot.ip = "127.0.0.1"
    try:
        yield instance
    finally:
        instance.stop()


def test_run_all_returns_expected_checks(runtime: Runtime) -> None:
    results = Troubleshooting(runtime).run_all()

    ids = [item["id"] for item in results]
    assert ids == [
        "camera",
        "light_framing",
        "controller_ping",
        "controller_ports",
        "robot_state",
        "mediapipe_models",
        "performance",
        "config",
    ]
    for item in results:
        assert set(item) == {
            "id",
            "name",
            "ok",
            "detail",
            "probable_cause",
            "suggested_action",
            "duration_ms",
        }
        assert item["ok"] in (True, False, None)
        assert isinstance(item["detail"], str)
        assert item["duration_ms"] >= 0


def test_mediapipe_check_uses_vendored_task_models(runtime: Runtime) -> None:
    # No depende de que mediapipe/cv2 estén en el intérprete que corre la
    # suite: el chequeo debe reportar los modelos .task vendorizados igual.
    result = Troubleshooting(runtime)._check_mediapipe()

    assert ".task" in result["detail"]
    assert "vendorizados" in result["detail"]
    assert "legacy" not in result["detail"].lower()


def test_mock_adapter_and_config_are_reported(runtime: Runtime) -> None:
    results = {item["id"]: item for item in Troubleshooting(runtime).run_all()}

    assert results["robot_state"]["ok"] is None
    assert "mock" in results["robot_state"]["detail"].lower()
    assert results["config"]["ok"] is True


def test_ping_uses_double_dash_and_no_shell(runtime: Runtime, monkeypatch) -> None:
    calls: list[tuple[list[str], dict]] = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("exp_brazo.troubleshooting.shutil.which", lambda _name: "/usr/bin/ping")
    monkeypatch.setattr("exp_brazo.troubleshooting.subprocess.run", fake_run)

    result = Troubleshooting(runtime)._check_controller_ping()

    assert result["ok"] is True
    assert calls
    args, kwargs = calls[0]
    assert "--" in args
    assert args.index("--") < args.index("127.0.0.1")
    assert kwargs.get("shell") is not True


def test_ping_rejects_invalid_ip(runtime: Runtime) -> None:
    original = runtime.config.robot.ip
    runtime.config.robot.ip = "not-an-ip"
    try:
        result = Troubleshooting(runtime)._check_controller_ping()
    finally:
        runtime.config.robot.ip = original

    assert result["ok"] is False
    assert "inválida" in result["detail"].lower()


def test_to_text_contains_report_header(runtime: Runtime) -> None:
    runner = Troubleshooting(runtime)
    text = runner.to_text()

    assert "REPORTE" in text
    assert "camera" in text
    assert "Acción sugerida" in text or "OK" in text


class FakePipeline:
    """Pipeline falso que ya tiene la cámara abierta y publica un frame."""

    def __init__(self, frame) -> None:
        self._frame = frame
        self.stopped = False
        self.status_calls = 0

    def status(self) -> dict:
        self.status_calls += 1
        return {
            "camera_ok": True,
            "running": True,
            "fps": 12.0,
            "latency_ms": 80.0,
            "frames": 42,
            "last_error": None,
            "heartbeat_age_s": 0.1,
            "gestures": {},
        }

    def snapshot(self):
        return self._frame

    def get_jpeg(self) -> bytes:
        return b"jpeg"

    def stop(self) -> None:
        self.stopped = True


def test_camera_check_does_not_false_negative_when_pipeline_owns_camera(
    runtime: Runtime,
) -> None:
    np = pytest.importorskip("numpy")
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    runtime.pipeline = FakePipeline(frame)
    try:
        results = {item["id"]: item for item in Troubleshooting(runtime).run_all()}
    finally:
        runtime.pipeline = None

    camera = results["camera"]
    assert camera["ok"] is True
    assert "en uso por el sistema" in camera["detail"]
    assert "frames recientes" in camera["detail"]
