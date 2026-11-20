"""Decisión de comportamientos y controlador de ejecución."""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from .contracts import (
    Behavior,
    DecisionConfig,
    Gesture,
    PerceptionEvent,
    RobotState,
    RunMode,
)
from .robot.base import RobotAdapter


@dataclass
class Decision:
    """Resultado de evaluar un evento de percepción."""

    behavior: Behavior | None
    reason: str


class DecisionEngine:
    """Selecciona comportamiento por gesto, prioridad, cooldown y anti-repetición."""

    def __init__(self, catalog: list[Behavior], config: DecisionConfig) -> None:
        self._lock = threading.RLock()
        self._catalog = [behavior.model_copy(deep=True) for behavior in catalog]
        self.config = config
        self.last_behavior_id: str | None = None
        self._last_run: dict[str, float] = {}

    def set_catalog(self, catalog: list[Behavior]) -> None:
        with self._lock:
            self._catalog = [behavior.model_copy(deep=True) for behavior in catalog]

    def set_config(self, config: DecisionConfig) -> None:
        with self._lock:
            self.config = config

    def get_behavior(self, behavior_id: str) -> Behavior | None:
        with self._lock:
            for behavior in self._catalog:
                if behavior.id == behavior_id:
                    return behavior.model_copy(deep=True)
            return None

    def decide(
        self,
        event: PerceptionEvent,
        now: float | None = None,
        bypass_cooldown: bool = False,
    ) -> Decision:
        with self._lock:
            moment = time.monotonic() if now is None else float(now)

            candidates = sorted(
                (
                    behavior
                    for behavior in self._catalog
                    if behavior.enabled and behavior.trigger_gesture == event.gesture
                ),
                key=lambda behavior: (-behavior.priority, behavior.id),
            )
            if not candidates:
                return Decision(None, "sin_comportamiento")

            if bypass_cooldown:
                eligible = list(candidates)
            else:
                eligible = [
                    behavior
                    for behavior in candidates
                    if moment - self._last_run.get(behavior.id, float("-inf"))
                    >= behavior.cooldown_s
                ]

            if (
                self.config.anti_repetition
                and self.last_behavior_id is not None
                and len(eligible) > 1
            ):
                filtered = [
                    behavior for behavior in eligible if behavior.id != self.last_behavior_id
                ]
                if filtered:
                    eligible = filtered

            if not eligible:
                return Decision(None, "cooldown")

            chosen = eligible[0]
            self._last_run[chosen.id] = moment
            self.last_behavior_id = chosen.id
            return Decision(chosen, "ok")


class RobotController:
    """Coordina eventos, decisión, idempotencia y ejecución thread-safe."""

    def __init__(
        self,
        adapter: RobotAdapter,
        decision_engine: DecisionEngine,
        *,
        busy_policy: str = "drop",
        max_log: int = 2000,
        execute_inline: bool = False,
        clock=time.monotonic,
    ) -> None:
        self.adapter = adapter
        self.decision_engine = decision_engine
        self.busy_policy = busy_policy
        self.max_log = max_log
        self.execute_inline = execute_inline
        self.clock = clock

        self.logs: deque[dict] = deque(maxlen=max_log)
        self.on_record: Callable[[dict], None] | None = None
        self.on_event: Callable[[PerceptionEvent], None] | None = None

        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._paused = False
        self._mode = RunMode.AUTO
        self._active = False
        self._tasks: deque[tuple[dict, Behavior, PerceptionEvent]] = deque()
        self._pending: tuple[dict, Behavior, PerceptionEvent] | None = None
        self._event_seen: dict[str, float] = {}
        self._last_error: str | None = None
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

        if not self.execute_inline:
            self.start()

    def start(self) -> None:
        """Arranca el worker si no se está ejecutando inline."""

        if self.execute_inline:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping.clear()
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="exp-brazo-controller",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """Detiene el worker y el movimiento actual del adaptador."""

        self._stopping.set()
        with self._cond:
            self._cond.notify_all()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        with self._lock:
            self._thread = None
        self.adapter.stop()

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self._paused = bool(paused)

    def set_mode(self, mode: RunMode | str) -> None:
        with self._lock:
            self._mode = mode if isinstance(mode, RunMode) else RunMode(mode)

    def submit_event(self, event: PerceptionEvent) -> dict:
        """Procesa un evento de percepción y devuelve el registro resultante."""

        callback = self.on_event
        if callback is not None:
            try:
                callback(event)
            except Exception:
                pass

        with self._lock:
            record = self._base_record(event.event_id, event.gesture, event.confidence)

            if self._paused:
                record["decision"] = "paused"
                record["result"] = "would_have_done"
                self._record(record)
                return dict(record)

            if self._mode in (RunMode.MANUAL, RunMode.AMBIENT):
                record["decision"] = "ignored_mode"
                record["result"] = "ignored_mode"
                self._record(record)
                return dict(record)

            ttl = self._dedupe_ttl()
            now = self.clock()
            self._prune_seen(now, ttl)
            seen_at = self._event_seen.get(event.event_id)
            if seen_at is not None and now - seen_at < ttl:
                record["decision"] = "deduplicated"
                record["result"] = "deduplicated"
                self._record(record)
                return dict(record)
            self._event_seen[event.event_id] = now

            decision = self.decision_engine.decide(event, now=now)
            record["decision"] = decision.reason
            if decision.behavior is None:
                record["result"] = decision.reason
                self._record(record)
                return dict(record)

            behavior = decision.behavior
            record["behavior_id"] = behavior.id
            state = self.adapter.state

            if state in (RobotState.DISCONNECTED, RobotState.FAULT):
                record["result"] = "would_have_done"
                record["error"] = f"adaptador {state.value}"
                self._record(record)
                return dict(record)

            busy_record = self._handle_busy(record, behavior, event)
            if busy_record is not None:
                return busy_record

            self._dispatch_execution(record, behavior, event)
            return dict(record)

    def trigger_manual(self, behavior_id: str, *, allow_disabled: bool = False) -> dict:
        """Ejecuta un comportamiento por pedido manual del operador."""

        with self._lock:
            behavior = self.decision_engine.get_behavior(behavior_id)
            if behavior is None:
                record = self._base_record(uuid.uuid4().hex, None, None)
                record["decision"] = "manual"
                record["result"] = "rejected"
                record["error"] = "comportamiento inexistente"
                self._record(record)
                return dict(record)

            if not behavior.enabled and not allow_disabled:
                record = self._base_record(
                    uuid.uuid4().hex, behavior.trigger_gesture, 1.0
                )
                record["decision"] = "manual"
                record["behavior_id"] = behavior.id
                record["result"] = "rejected"
                record["error"] = "comportamiento deshabilitado"
                self._record(record)
                return dict(record)

            event = PerceptionEvent(
                gesture=behavior.trigger_gesture,
                confidence=1.0,
                event_id=uuid.uuid4().hex,
            )
            record = self._base_record(event.event_id, event.gesture, event.confidence)
            record["decision"] = "manual"
            record["behavior_id"] = behavior.id

            if self._paused:
                record["result"] = "would_have_done"
                self._record(record)
                return dict(record)

            state = self.adapter.state
            if state in (RobotState.DISCONNECTED, RobotState.FAULT):
                record["result"] = "would_have_done"
                record["error"] = f"adaptador {state.value}"
                self._record(record)
                return dict(record)

            busy_record = self._handle_busy(record, behavior, event)
            if busy_record is not None:
                return busy_record

            self._dispatch_execution(record, behavior, event)
            return dict(record)

    def home(self) -> dict:
        """Lleva el robot a home; permitido incluso en pausa."""

        with self._lock:
            record = self._base_record(None, None, None)
            record["decision"] = "home"
            record["behavior_id"] = "__home__"

            state = self.adapter.state
            if state in (RobotState.DISCONNECTED, RobotState.FAULT):
                record["result"] = "would_have_done"
                record["error"] = f"adaptador {state.value}"
                self._record(record)
                return dict(record)

            if self._is_busy_locked():
                record["result"] = "dropped_busy"
                self._record(record)
                return dict(record)

            self._active = True

        # ``adapter.home()`` hace I/O de red: se ejecuta sin mantener el lock
        # para que ``status()`` y el resto de la API respondan mientras tanto.
        try:
            result = self.adapter.home()
        except Exception as exc:
            with self._lock:
                self._active = False
                record["result"] = "error"
                record["error"] = str(exc)
                self._record(record)
            raise

        with self._lock:
            self._active = False
            record["duration_ms"] = result.duration_ms
            if result.ok:
                record["result"] = "executed"
            else:
                record["result"] = "error"
                record["error"] = result.error
            self._record(record)
        return dict(record)

    def status(self) -> dict:
        with self._lock:
            queue_len = len(self._tasks) + (1 if self._pending is not None else 0)
            return {
                "robot_state": self.adapter.state.value,
                "mode": self._mode.value,
                "paused": self._paused,
                "busy": self.adapter.state == RobotState.BUSY,
                "queue_len": queue_len,
                "logs": len(self.logs),
                "last_error": self._last_error,
            }

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Espera hasta que no queden ejecuciones ni cola pendiente."""

        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                if (
                    not self._active
                    and not self._tasks
                    and self._pending is None
                    and self.adapter.state != RobotState.BUSY
                ):
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)

    def _worker_loop(self) -> None:
        while True:
            with self._cond:
                while not self._stopping.is_set():
                    # Un pending keep_last sólo se ejecuta cuando el adaptador
                    # dejó de estar ocupado (por ejemplo, terminó el otro worker).
                    if self.adapter.state == RobotState.BUSY:
                        self._cond.wait(timeout=0.05)
                        continue
                    if self._pending is not None:
                        pending = self._pending
                        self._pending = None
                        record, behavior, event = pending
                        from_pending = True
                        break
                    if self._tasks:
                        record, behavior, event = self._tasks.popleft()
                        from_pending = False
                        break
                    self._cond.wait(timeout=0.1)
                if self._stopping.is_set():
                    return
                self._active = True

            try:
                if from_pending:
                    execution_record = self._base_record(
                        event.event_id, event.gesture, event.confidence
                    )
                    execution_record["decision"] = "ok"
                    execution_record["behavior_id"] = behavior.id
                    self._execute_record(execution_record, behavior, event)
                else:
                    self._execute_record(record, behavior, event)
            except Exception as exc:  # el worker no debe morir por una ejecución
                with self._lock:
                    record["result"] = "error"
                    record["error"] = str(exc)
                self._record(record)
            finally:
                with self._cond:
                    self._active = False
                    self._cond.notify_all()

    def _dispatch_execution(
        self, record: dict, behavior: Behavior, event: PerceptionEvent
    ) -> None:
        if self.execute_inline:
            self._active = True
            try:
                self._execute_record(record, behavior, event)
            finally:
                self._active = False
            return

        self._tasks.append((record, behavior, event))
        record["result"] = "queued"
        self._cond.notify_all()

    def _execute_record(self, record: dict, behavior: Behavior, event: PerceptionEvent) -> dict:
        result = self.adapter.execute_behavior(behavior, event.event_id)
        record["behavior_id"] = behavior.id
        record["duration_ms"] = result.duration_ms

        if result.deduplicated:
            record["result"] = "deduplicated"
        elif result.ok:
            record["result"] = "executed"
        else:
            record["result"] = "error"
            record["error"] = result.error

        self._record(record)
        return record

    def _handle_busy(
        self, record: dict, behavior: Behavior, event: PerceptionEvent
    ) -> dict | None:
        if not self._is_busy_locked():
            return None

        if self.busy_policy == "keep_last":
            self._pending = (record, behavior, event)
            record["result"] = "queued_busy"
            self._cond.notify_all()
        else:
            record["result"] = "dropped_busy"
        self._record(record)
        return dict(record)

    def _is_busy_locked(self) -> bool:
        return (
            self._active
            or self.adapter.state == RobotState.BUSY
            or bool(self._tasks)
            or self._pending is not None
        )

    def _base_record(
        self,
        event_id: str | None,
        gesture: Gesture | str | None,
        confidence: float | None,
    ) -> dict:
        if isinstance(gesture, Gesture):
            gesture_value = gesture.value
        else:
            gesture_value = gesture
        return {
            "ts": self.clock(),
            "event_id": event_id,
            "gesture": gesture_value,
            "confidence": confidence,
            "decision": None,
            "behavior_id": None,
            "result": None,
            "error": None,
            "mode": self._mode.value,
            "duration_ms": 0.0,
        }

    def _record(self, record: dict) -> None:
        snapshot = dict(record)
        with self._lock:
            self.logs.append(snapshot)
            error = record.get("error")
            if error:
                self._last_error = str(error)
        callback = self.on_record
        if callback is not None:
            try:
                callback(snapshot)
            except Exception:
                pass

    def _dedupe_ttl(self) -> float:
        config = getattr(self.adapter, "robot_config", None)
        return float(getattr(config, "dedupe_ttl_s", 300.0))

    def _prune_seen(self, now: float, ttl: float) -> None:
        expired = [
            event_id for event_id, seen_at in self._event_seen.items() if now - seen_at >= ttl
        ]
        for event_id in expired:
            del self._event_seen[event_id]
