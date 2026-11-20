"""Adaptador de robot simulado, sin hardware ni movimiento real."""

from __future__ import annotations

import threading
import time

from ..contracts import Behavior, ExecutionResult, RobotConfig, RobotState
from .base import RobotAdapter


class MockAdapter(RobotAdapter):
    """Simula ejecución de comportamientos y conserva un historial."""

    def __init__(
        self,
        robot_config: RobotConfig,
        *,
        time_scale: float = 1.0,
        fail_on_execute: bool = False,
        clock=time.monotonic,
    ) -> None:
        super().__init__("mock", robot_config)
        self.time_scale = float(time_scale)
        self.fail_on_execute = fail_on_execute
        self.clock = clock
        self.history: list[dict] = []
        self._seen: dict[str, float] = {}
        self._lock = threading.RLock()

    def connect(self) -> bool:
        with self._lock:
            self.state = RobotState.IDLE
            return True

    def disconnect(self) -> None:
        with self._lock:
            self.state = RobotState.DISCONNECTED

    def execute_behavior(self, behavior: Behavior, event_id: str) -> ExecutionResult:
        with self._lock:
            if self.state in (RobotState.DISCONNECTED, RobotState.FAULT):
                return ExecutionResult(
                    ok=False,
                    behavior_id=behavior.id,
                    event_id=event_id,
                    error=f"adaptador no disponible: {self.state.value}",
                )

            now = self.clock()
            if event_id:
                seen_at = self._seen.get(event_id)
                if seen_at is not None and now - seen_at < self.robot_config.dedupe_ttl_s:
                    return ExecutionResult(
                        ok=True,
                        behavior_id=behavior.id,
                        event_id=event_id,
                        deduplicated=True,
                        steps_executed=0,
                    )
                self._seen[event_id] = now
                self._prune_seen(now)

            limited = self._check_limits(behavior, event_id)
            if limited is not None:
                return limited

            if self.fail_on_execute:
                self.state = RobotState.FAULT
                return ExecutionResult(
                    ok=False,
                    behavior_id=behavior.id,
                    event_id=event_id,
                    error="fallo inyectado",
                )

            duration_ms = self._duration_ms(behavior)
            steps = len(behavior.steps)
            self.state = RobotState.BUSY

        self._sleep_ms(duration_ms)

        with self._lock:
            self.history.append(
                {
                    "behavior_id": behavior.id,
                    "event_id": event_id,
                    "steps": steps,
                    "duration_ms": duration_ms,
                    "ts": self.clock(),
                }
            )
            self.state = RobotState.IDLE
            return ExecutionResult(
                ok=True,
                behavior_id=behavior.id,
                event_id=event_id,
                duration_ms=duration_ms,
                steps_executed=steps,
            )

    def home(self) -> ExecutionResult:
        with self._lock:
            if self.state in (RobotState.DISCONNECTED, RobotState.FAULT):
                return ExecutionResult(
                    ok=False,
                    behavior_id="__home__",
                    error=f"adaptador no disponible: {self.state.value}",
                )

            errors = self.validate_joint_target(
                list(self.robot_config.home_joints), where="home"
            )
            if errors:
                return ExecutionResult(
                    ok=False,
                    behavior_id="__home__",
                    error="; ".join(errors),
                )

            if self.fail_on_execute:
                self.state = RobotState.FAULT
                return ExecutionResult(
                    ok=False,
                    behavior_id="__home__",
                    error="fallo inyectado",
                )

            duration_ms = 500.0 * self.time_scale
            self.state = RobotState.BUSY

        self._sleep_ms(duration_ms)

        with self._lock:
            self.history.append(
                {
                    "behavior_id": "__home__",
                    "event_id": None,
                    "steps": 0,
                    "duration_ms": duration_ms,
                    "ts": self.clock(),
                }
            )
            self.state = RobotState.IDLE
            return ExecutionResult(
                ok=True,
                behavior_id="__home__",
                duration_ms=duration_ms,
                steps_executed=0,
            )

    def stop(self) -> None:
        with self._lock:
            self.state = RobotState.IDLE

    def status(self) -> dict:
        with self._lock:
            return {
                "name": "mock",
                "state": self.state.value,
                "history_len": len(self.history),
                "last": dict(self.history[-1]) if self.history else None,
            }

    def inject_fault(self, state: RobotState = RobotState.FAULT) -> None:
        """Fuerza un estado de fallo para pruebas de caos."""

        with self._lock:
            self.state = state

    def clear_fault(self) -> None:
        """Limpia un fallo inyectado y deja el adaptador en IDLE."""

        with self._lock:
            if self.state == RobotState.FAULT:
                self.state = RobotState.IDLE

    def _duration_ms(self, behavior: Behavior) -> float:
        total_ms = sum(step.duration_ms + step.pause_ms for step in behavior.steps)
        return total_ms * self.time_scale

    def _sleep_ms(self, duration_ms: float) -> None:
        if self.time_scale <= 0:
            return
        time.sleep(max(duration_ms / 1000.0, 0.01))

    def _prune_seen(self, now: float) -> None:
        ttl = self.robot_config.dedupe_ttl_s
        expired = [event_id for event_id, seen_at in self._seen.items() if now - seen_at >= ttl]
        for event_id in expired:
            del self._seen[event_id]
