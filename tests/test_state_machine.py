"""Pruebas de decisión y del controlador de ejecución."""

from __future__ import annotations

import threading

from exp_brazo.contracts import (
    Behavior,
    BehaviorStep,
    DecisionConfig,
    Gesture,
    RobotState,
    RunMode,
)
from exp_brazo.robot.mock import MockAdapter
from exp_brazo.state_machine import DecisionEngine, RobotController


def _behavior(
    behavior_id: str,
    *,
    gesture: Gesture = Gesture.HOLA,
    priority: int = 0,
    cooldown_s: float = 0.0,
    enabled: bool = True,
) -> Behavior:
    return Behavior(
        id=behavior_id,
        trigger_gesture=gesture,
        name=behavior_id,
        speed_pct=10,
        acceleration_pct=10,
        priority=priority,
        cooldown_s=cooldown_s,
        enabled=enabled,
        steps=[BehaviorStep(deltas={"J1": 1.0}, duration_ms=0, pause_ms=0)],
    )


def _controller(
    robot_cfg,
    behaviors: list[Behavior],
    *,
    busy_policy: str = "drop",
    adapter: MockAdapter | None = None,
) -> tuple[RobotController, MockAdapter]:
    if adapter is None:
        adapter = MockAdapter(robot_cfg, time_scale=0)
        adapter.connect()
    engine = DecisionEngine(
        behaviors,
        DecisionConfig(anti_repetition=False, busy_policy=busy_policy),
    )
    controller = RobotController(
        adapter,
        engine,
        busy_policy=busy_policy,
        execute_inline=True,
    )
    return controller, adapter


def test_higher_priority_wins(event):
    engine = DecisionEngine(
        [
            _behavior("baja", priority=1),
            _behavior("alta", priority=10),
        ],
        DecisionConfig(anti_repetition=False, busy_policy="drop"),
    )

    decision = engine.decide(event(Gesture.HOLA), now=1.0)

    assert decision.behavior is not None
    assert decision.behavior.id == "alta"
    assert decision.reason == "ok"


def test_cooldown_blocks_immediate_repeat(event):
    engine = DecisionEngine(
        [_behavior("unico", cooldown_s=5.0)],
        DecisionConfig(anti_repetition=False, busy_policy="drop"),
    )

    first = engine.decide(event(Gesture.HOLA), now=100.0)
    second = engine.decide(event(Gesture.HOLA), now=101.0)

    assert first.behavior is not None
    assert second.behavior is None
    assert second.reason == "cooldown"


def test_anti_repetition_alternates_behaviors(event):
    engine = DecisionEngine(
        [
            _behavior("primero", priority=10),
            _behavior("segundo", priority=5),
        ],
        DecisionConfig(anti_repetition=True, busy_policy="drop"),
    )

    first = engine.decide(event(Gesture.HOLA), now=1.0)
    second = engine.decide(event(Gesture.HOLA), now=2.0)
    third = engine.decide(event(Gesture.HOLA), now=3.0)

    assert first.behavior is not None and first.behavior.id == "primero"
    assert second.behavior is not None and second.behavior.id == "segundo"
    assert third.behavior is not None and third.behavior.id == "primero"


def test_busy_adapter_drops_event(robot_cfg, event):
    controller, adapter = _controller(robot_cfg, [_behavior("hola")])
    adapter.state = RobotState.BUSY

    record = controller.submit_event(event(Gesture.HOLA))

    assert record["result"] == "dropped_busy"
    assert adapter.history == []


def test_duplicate_event_id_is_deduplicated(robot_cfg, event):
    controller, adapter = _controller(robot_cfg, [_behavior("hola")])
    duplicate = event(Gesture.HOLA, event_id="evento-repetido")

    first = controller.submit_event(duplicate)
    second = controller.submit_event(duplicate)

    assert first["result"] == "executed"
    assert second["result"] == "deduplicated"
    assert len(adapter.history) == 1


def test_paused_does_not_execute(robot_cfg, event):
    controller, adapter = _controller(robot_cfg, [_behavior("hola")])
    controller.set_paused(True)

    record = controller.submit_event(event(Gesture.HOLA))

    assert record["result"] == "would_have_done"
    assert record["decision"] == "paused"
    assert adapter.history == []


def test_adapter_fault_reports_would_have_done(robot_cfg, event):
    controller, adapter = _controller(robot_cfg, [_behavior("hola")])
    adapter.inject_fault()

    record = controller.submit_event(event(Gesture.HOLA))

    assert record["result"] == "would_have_done"
    assert "adaptador fault" in (record["error"] or "")
    assert adapter.history == []


def test_manual_trigger_executes(robot_cfg):
    controller, adapter = _controller(robot_cfg, [_behavior("hola")])

    record = controller.trigger_manual("hola")

    assert record["result"] == "executed"
    assert record["decision"] == "manual"
    assert len(adapter.history) == 1
    assert adapter.history[0]["behavior_id"] == "hola"


def test_manual_trigger_rejects_disabled_behavior(robot_cfg, catalog):
    controller, adapter = _controller(robot_cfg, catalog)

    record = controller.trigger_manual("ambiental")

    assert record["result"] == "rejected"
    assert "deshabilitado" in (record["error"] or "")
    assert adapter.history == []


def test_manual_trigger_allow_disabled(robot_cfg, catalog):
    controller, adapter = _controller(robot_cfg, catalog)
    before = len(adapter.history)

    record = controller.trigger_manual("ambiental", allow_disabled=True)

    assert record["result"] == "executed"
    assert len(adapter.history) == before + 1
    assert adapter.history[-1]["behavior_id"] == "ambiental"


def test_manual_mode_ignores_perception(robot_cfg, event):
    controller, adapter = _controller(robot_cfg, [_behavior("hola")])
    controller.set_mode(RunMode.MANUAL)

    record = controller.submit_event(event(Gesture.HOLA))

    assert record["result"] == "ignored_mode"
    assert adapter.history == []


class BlockingHomeAdapter(MockAdapter):
    """MockAdapter cuyo ``home()`` queda bloqueado hasta que el test lo libera."""

    def __init__(self, robot_config) -> None:
        super().__init__(robot_config, time_scale=0)
        self.home_started = threading.Event()
        self.release_home = threading.Event()

    def home(self):
        self.home_started.set()
        self.release_home.wait(5.0)
        return super().home()


def test_home_does_not_block_status_while_adapter_io_runs(robot_cfg):
    adapter = BlockingHomeAdapter(robot_cfg)
    adapter.connect()
    controller = RobotController(
        adapter,
        DecisionEngine([], DecisionConfig(anti_repetition=False, busy_policy="drop")),
        execute_inline=True,
    )

    result: dict = {}
    home_thread = threading.Thread(target=lambda: result.update(controller.home()))
    home_thread.start()
    try:
        assert adapter.home_started.wait(2.0) is True

        status_box: dict = {}
        status_thread = threading.Thread(
            target=lambda: status_box.update(controller.status())
        )
        status_thread.start()
        status_thread.join(timeout=2.0)

        assert status_thread.is_alive() is False, "status() se bloqueó durante home()"
        assert "robot_state" in status_box
    finally:
        adapter.release_home.set()
        home_thread.join(timeout=5.0)

    assert home_thread.is_alive() is False
    assert result["result"] == "executed"
    assert controller.status()["busy"] is False
