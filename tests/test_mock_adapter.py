"""Pruebas del adaptador MockAdapter."""

from __future__ import annotations

from exp_brazo.contracts import Behavior, BehaviorStep, Gesture, RobotState
from exp_brazo.robot.mock import MockAdapter


def _bad_behavior() -> Behavior:
    return Behavior(
        id="fuera_de_limite",
        trigger_gesture=Gesture.NO,
        name="Fuera de límite",
        speed_pct=10,
        acceleration_pct=10,
        steps=[BehaviorStep(deltas={"J1": 1000.0}, duration_ms=10, pause_ms=0)],
    )


def test_execute_records_history(robot_cfg, catalog):
    adapter = MockAdapter(robot_cfg, time_scale=0, clock=lambda: 100.0)
    behavior = catalog[0]

    assert adapter.connect() is True
    result = adapter.execute_behavior(behavior, "evento-1")

    assert result.ok is True
    assert result.steps_executed == len(behavior.steps)
    assert adapter.state == RobotState.IDLE
    assert len(adapter.history) == 1
    entry = adapter.history[0]
    assert entry["behavior_id"] == behavior.id
    assert entry["event_id"] == "evento-1"
    assert entry["steps"] == len(behavior.steps)


def test_execute_rejects_limit_violation(robot_cfg):
    adapter = MockAdapter(robot_cfg, time_scale=0, clock=lambda: 100.0)
    adapter.connect()

    result = adapter.execute_behavior(_bad_behavior(), "evento-malo")

    assert result.ok is False
    assert "excede el límite" in (result.error or "")
    assert adapter.history == []
    assert adapter.state == RobotState.IDLE


def test_execute_rejects_speed_and_acceleration_above_robot_max(robot_cfg):
    behavior = Behavior(
        id="demasiado_rapido",
        trigger_gesture=Gesture.HOLA,
        name="Demasiado rápido",
        speed_pct=robot_cfg.max_speed_pct + 1,
        acceleration_pct=robot_cfg.max_accel_pct + 1,
        steps=[BehaviorStep(deltas={"J1": 1.0}, duration_ms=10, pause_ms=0)],
    )
    adapter = MockAdapter(robot_cfg, time_scale=0, clock=lambda: 100.0)
    adapter.connect()

    result = adapter.execute_behavior(behavior, "evento-rapido")

    assert result.ok is False
    assert "speed_pct" in (result.error or "")
    assert "acceleration_pct" in (result.error or "")
    assert adapter.history == []
    assert adapter.state == RobotState.IDLE


def test_dedupe_same_event_id(robot_cfg, catalog):
    adapter = MockAdapter(robot_cfg, time_scale=0, clock=lambda: 1000.0)
    adapter.connect()
    behavior = catalog[0]

    first = adapter.execute_behavior(behavior, "mismo-evento")
    second = adapter.execute_behavior(behavior, "mismo-evento")

    assert first.ok is True
    assert first.deduplicated is False
    assert second.ok is True
    assert second.deduplicated is True
    assert second.steps_executed == 0
    assert len(adapter.history) == 1


def test_injected_failure_sets_fault_and_clear_fault(robot_cfg, catalog):
    adapter = MockAdapter(robot_cfg, time_scale=0, fail_on_execute=True)
    adapter.connect()
    behavior = catalog[0]

    result = adapter.execute_behavior(behavior, "evento-fallo")
    assert result.ok is False
    assert result.error == "fallo inyectado"
    assert adapter.state == RobotState.FAULT

    blocked = adapter.execute_behavior(behavior, "otro-evento")
    assert blocked.ok is False
    assert "adaptador no disponible" in (blocked.error or "")

    adapter.clear_fault()
    adapter.fail_on_execute = False
    assert adapter.state == RobotState.IDLE
    assert adapter.execute_behavior(behavior, "evento-recuperado").ok is True


def test_home_records_history(robot_cfg):
    adapter = MockAdapter(robot_cfg, time_scale=0, clock=lambda: 50.0)
    adapter.connect()

    result = adapter.home()

    assert result.ok is True
    assert adapter.state == RobotState.IDLE
    assert len(adapter.history) == 1
    assert adapter.history[-1]["behavior_id"] == "__home__"


def test_home_rejects_invalid_home_joints(robot_cfg):
    adapter = MockAdapter(robot_cfg, time_scale=0, clock=lambda: 50.0)
    adapter.connect()
    original = robot_cfg.home_joints[0]
    try:
        robot_cfg.home_joints[0] = 999.0

        result = adapter.home()

        assert result.ok is False
        assert "límite" in (result.error or "")
        assert adapter.history == []
    finally:
        robot_cfg.home_joints[0] = original


def test_status_shape(robot_cfg, catalog):
    adapter = MockAdapter(robot_cfg, time_scale=0)
    adapter.connect()
    adapter.execute_behavior(catalog[0], "status-evento")

    status = adapter.status()

    assert status["name"] == "mock"
    assert status["state"] == RobotState.IDLE.value
    assert status["history_len"] == 1
    assert status["last"]["behavior_id"] == catalog[0].id
