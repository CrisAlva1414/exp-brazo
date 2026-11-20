"""Pruebas del supervisor con reloj y componentes falsos."""

from __future__ import annotations

import time

from exp_brazo.contracts import RobotState, SupervisorConfig
from exp_brazo.supervisor import Supervisor


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def set(self, value: float) -> None:
        self.now = float(value)


class FakePipeline:
    def __init__(self, *, camera_ok: bool = False, running: bool = True) -> None:
        self.camera_ok = camera_ok
        self.running = running
        self.restart_calls = 0
        self.status_raises = False

    def status(self) -> dict:
        if self.status_raises:
            raise RuntimeError("pipeline roto")
        return {
            "camera_ok": self.camera_ok,
            "running": self.running,
            "heartbeat_age_s": 0.1,
            "last_error": None if self.camera_ok else "cámara no disponible",
        }

    def restart(self) -> None:
        self.restart_calls += 1


class FakeAdapter:
    def __init__(self, state: RobotState = RobotState.IDLE, *, name: str = "mock") -> None:
        self.state = state
        self.name = name
        self.reconnect_calls = 0
        self.reconnect_attempts: list[int] = []

    def reconnect(self, attempts: int = 0) -> bool:
        self.reconnect_calls += 1
        self.reconnect_attempts.append(attempts)
        self.state = RobotState.IDLE
        return True


class FakeController:
    def __init__(self, *, mode: str = "auto", paused: bool = False) -> None:
        self.mode = mode
        self.paused = paused

    def status(self) -> dict:
        return {"mode": self.mode, "paused": self.paused}


def _supervisor(
    *,
    pipeline: FakePipeline,
    controller: FakeController | None = None,
    adapter: FakeAdapter | None = None,
    heartbeat_timeout_s: float = 5.0,
    on_ambient=None,
    clock: FakeClock | None = None,
) -> tuple[Supervisor, FakeClock, list[int]]:
    fake_clock = clock or FakeClock()
    calls: list[int] = []

    def ambient() -> None:
        calls.append(1)

    supervisor = Supervisor(
        pipeline=pipeline,
        adapter=adapter or FakeAdapter(),
        controller=controller or FakeController(),
        config=SupervisorConfig(
            heartbeat_timeout_s=heartbeat_timeout_s,
            restart_backoff_s=[1, 2, 4, 8, 15],
            ambient_interval_s=20.0,
        ),
        on_ambient=ambient if on_ambient is None else on_ambient,
        clock=fake_clock,
    )
    return supervisor, fake_clock, calls


def test_camera_down_restarts_with_backoff_and_triggers_ambient() -> None:
    pipeline = FakePipeline(camera_ok=False)
    supervisor, clock, ambient_calls = _supervisor(pipeline=pipeline)

    layers = supervisor.force_check()
    assert layers["camera"]["ok"] is False
    assert pipeline.restart_calls == 0

    clock.set(5.1)
    supervisor.force_check()
    assert pipeline.restart_calls == 1

    clock.set(5.5)
    supervisor.force_check()
    assert pipeline.restart_calls == 1

    clock.set(6.1)
    supervisor.force_check()
    assert pipeline.restart_calls == 2

    assert ambient_calls == []

    clock.set(20.1)
    supervisor.force_check()
    assert ambient_calls == [1]

    clock.set(20.5)
    supervisor.force_check()
    assert ambient_calls == [1]

    clock.set(40.2)
    supervisor.force_check()
    assert ambient_calls == [1, 1]


def test_camera_ok_does_not_restart_or_trigger_ambient() -> None:
    pipeline = FakePipeline(camera_ok=True)
    supervisor, clock, ambient_calls = _supervisor(pipeline=pipeline)

    clock.set(100.0)
    layers = supervisor.force_check()

    assert layers["camera"]["ok"] is True
    assert layers["perception"]["ok"] is True
    assert pipeline.restart_calls == 0
    assert ambient_calls == []


def test_manual_mode_does_not_trigger_ambient() -> None:
    pipeline = FakePipeline(camera_ok=False)
    controller = FakeController(mode="manual")
    supervisor, clock, ambient_calls = _supervisor(
        pipeline=pipeline, controller=controller
    )

    clock.set(100.0)
    supervisor.force_check()

    assert ambient_calls == []


def test_status_has_all_layers_and_survives_pipeline_errors() -> None:
    pipeline = FakePipeline(camera_ok=False)
    pipeline.status_raises = True
    supervisor, _, _ = _supervisor(pipeline=pipeline)

    status = supervisor.status()

    for layer in ("camera", "perception", "decision", "adapter", "supervisor"):
        assert layer in status
        assert "ok" in status[layer]
        assert "detail" in status[layer]
    assert "last_error" in status
    assert status["camera"]["ok"] is False
    assert status["adapter_reconnect_count"] == 0
    assert "adapter_reconnecting" in status
    # force_check no debe propagar excepciones del pipeline.
    supervisor.force_check()


def test_dobot_adapter_reconnects_after_timeout() -> None:
    pipeline = FakePipeline(camera_ok=True)
    adapter = FakeAdapter(state=RobotState.FAULT, name="dobot")
    clock = FakeClock()
    supervisor, _clock, _ = _supervisor(
        pipeline=pipeline,
        adapter=adapter,
        heartbeat_timeout_s=1.0,
        clock=clock,
    )

    supervisor.force_check()
    clock.set(2.0)
    supervisor.force_check()

    deadline = time.monotonic() + 2.0
    while adapter.reconnect_calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert adapter.reconnect_calls >= 1
    assert adapter.reconnect_attempts[0] == 1
    assert adapter.state == RobotState.IDLE

    supervisor.force_check()
    assert adapter.reconnect_calls == 1


def test_mock_adapter_is_not_reconnected() -> None:
    pipeline = FakePipeline(camera_ok=True)
    adapter = FakeAdapter(state=RobotState.FAULT, name="mock")
    supervisor, _clock, _ = _supervisor(
        pipeline=pipeline,
        adapter=adapter,
        heartbeat_timeout_s=1.0,
    )

    supervisor.force_check()
    time.sleep(0.1)

    assert adapter.reconnect_calls == 0
