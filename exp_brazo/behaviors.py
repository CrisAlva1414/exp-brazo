"""Catálogo de comportamientos y validación de límites articulares."""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .contracts import (
    JOINT_NAMES,
    Behavior,
    BehaviorCatalog,
    Gesture,
    RobotConfig,
)


class BehaviorValidationError(ValueError):
    """Errores de validación de uno o más comportamientos."""

    def __init__(self, errors: list[str] | str) -> None:
        if isinstance(errors, str):
            errors = [errors]
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def validate_behavior(behavior: Behavior, robot: RobotConfig) -> list[str]:
    """Valida un comportamiento contra los límites duros del robot."""

    errors: list[str] = []

    for step_index, step in enumerate(behavior.steps, start=1):
        for joint_index, joint in enumerate(JOINT_NAMES):
            delta = step.deltas.get(joint, 0.0)
            if delta == 0:
                continue

            limits = robot.joint_limits.get(joint)
            if limits is None:
                errors.append(
                    f"Comportamiento '{behavior.id}', paso {step_index}: "
                    f"falta el límite para {joint}."
                )
                continue

            low, high = limits
            angle = robot.home_joints[joint_index] + delta
            if angle < low or angle > high:
                errors.append(
                    f"Comportamiento '{behavior.id}', paso {step_index}: "
                    f"{joint}={angle:g}° excede el límite [{low:g}, {high:g}]."
                )

    if behavior.speed_pct > robot.max_speed_pct:
        errors.append(
            f"Comportamiento '{behavior.id}': speed_pct={behavior.speed_pct} "
            f"excede el máximo {robot.max_speed_pct}."
        )

    if behavior.acceleration_pct > robot.max_accel_pct:
        errors.append(
            f"Comportamiento '{behavior.id}': acceleration_pct={behavior.acceleration_pct} "
            f"excede el máximo {robot.max_accel_pct}."
        )

    return errors


def validate_behavior_set(behaviors: Iterable[Behavior], robot: RobotConfig) -> list[str]:
    """Valida un conjunto completo: IDs únicos y cada comportamiento."""

    errors: list[str] = []
    seen_ids: set[str] = set()

    for behavior in behaviors:
        if behavior.id in seen_ids:
            errors.append(f"Comportamiento duplicado: '{behavior.id}'.")
        seen_ids.add(behavior.id)
        errors.extend(validate_behavior(behavior, robot))

    return errors


def _atomic_write_yaml(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(payload, handle, sort_keys=False, allow_unicode=True)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


class BehaviorCatalogStore:
    """Carga y persiste ``behaviors.yaml`` validando contra el robot."""

    def __init__(
        self,
        path: Path = Path("config/behaviors.yaml"),
        robot: RobotConfig | None = None,
    ) -> None:
        if robot is None:
            raise TypeError("BehaviorCatalogStore requiere robot=RobotConfig")
        self.path = Path(path)
        self.robot = robot
        self._lock = threading.RLock()
        self._behaviors: list[Behavior] = []

    def load(self) -> list[Behavior]:
        """Carga el catálogo; si falla, conserva el catálogo anterior."""

        with self._lock:
            if not self.path.exists():
                raise BehaviorValidationError(
                    f"No existe el catálogo de comportamientos: {self.path}."
                )

            try:
                raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
            except OSError as exc:
                raise BehaviorValidationError(f"No se pudo leer {self.path}: {exc}") from exc
            except yaml.YAMLError as exc:
                raise BehaviorValidationError(f"YAML inválido en {self.path}: {exc}") from exc

            try:
                catalog = BehaviorCatalog.model_validate(raw)
            except ValidationError as exc:
                details = [
                    f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                    for error in exc.errors()
                ]
                raise BehaviorValidationError(details) from exc

            errors = self.validate(catalog.behaviors)
            if errors:
                raise BehaviorValidationError(errors)

            self._behaviors = [behavior.model_copy(deep=True) for behavior in catalog.behaviors]
            return self.get_all()

    def get_all(self) -> list[Behavior]:
        with self._lock:
            return [behavior.model_copy(deep=True) for behavior in self._behaviors]

    def get(self, behavior_id: str) -> Behavior | None:
        with self._lock:
            for behavior in self._behaviors:
                if behavior.id == behavior_id:
                    return behavior.model_copy(deep=True)
            return None

    def by_gesture(self, gesture: Gesture | str) -> list[Behavior]:
        wanted = Gesture(gesture)
        with self._lock:
            return [
                behavior.model_copy(deep=True)
                for behavior in self._behaviors
                if behavior.trigger_gesture == wanted
            ]

    def save(self, behaviors: list[Behavior]) -> None:
        """Valida y persiste atómicamente el catálogo completo."""

        errors = self.validate(behaviors)
        if errors:
            raise BehaviorValidationError(errors)

        payload = {
            "behaviors": [behavior.model_dump(mode="json") for behavior in behaviors],
        }
        with self._lock:
            _atomic_write_yaml(self.path, payload)
            self._behaviors = [behavior.model_copy(deep=True) for behavior in behaviors]

    def validate(self, behaviors: Iterable[Behavior]) -> list[str]:
        return validate_behavior_set(behaviors, self.robot)
