"""Contratos de dominio de Exp-brazo.

Modelos pydantic v2 estrictos (``extra="forbid"``) compartidos por las capas
de percepción, decisión, actuación y supervisión.
"""

from __future__ import annotations

import ipaddress
import math
import time
import uuid
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

JOINT_NAMES = ["J1", "J2", "J3", "J4", "J5", "J6"]

# SIN VALIDAR: envelope conservador. El operador puede ACHICARLO, nunca ampliarlo
# desde la API.
PHYSICAL_JOINT_ENVELOPE = {
    "J1": (-180.0, 180.0),
    "J2": (-180.0, 180.0),
    "J3": (-160.0, 160.0),
    "J4": (-180.0, 180.0),
    "J5": (-180.0, 180.0),
    "J6": (-180.0, 180.0),
}


class Gesture(StrEnum):
    """Gestos reconocidos por la capa de percepción."""

    HOLA = "HOLA"
    SI = "SI"
    NO = "NO"
    SEIS_SIETE = "SEIS_SIETE"


class RobotState(StrEnum):
    """Estado operativo del adaptador de robot."""

    IDLE = "idle"
    BUSY = "busy"
    FAULT = "fault"
    DISCONNECTED = "disconnected"


class RunMode(StrEnum):
    """Modo de operación del controlador."""

    AUTO = "auto"
    MANUAL = "manual"
    AMBIENT = "ambient"
    SIM = "simulation"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PerceptionEvent(_StrictModel):
    """Evento discreto producido por la capa de percepción."""

    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    gesture: Gesture
    confidence: float = Field(ge=0, le=1)
    timestamp: float = Field(default_factory=time.time)
    source: str = "camera"


class BehaviorStep(_StrictModel):
    """Paso de un comportamiento: deltas articulares y tiempos."""

    deltas: dict[str, float]
    duration_ms: int = Field(ge=0, le=60_000)
    pause_ms: int = Field(ge=0, le=60_000)

    @field_validator("deltas")
    @classmethod
    def _validate_deltas(cls, value: dict[str, float]) -> dict[str, float]:
        normalized: dict[str, float] = {}
        for joint, raw in value.items():
            if joint not in JOINT_NAMES:
                raise ValueError(f"articulación desconocida: {joint}")
            number = float(raw)
            if not math.isfinite(number):
                raise ValueError(f"delta no finito en {joint}")
            normalized[joint] = number
        return {joint: normalized.get(joint, 0.0) for joint in JOINT_NAMES}


class Behavior(_StrictModel):
    """Comportamiento completo disparado por un gesto."""

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    trigger_gesture: Gesture
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    speed_pct: int = Field(ge=1, le=100)
    acceleration_pct: int = Field(ge=1, le=100)
    priority: int = Field(default=0, ge=-1000, le=1000)
    cooldown_s: float = Field(ge=0, le=3600, default=0.0)
    enabled: bool = True
    validated: bool = False
    steps: list[BehaviorStep] = Field(min_length=1, max_length=100)


class GestureSettings(_StrictModel):
    """Parámetros de reconocimiento de un gesto."""

    enabled: bool = True
    min_confidence: float = Field(ge=0, le=1, default=0.6)
    hold_ms: int = Field(ge=0, default=200)
    cooldown_s: float = Field(ge=0, default=2.0)
    window_ms: int = Field(ge=100, default=1500)
    min_direction_changes: int = Field(ge=1, default=3)
    min_amplitude_ratio: float = Field(ge=0, default=0.04)
    min_cycles: int = Field(ge=1, default=3)
    min_correlation: float = Field(ge=0, le=1, default=0.5)
    # Robustez contra jitter de landmarks (SIN VALIDAR con personas reales).
    min_step_ratio: float = Field(ge=0.0, le=0.2, default=0.01)
    dominance_ratio: float = Field(ge=1.0, le=5.0, default=1.3)
    smoothing_alpha: float = Field(gt=0.0, le=1.0, default=0.65)
    min_samples: int = Field(ge=3, le=120, default=6)
    # Condiciones de contexto para gestos deliberados (SIN VALIDAR).
    require_raised: bool = False
    require_two_hands: bool = False
    min_visibility: float = Field(ge=0, le=1, default=0.6)


class CameraConfig(_StrictModel):
    index: int = 0
    width: int = 640
    height: int = 480
    fps: int = 30
    fourcc: str = "MJPG"


class PerceptionConfig(_StrictModel):
    engine: str = "mediapipe_tasks"
    max_hands: int = 2
    min_detection_confidence: float = Field(ge=0, le=1, default=0.5)
    min_tracking_confidence: float = Field(ge=0, le=1, default=0.5)
    person_selection: str = "largest_pose"
    # Rendimiento de pose: 0 = lite, 1 = full (modelos .task vendorizados).
    pose_model_complexity: int = Field(ge=0, le=1, default=1)
    # 0/1 se aceptan por compatibilidad; ambos usan hand_landmarker.task, que
    # es el único modelo de manos vendorizado (MediaPipe Tasks no ofrece lite).
    hand_model_complexity: int = Field(ge=0, le=1, default=0)
    # Ancho máximo al que se redimensiona el frame antes de inferir.
    process_max_width: int = Field(ge=320, le=1920, default=640)
    # Tope de inferencias por segundo (throttle del pipeline).
    detection_fps: int = Field(ge=1, le=60, default=15)
    global_cooldown_s: float = Field(ge=0, le=10, default=1.0)
    gestures: dict[Gesture, GestureSettings]


class DecisionConfig(_StrictModel):
    anti_repetition: bool = True
    busy_policy: Literal["drop", "keep_last"] = "drop"


class RobotConfig(_StrictModel):
    adapter: Literal["mock", "dobot"] = "mock"
    ip: str = "192.168.5.1"
    dashboard_port: int = Field(default=29999, ge=1, le=65535)
    move_port: int = Field(default=30003, ge=1, le=65535)
    feedback_port: int = Field(default=30004, ge=1, le=65535)
    connect_timeout_s: float = Field(default=3.0, gt=0, le=30)
    max_speed_pct: int = Field(ge=1, le=100, default=30)
    max_accel_pct: int = Field(ge=1, le=100, default=30)
    joint_limits: dict[str, tuple[float, float]]
    home_joints: list[float]
    dedupe_ttl_s: float = Field(default=300.0, ge=0, le=86400)

    @field_validator("ip")
    @classmethod
    def _validate_ip(cls, value: str) -> str:
        try:
            ipaddress.ip_address(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"IP inválida: {value!r}. Usá una dirección IPv4 o IPv6 literal."
            ) from exc
        return value

    @field_validator("home_joints")
    @classmethod
    def _validate_home_joints(cls, value: list[float]) -> list[float]:
        if len(value) != len(JOINT_NAMES):
            raise ValueError(
                f"home_joints debe tener exactamente {len(JOINT_NAMES)} valores "
                f"(recibidos {len(value)})."
            )
        return value

    @field_validator("joint_limits")
    @classmethod
    def _validate_joint_limits(
        cls, value: dict[str, tuple[float, float]]
    ) -> dict[str, tuple[float, float]]:
        missing = [joint for joint in JOINT_NAMES if joint not in value]
        if missing:
            raise ValueError(
                "joint_limits debe incluir exactamente J1..J6; faltan: "
                f"{', '.join(missing)}."
            )
        unknown = [joint for joint in value if joint not in JOINT_NAMES]
        if unknown:
            raise ValueError(
                "joint_limits contiene articulaciones desconocidas: "
                f"{', '.join(str(joint) for joint in unknown)}."
            )

        normalized: dict[str, tuple[float, float]] = {}
        for joint in JOINT_NAMES:
            try:
                low = float(value[joint][0])
                high = float(value[joint][1])
            except (TypeError, ValueError, IndexError) as exc:
                raise ValueError(
                    f"joint_limits[{joint}] debe ser un par numérico (mínimo, máximo)."
                ) from exc
            if not math.isfinite(low) or not math.isfinite(high):
                raise ValueError(f"joint_limits[{joint}] debe contener valores finitos.")
            if low > high:
                raise ValueError(
                    f"joint_limits[{joint}]: el mínimo {low:g}° no puede ser mayor "
                    f"que el máximo {high:g}°."
                )
            env_low, env_high = PHYSICAL_JOINT_ENVELOPE[joint]
            if low < env_low or high > env_high:
                raise ValueError(
                    f"joint_limits[{joint}]=[{low:g}, {high:g}] excede el envelope "
                    f"físico permitido [{env_low:g}, {env_high:g}]; solo se puede achicar."
                )
            normalized[joint] = (low, high)
        return normalized

    @model_validator(mode="after")
    def _validate_home_within_limits(self) -> RobotConfig:
        for index, joint in enumerate(JOINT_NAMES):
            try:
                value = float(self.home_joints[index])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"home_joints[{index}] ({joint}) debe ser un valor numérico."
                ) from exc
            if not math.isfinite(value):
                raise ValueError(f"home_joints[{index}] ({joint}) debe ser finito.")

            low, high = self.joint_limits[joint]
            if value < low or value > high:
                raise ValueError(
                    f"home_joints[{index}] ({joint})={value:g}° está fuera de "
                    f"joint_limits [{low:g}, {high:g}]."
                )

            env_low, env_high = PHYSICAL_JOINT_ENVELOPE[joint]
            if value < env_low or value > env_high:
                raise ValueError(
                    f"home_joints[{index}] ({joint})={value:g}° está fuera del "
                    f"envelope físico [{env_low:g}, {env_high:g}]."
                )
        return self

    @model_validator(mode="after")
    def _validate_dobot_ip(self) -> RobotConfig:
        if self.adapter != "dobot":
            return self

        address = ipaddress.ip_address(self.ip)
        candidates = [address]
        mapped = getattr(address, "ipv4_mapped", None)
        if mapped is not None:
            candidates.append(mapped)

        for candidate in candidates:
            if candidate.is_loopback:
                raise ValueError(
                    f"robot.ip={self.ip!r} no puede ser loopback con "
                    "adapter='dobot'; usá la IP de red del robot."
                )
            if candidate.is_link_local:
                raise ValueError(
                    f"robot.ip={self.ip!r} no puede ser link-local (169.254/16 "
                    "o fe80::/10) con adapter='dobot'."
                )
            if candidate.is_unspecified:
                raise ValueError(
                    f"robot.ip={self.ip!r} no puede ser unspecified "
                    "(0.0.0.0 o ::) con adapter='dobot'."
                )
            if candidate.is_multicast:
                raise ValueError(
                    f"robot.ip={self.ip!r} no puede ser multicast con "
                    "adapter='dobot'."
                )
        return self


class DashboardConfig(_StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    log_max_entries: int = Field(default=2000, ge=10, le=10_000)

    @field_validator("host")
    @classmethod
    def _validate_host(cls, value: str) -> str:
        candidate = value.strip()
        if candidate in {"localhost", "0.0.0.0", "::"}:
            return candidate
        try:
            ipaddress.ip_address(candidate)
            return candidate
        except ValueError:
            pass
        if not candidate or any(ch in candidate for ch in " */?#"):
            raise ValueError("host inválido; usá una IP, localhost o 0.0.0.0")
        if not all(ch.isalnum() or ch in ".-" for ch in candidate):
            raise ValueError("host inválido; caracteres no permitidos")
        return candidate


class SupervisorConfig(_StrictModel):
    heartbeat_timeout_s: float = Field(default=5.0, ge=1, le=60)
    restart_backoff_s: list[float] = Field(default_factory=list, max_length=20)
    ambient_interval_s: float = Field(default=20.0, ge=1, le=3600)

    @field_validator("restart_backoff_s")
    @classmethod
    def _validate_restart_backoff(cls, value: list[float]) -> list[float]:
        for index, delay in enumerate(value):
            try:
                number = float(delay)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"restart_backoff_s[{index}] debe ser un número"
                ) from exc
            if not math.isfinite(number) or not (0 <= number <= 60):
                raise ValueError(
                    f"restart_backoff_s[{index}]={number:g} fuera de rango [0, 60]"
                )
        return value


class AppConfig(_StrictModel):
    version: int = 1
    camera: CameraConfig
    perception: PerceptionConfig
    decision: DecisionConfig
    robot: RobotConfig
    dashboard: DashboardConfig
    supervisor: SupervisorConfig


class BehaviorCatalog(_StrictModel):
    behaviors: list[Behavior]


class ExecutionResult(_StrictModel):
    """Resultado de una orden enviada al adaptador de robot."""

    ok: bool
    behavior_id: str | None = None
    event_id: str | None = None
    error: str | None = None
    duration_ms: float = 0.0
    steps_executed: int = 0
    deduplicated: bool = False
