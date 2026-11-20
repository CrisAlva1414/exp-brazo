"""Pruebas del DobotAdapter sin hardware ni red real."""

from __future__ import annotations

import numpy as np
import pytest

from exp_brazo.contracts import Behavior, BehaviorStep, Gesture, RobotState
from exp_brazo.robot.dobot import DobotAdapter, DobotError, MyType


class FakeSocket:
    """Socket mínimo: encola respuestas de texto y registra lo enviado."""

    def __init__(self, responses: list | None = None) -> None:
        self.responses = list(responses or [])
        self.sent: list[str] = []
        self.timeout = None
        self.closed = False
        self.raise_on_recv = False

    def sendall(self, data: bytes) -> None:
        self.sent.append(data.decode("utf-8"))

    def recv(self, size: int) -> bytes:
        if self.raise_on_recv:
            raise OSError("socket simulado roto")
        if not self.responses:
            raise TimeoutError("sin más respuestas simuladas")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, bytes):
            return item
        return str(item).encode("utf-8")

    def settimeout(self, timeout) -> None:
        self.timeout = timeout

    def gettimeout(self):
        return self.timeout

    def close(self) -> None:
        self.closed = True


class FakeFactory:
    """Devuelve sockets falsos según el puerto solicitado."""

    def __init__(self, robot_cfg, dashboard: FakeSocket, feedback: FakeSocket) -> None:
        self.robot_cfg = robot_cfg
        self.dashboard = dashboard
        self.feedback = feedback
        self.calls: list[tuple] = []

    def __call__(self, address, timeout):
        self.calls.append((address, timeout))
        _ip, port = address
        if port == self.robot_cfg.dashboard_port:
            return self.dashboard
        if port == self.robot_cfg.feedback_port:
            return self.feedback
        raise AssertionError(f"puerto inesperado: {port}")


def _make_adapter(robot_cfg, responses: list, *, clock=None):
    dashboard = FakeSocket(responses)
    feedback = FakeSocket()
    factory = FakeFactory(robot_cfg, dashboard, feedback)
    adapter = DobotAdapter(
        robot_cfg,
        socket_factory=factory,
        start_feedback_thread=False,
        clock=clock or (lambda: 1000.0),
    )
    return adapter, dashboard, feedback, factory


def _behavior(*, j1: float = 5.0, speed: int = 10) -> Behavior:
    return Behavior(
        id="saludo_prueba",
        trigger_gesture=Gesture.HOLA,
        name="Saludo de prueba",
        speed_pct=speed,
        acceleration_pct=10,
        steps=[BehaviorStep(deltas={"J1": j1}, duration_ms=100, pause_ms=0)],
    )


def _enable_ok() -> str:
    return "0,{1},EnableRobot();"


def _movj_ok(command_id: int = 2) -> str:
    return f"0,{{{command_id}}},MovJ(joint={{0,0,0,0,0,0}},a=10,v=10);"


def test_feedback_dtype_has_documented_size() -> None:
    assert MyType.itemsize == 1440


def test_parse_rejects_unrecognized_response(robot_cfg) -> None:
    adapter, *_ = _make_adapter(robot_cfg, [])

    with pytest.raises(DobotError, match="no reconocida"):
        adapter._parse("basura")


def test_parse_extracts_error_and_command_ids(robot_cfg) -> None:
    adapter, *_ = _make_adapter(robot_cfg, [])

    assert adapter._parse("0,{5},MovJ(...)") == (0, 5, "0,{5},MovJ(...)")
    assert adapter._parse("0,{0},X()") == (0, 0, "0,{0},X()")


def test_move_joint_rejects_zero_command_id_without_waiting(robot_cfg) -> None:
    adapter, dashboard, *_ = _make_adapter(
        robot_cfg,
        [_enable_ok(), "0,{0},MovJ(joint={0,0,0,0,0,0},a=10,v=10);"],
    )

    try:
        assert adapter.connect() is True
        with pytest.raises(DobotError, match="CommandID"):
            adapter._move_joint([0.0] * 6, 10, 10, timeout_s=0.1)
        assert "GetCurrentCommandID()" not in dashboard.sent
    finally:
        adapter.disconnect()


def test_wait_for_command_accepts_dashboard_fallback(robot_cfg) -> None:
    adapter, dashboard, *_ = _make_adapter(
        robot_cfg,
        [_enable_ok(), "0,{5},GetCurrentCommandID()"],
    )

    try:
        assert adapter.connect() is True
        assert adapter._wait_for_command(5, 1.0) is True
        assert "GetCurrentCommandID()" in dashboard.sent
    finally:
        adapter.disconnect()


def test_repeated_feedback_errors_force_fault_until_reconnect(robot_cfg) -> None:
    adapter, *_ = _make_adapter(robot_cfg, [_enable_ok()])

    try:
        assert adapter.connect() is True
        for _ in range(3):
            adapter._set_feedback_error("paquete inválido")

        assert adapter.state == RobotState.FAULT
        assert "feedback perdido" in (adapter.last_error or "")

        adapter.inject_feedback({"robot_mode": 5, "current_command_id": 1})
        assert adapter._feedback_ok is True
        assert adapter.state == RobotState.FAULT
    finally:
        adapter.disconnect()


def test_status_with_fresh_feedback_uses_cache(robot_cfg, monkeypatch) -> None:
    adapter, *_ = _make_adapter(robot_cfg, [_enable_ok()], clock=lambda: 1000.0)

    try:
        assert adapter.connect() is True
        adapter.inject_feedback(
            {
                "robot_mode": 5,
                "current_command_id": 9,
                "q_actual": [1, 2, 3, 4, 5, 6],
                "ts": 1000.0,
            }
        )

        def _fail(*_args, **_kwargs):
            raise AssertionError("status no debe consultar el dashboard con feedback fresco")

        monkeypatch.setattr(adapter, "_query", _fail)

        status = adapter.status()
        assert status["robot_mode"] == 5
        assert status["current_command_id"] == 9
        assert status["angles"] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    finally:
        adapter.disconnect()


def test_connect_ok_sets_idle(robot_cfg) -> None:
    adapter, dashboard, _feedback, factory = _make_adapter(robot_cfg, [_enable_ok()])

    try:
        assert adapter.connect() is True
        assert adapter.state == RobotState.IDLE
        assert adapter.last_error is None
        assert dashboard.sent == ["EnableRobot()"]
        assert [call[0][1] for call in factory.calls] == [
            robot_cfg.dashboard_port,
            robot_cfg.feedback_port,
        ]
    finally:
        adapter.disconnect()


def test_connect_not_tcp_sets_fault(robot_cfg) -> None:
    adapter, _dashboard, _feedback, _factory = _make_adapter(robot_cfg, ["Not Tcp"])

    assert adapter.connect() is False
    assert adapter.state == RobotState.FAULT
    assert "TCP/IP" in (adapter.last_error or "")


def test_execute_behavior_sends_delta_and_returns_home(robot_cfg) -> None:
    adapter, dashboard, _feedback, _factory = _make_adapter(
        robot_cfg,
        [_enable_ok(), _movj_ok(2), _movj_ok(2)],
    )

    try:
        assert adapter.connect() is True
        adapter.inject_feedback({"robot_mode": 5, "current_command_id": 2})

        result = adapter.execute_behavior(_behavior(j1=5.0), "evento-1")

        assert result.ok is True
        assert result.steps_executed == 1
        assert adapter.state == RobotState.IDLE

        movj = [cmd for cmd in dashboard.sent if cmd.startswith("MovJ")]
        assert len(movj) == 2
        assert "joint={5.000000,0.000000,0.000000,0.000000,0.000000,0.000000}" in movj[0]
        assert "a=10,v=10" in movj[0]
        assert "joint={0.000000,0.000000,0.000000,0.000000,0.000000,0.000000}" in movj[-1]
    finally:
        adapter.disconnect()


def test_behavior_out_of_limits_sends_no_movj(robot_cfg) -> None:
    adapter, dashboard, _feedback, _factory = _make_adapter(robot_cfg, [_enable_ok()])

    try:
        assert adapter.connect() is True
        result = adapter.execute_behavior(_behavior(j1=1000.0), "evento-malo")

        assert result.ok is False
        assert "excede el límite" in (result.error or "")
        assert all(not cmd.startswith("MovJ") for cmd in dashboard.sent)
        assert adapter.state == RobotState.IDLE
    finally:
        adapter.disconnect()


def test_move_joint_rejects_out_of_envelope_before_sending(robot_cfg) -> None:
    adapter, dashboard, _feedback, _factory = _make_adapter(robot_cfg, [_enable_ok()])

    try:
        assert adapter.connect() is True
        with pytest.raises(DobotError, match="MovJ"):
            adapter._move_joint([999, 0, 0, 0, 0, 0], 10, 10, timeout_s=0.1)

        assert all(not cmd.startswith("MovJ") for cmd in dashboard.sent)
        assert all(
            not entry["cmd"].startswith("MovJ") for entry in adapter.command_log
        )
    finally:
        adapter.disconnect()


def test_dedupe_same_event_id(robot_cfg) -> None:
    adapter, dashboard, _feedback, _factory = _make_adapter(
        robot_cfg,
        [_enable_ok(), _movj_ok(2), _movj_ok(2)],
        clock=lambda: 1000.0,
    )

    try:
        assert adapter.connect() is True
        adapter.inject_feedback({"robot_mode": 5, "current_command_id": 2})

        first = adapter.execute_behavior(_behavior(), "mismo-evento")
        second = adapter.execute_behavior(_behavior(), "mismo-evento")

        assert first.ok is True
        assert first.deduplicated is False
        assert second.ok is True
        assert second.deduplicated is True
        assert second.steps_executed == 0
        assert len([cmd for cmd in dashboard.sent if cmd.startswith("MovJ")]) == 2
    finally:
        adapter.disconnect()


def test_movj_error_sets_fault(robot_cfg) -> None:
    adapter, _dashboard, _feedback, _factory = _make_adapter(
        robot_cfg,
        [_enable_ok(), "1,{2},MovJ(joint={0,0,0,0,0,0},a=10,v=10);"],
    )

    try:
        assert adapter.connect() is True
        adapter.inject_feedback({"robot_mode": 5, "current_command_id": 2})

        result = adapter.execute_behavior(_behavior(), "evento-error")

        assert result.ok is False
        assert "ErrorID=1" in (result.error or "")
        assert adapter.state == RobotState.FAULT
        assert "ErrorID=1" in (adapter.last_error or "")
    finally:
        adapter.disconnect()


def test_status_does_not_raise_when_sockets_fail(robot_cfg) -> None:
    adapter, dashboard, _feedback, _factory = _make_adapter(robot_cfg, [_enable_ok()])

    try:
        assert adapter.connect() is True
        dashboard.raise_on_recv = True

        status = adapter.status()

        assert status["name"] == "dobot"
        assert "state" in status
        assert "last_error" in status
    finally:
        adapter.disconnect()


def test_home_sends_home_and_returns_idle(robot_cfg) -> None:
    adapter, dashboard, _feedback, _factory = _make_adapter(
        robot_cfg,
        [_enable_ok(), _movj_ok(2)],
    )

    try:
        assert adapter.connect() is True
        adapter.inject_feedback({"robot_mode": 5, "current_command_id": 2})

        result = adapter.home()

        assert result.ok is True
        assert result.behavior_id == "__home__"
        assert adapter.state == RobotState.IDLE

        movj = [cmd for cmd in dashboard.sent if cmd.startswith("MovJ")]
        assert len(movj) == 1
        assert "joint={0.000000,0.000000,0.000000,0.000000,0.000000,0.000000}" in movj[0]
        assert "a=30,v=10" in movj[0]
    finally:
        adapter.disconnect()


def test_feedback_packet_is_built_with_documented_dtype() -> None:
    packet = np.zeros(1, dtype=MyType)
    packet["TestValue"] = 0x123456789ABCDEF
    packet["RobotMode"] = 5
    packet["CurrentCommandId"] = 7

    raw = packet.tobytes()
    parsed = np.frombuffer(raw, dtype=MyType)

    assert len(raw) == 1440
    assert int(parsed["TestValue"][0]) == 0x123456789ABCDEF
    assert int(parsed["RobotMode"][0]) == 5
    assert int(parsed["CurrentCommandId"][0]) == 7


def test_move_joint_refuses_when_feedback_is_lost(robot_cfg) -> None:
    adapter, dashboard, _feedback, _factory = _make_adapter(robot_cfg, [_enable_ok()])

    try:
        assert adapter.connect() is True
        for _ in range(3):
            adapter._set_feedback_error("paquete inválido")
        assert adapter.state == RobotState.FAULT

        sent_before = list(dashboard.sent)
        with pytest.raises(DobotError, match="FAULT"):
            adapter._move_joint([0.0] * 6, 10, 10, timeout_s=0.1)

        assert dashboard.sent == sent_before
        assert all(not entry["cmd"].startswith("MovJ") for entry in adapter.command_log)
    finally:
        adapter.disconnect()


def test_execute_behavior_aborts_when_feedback_is_lost(
    robot_cfg, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, dashboard, _feedback, _factory = _make_adapter(
        robot_cfg,
        [_enable_ok(), _movj_ok(2)],
    )
    behavior = Behavior(
        id="dos_pasos",
        trigger_gesture=Gesture.HOLA,
        name="Dos pasos",
        speed_pct=10,
        acceleration_pct=10,
        steps=[
            BehaviorStep(deltas={"J1": 1.0}, duration_ms=100, pause_ms=0),
            BehaviorStep(deltas={"J1": 2.0}, duration_ms=100, pause_ms=0),
        ],
    )

    try:
        assert adapter.connect() is True
        adapter.inject_feedback({"robot_mode": 5, "current_command_id": 2})

        real_move = adapter._move_joint
        executed_targets: list[list[float]] = []

        def move_then_lose_feedback(target, *args, **kwargs):
            result = real_move(target, *args, **kwargs)
            executed_targets.append(list(target))
            if len(executed_targets) == 1:
                for _ in range(3):
                    adapter._set_feedback_error("paquete inválido")
            return result

        monkeypatch.setattr(adapter, "_move_joint", move_then_lose_feedback)

        result = adapter.execute_behavior(behavior, "evento-feedback-perdido")

        assert result.ok is False
        assert "feedback perdido" in (result.error or "")
        assert result.steps_executed == 1
        assert adapter.state == RobotState.FAULT
        movj = [cmd for cmd in dashboard.sent if cmd.startswith("MovJ")]
        assert len(movj) == 1
    finally:
        adapter.disconnect()
