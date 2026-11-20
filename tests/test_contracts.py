"""Pruebas de validación de los contratos de percepción."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from exp_brazo.contracts import (
    JOINT_NAMES,
    PHYSICAL_JOINT_ENVELOPE,
    Behavior,
    BehaviorStep,
    CameraConfig,
    DashboardConfig,
    Gesture,
    GestureSettings,
    PerceptionConfig,
    RobotConfig,
    SupervisorConfig,
)


def _robot(**overrides) -> RobotConfig:
    payload = {
        "ip": "192.168.5.1",
        "joint_limits": {joint: PHYSICAL_JOINT_ENVELOPE[joint] for joint in JOINT_NAMES},
        "home_joints": [0.0] * len(JOINT_NAMES),
    }
    payload.update(overrides)
    return RobotConfig(**payload)


def _config(**overrides) -> PerceptionConfig:
    gestures = {gesture: GestureSettings(enabled=False) for gesture in Gesture}
    return PerceptionConfig(gestures=gestures, **overrides)


def test_camera_config_defaults() -> None:
    config = CameraConfig()

    assert config.index == 0
    assert config.width == 640
    assert config.height == 480
    assert config.fps == 30
    assert config.fourcc == "MJPG"


def test_perception_performance_defaults() -> None:
    config = _config()

    assert config.pose_model_complexity == 1
    assert config.hand_model_complexity == 0
    assert config.process_max_width == 640
    assert config.detection_fps == 15


def test_perception_accepts_valid_performance_values() -> None:
    config = _config(
        pose_model_complexity=1,
        hand_model_complexity=1,
        process_max_width=1920,
        detection_fps=60,
    )

    assert config.pose_model_complexity == 1
    assert config.hand_model_complexity == 1
    assert config.process_max_width == 1920
    assert config.detection_fps == 60


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pose_model_complexity", -1),
        ("pose_model_complexity", 2),
        ("hand_model_complexity", -1),
        ("hand_model_complexity", 2),
        ("process_max_width", 319),
        ("process_max_width", 1921),
        ("detection_fps", 0),
        ("detection_fps", 61),
    ],
)
def test_perception_rejects_out_of_range_values(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        _config(**{field: value})


def test_robot_config_accepts_valid_home_and_envelope() -> None:
    config = _robot(home_joints=[0, 10, -10, 0, 0, 0])

    assert config.home_joints == [0, 10, -10, 0, 0, 0]


def test_robot_config_rejects_invalid_ip() -> None:
    with pytest.raises(ValidationError, match="IP inválida"):
        _robot(ip="no-es-una-ip")


@pytest.mark.parametrize("port_field", ["dashboard_port", "move_port", "feedback_port"])
@pytest.mark.parametrize("port", [0, 65536])
def test_robot_config_rejects_ports_out_of_range(port_field: str, port: int) -> None:
    with pytest.raises(ValidationError):
        _robot(**{port_field: port})


def test_robot_config_rejects_invalid_connect_timeout() -> None:
    with pytest.raises(ValidationError):
        _robot(connect_timeout_s=0)


def test_robot_config_rejects_partial_joint_limits() -> None:
    with pytest.raises(ValidationError, match="J1..J6"):
        _robot(joint_limits={"J1": (-180, 180)})


def test_robot_config_rejects_envelope_expansion() -> None:
    limits = {joint: PHYSICAL_JOINT_ENVELOPE[joint] for joint in JOINT_NAMES}
    limits["J1"] = (-200, 200)

    with pytest.raises(ValidationError, match="envelope"):
        _robot(joint_limits=limits)


def test_robot_config_rejects_home_outside_joint_limits() -> None:
    limits = {joint: PHYSICAL_JOINT_ENVELOPE[joint] for joint in JOINT_NAMES}
    limits["J3"] = (-90, 90)

    with pytest.raises(ValidationError, match="home_joints"):
        _robot(joint_limits=limits, home_joints=[0, 0, 120, 0, 0, 0])


def test_robot_config_rejects_home_outside_envelope() -> None:
    with pytest.raises(ValidationError, match="home_joints"):
        _robot(home_joints=[0, 0, 200, 0, 0, 0])


@pytest.mark.parametrize("behavior_id", ["=cmd", "bad/id"])
def test_behavior_id_rejects_path_characters(behavior_id: str) -> None:
    with pytest.raises(ValidationError):
        Behavior(
            id=behavior_id,
            trigger_gesture=Gesture.HOLA,
            name="inválido",
            speed_pct=10,
            acceleration_pct=10,
            steps=[BehaviorStep(deltas={"J1": 1.0}, duration_ms=0, pause_ms=0)],
        )


@pytest.mark.parametrize("behavior_id", ["seis_siete", "ambiental"])
def test_behavior_id_accepts_known_ids(behavior_id: str) -> None:
    behavior = Behavior(
        id=behavior_id,
        trigger_gesture=Gesture.HOLA,
        name="válido",
        speed_pct=10,
        acceleration_pct=10,
        steps=[BehaviorStep(deltas={"J1": 1.0}, duration_ms=0, pause_ms=0)],
    )

    assert behavior.id == behavior_id


def _behavior_payload(**overrides) -> dict:
    payload = {
        "id": "hola",
        "trigger_gesture": Gesture.HOLA,
        "name": "Saludo",
        "description": "SIN VALIDAR",
        "speed_pct": 10,
        "acceleration_pct": 10,
        "priority": 0,
        "cooldown_s": 0.0,
        "steps": [{"deltas": {"J1": 1.0}, "duration_ms": 100, "pause_ms": 0}],
    }
    payload.update(overrides)
    return payload


def test_behavior_step_accepts_and_rejects_time_limits() -> None:
    step = BehaviorStep(deltas={"J1": 1.0}, duration_ms=60_000, pause_ms=60_000)
    assert step.duration_ms == 60_000
    assert step.pause_ms == 60_000

    with pytest.raises(ValidationError):
        BehaviorStep(deltas={"J1": 1.0}, duration_ms=60_001, pause_ms=0)
    with pytest.raises(ValidationError):
        BehaviorStep(deltas={"J1": 1.0}, duration_ms=0, pause_ms=60_001)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "x" * 121),
        ("description", "x" * 1001),
        ("priority", 1001),
        ("priority", -1001),
        ("cooldown_s", 3600.1),
    ],
)
def test_behavior_rejects_out_of_range_fields(field: str, value) -> None:
    with pytest.raises(ValidationError):
        Behavior.model_validate(_behavior_payload(**{field: value}))


def test_behavior_rejects_empty_or_excessive_steps() -> None:
    with pytest.raises(ValidationError):
        Behavior.model_validate(_behavior_payload(steps=[]))

    too_many = [{"deltas": {"J1": 1.0}, "duration_ms": 1, "pause_ms": 0}] * 101
    with pytest.raises(ValidationError):
        Behavior.model_validate(_behavior_payload(steps=too_many))


def test_behavior_accepts_field_limits() -> None:
    steps = [{"deltas": {"J1": 1.0}, "duration_ms": 1, "pause_ms": 0}] * 100
    behavior = Behavior.model_validate(
        _behavior_payload(
            name="x" * 120,
            description="x" * 1000,
            priority=1000,
            cooldown_s=3600,
            steps=steps,
        )
    )

    assert behavior.name == "x" * 120
    assert len(behavior.steps) == 100
    assert behavior.priority == 1000
    assert behavior.cooldown_s == 3600


def test_supervisor_config_accepts_and_rejects_limits() -> None:
    config = SupervisorConfig(
        heartbeat_timeout_s=1,
        ambient_interval_s=1,
        restart_backoff_s=[0, 60],
    )
    assert config.heartbeat_timeout_s == 1
    assert config.ambient_interval_s == 1

    config = SupervisorConfig(
        heartbeat_timeout_s=60,
        ambient_interval_s=3600,
        restart_backoff_s=[],
    )
    assert config.heartbeat_timeout_s == 60
    assert config.ambient_interval_s == 3600

    for overrides in (
        {"heartbeat_timeout_s": 0.9},
        {"heartbeat_timeout_s": 60.1},
        {"ambient_interval_s": 0.9},
        {"ambient_interval_s": 3600.1},
        {"restart_backoff_s": [-0.1]},
        {"restart_backoff_s": [60.1]},
        {"restart_backoff_s": [1] * 21},
    ):
        with pytest.raises(ValidationError):
            SupervisorConfig(**overrides)


def test_dashboard_log_max_entries_limits() -> None:
    assert DashboardConfig(log_max_entries=10).log_max_entries == 10
    assert DashboardConfig(log_max_entries=10_000).log_max_entries == 10_000

    for value in (9, 10_001):
        with pytest.raises(ValidationError):
            DashboardConfig(log_max_entries=value)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "127.10.20.30",
        "::1",
        "169.254.1.1",
        "fe80::1",
        "0.0.0.0",
        "::",
        "224.0.0.1",
        "ff02::1",
    ],
)
def test_robot_config_rejects_unsafe_dobot_ips(ip: str) -> None:
    with pytest.raises(ValidationError, match="no puede ser"):
        _robot(adapter="dobot", ip=ip)


def test_robot_config_allows_private_dobot_ip_and_mock_loopback() -> None:
    assert _robot(adapter="dobot", ip="192.168.5.1").ip == "192.168.5.1"
    assert _robot(adapter="dobot", ip="10.0.0.5").ip == "10.0.0.5"
    assert _robot(adapter="mock", ip="127.0.0.1").ip == "127.0.0.1"
