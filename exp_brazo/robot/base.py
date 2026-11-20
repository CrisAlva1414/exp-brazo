"""Interfaz común de los adaptadores de robot."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod

from ..behaviors import validate_behavior as _validate_behavior_limits
from ..contracts import (
    JOINT_NAMES,
    PHYSICAL_JOINT_ENVELOPE,
    Behavior,
    ExecutionResult,
    RobotConfig,
    RobotState,
)


class RobotAdapter(ABC):
    """Contrato de actuación: límites duros y operaciones básicas."""

    name: str
    state: RobotState
    robot_config: RobotConfig

    def __init__(
        self,
        name: str,
        robot_config: RobotConfig,
        state: RobotState = RobotState.DISCONNECTED,
    ) -> None:
        self.name = name
        self.robot_config = robot_config
        self.state = state

    @abstractmethod
    def connect(self) -> bool:
        """Establece la conexión con el robot."""

    @abstractmethod
    def disconnect(self) -> None:
        """Cierra la conexión con el robot."""

    @abstractmethod
    def execute_behavior(self, behavior: Behavior, event_id: str) -> ExecutionResult:
        """Ejecuta un comportamiento completo."""

    @abstractmethod
    def home(self) -> ExecutionResult:
        """Lleva el robot a la pose de home configurada."""

    @abstractmethod
    def stop(self) -> None:
        """Detiene el movimiento actual y vuelve a estado seguro."""

    @abstractmethod
    def status(self) -> dict:
        """Devuelve un resumen serializable del estado del adaptador."""

    def validate_behavior(self, behavior: Behavior) -> list[str]:
        """Valida un comportamiento contra los límites de este robot."""

        return _validate_behavior_limits(behavior, self.robot_config)

    def validate_joint_target(
        self, joints: list[float], where: str = "objetivo"
    ) -> list[str]:
        """Valida una pose articular contra el envelope y los límites del robot."""

        try:
            count = len(joints)
        except TypeError:
            return [f"{where}: se esperaba una lista de {len(JOINT_NAMES)} valores."]
        if count != len(JOINT_NAMES):
            return [
                f"{where}: se esperaban {len(JOINT_NAMES)} valores articulares "
                f"(recibidos {count})."
            ]

        errors: list[str] = []
        for index, joint in enumerate(JOINT_NAMES):
            try:
                value = float(joints[index])
            except (TypeError, ValueError):
                errors.append(f"{where}: {joint} no es un valor numérico válido.")
                continue
            if not math.isfinite(value):
                errors.append(f"{where}: {joint} no es finito.")
                continue

            limits = self.robot_config.joint_limits.get(joint)
            if limits is None:
                errors.append(f"{where}: falta el límite para {joint}.")
            else:
                low, high = limits
                if value < float(low) or value > float(high):
                    errors.append(
                        f"{where}: {joint}={value:g}° excede el límite "
                        f"[{low:g}, {high:g}]."
                    )

            env_low, env_high = PHYSICAL_JOINT_ENVELOPE[joint]
            if value < env_low or value > env_high:
                errors.append(
                    f"{where}: {joint}={value:g}° excede el envelope físico "
                    f"[{env_low:g}, {env_high:g}]."
                )
        return errors

    def _check_limits(
        self, behavior: Behavior, event_id: str | None = None
    ) -> ExecutionResult | None:
        """Devuelve un resultado fallido si el comportamiento viola límites."""

        errors = self.validate_behavior(behavior)
        for step_index, step in enumerate(behavior.steps, start=1):
            targets = [
                float(self.robot_config.home_joints[index])
                + float(step.deltas.get(joint, 0.0))
                for index, joint in enumerate(JOINT_NAMES)
            ]
            errors.extend(self.validate_joint_target(targets, where=f"paso {step_index}"))
        errors = list(dict.fromkeys(errors))
        if not errors:
            return None
        return ExecutionResult(
            ok=False,
            behavior_id=behavior.id,
            event_id=event_id,
            error="; ".join(errors),
        )
