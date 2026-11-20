"""Landmarks normalizados de pose y manos.

Estructuras simples e independientes de MediaPipe. Los índices de
``POSE_INDEX`` y ``HAND_INDEX`` corresponden a los modelos legacy de
MediaPipe (solutions). Las coordenadas están normalizadas en ``[0, 1]``
respecto del ancho/alto del frame.

Los umbrales que consumen estos datos son heurísticos y **SIN VALIDAR**
con personas reales.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Mapeo nombre -> índice del modelo MediaPipe Pose (legacy solutions).
POSE_INDEX: dict[str, int] = {
    "NOSE": 0,
    "LEFT_EAR": 7,
    "RIGHT_EAR": 8,
    "LEFT_SHOULDER": 11,
    "RIGHT_SHOULDER": 12,
    "LEFT_ELBOW": 13,
    "RIGHT_ELBOW": 14,
    "LEFT_WRIST": 15,
    "RIGHT_WRIST": 16,
    "LEFT_HIP": 23,
    "RIGHT_HIP": 24,
    "LEFT_INDEX": 19,
    "RIGHT_INDEX": 20,
}

# Mapeo nombre -> índice del modelo MediaPipe Hands (legacy solutions).
HAND_INDEX: dict[str, int] = {
    "WRIST": 0,
    "THUMB_MCP": 2,
    "THUMB_IP": 3,
    "THUMB_TIP": 4,
    "INDEX_PIP": 6,
    "INDEX_TIP": 8,
    "MIDDLE_PIP": 10,
    "MIDDLE_TIP": 12,
    "RING_PIP": 14,
    "RING_TIP": 16,
    "PINKY_PIP": 18,
    "PINKY_TIP": 20,
    "MIDDLE_MCP": 9,
}


@dataclass(frozen=True, slots=True)
class Landmark:
    """Punto normalizado con visibilidad opcional."""

    x: float
    y: float
    z: float = 0.0
    visibility: float = 1.0


@dataclass(frozen=True, slots=True)
class Hand:
    """Mano detectada con sus 21 landmarks."""

    landmarks: list[Landmark] = field(default_factory=list)
    label: str = "unknown"
    handedness: float = 1.0


@dataclass(frozen=True, slots=True)
class Pose:
    """Pose corporal con landmarks indexados por nombre."""

    landmarks: dict[str, Landmark] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FrameLandmarks:
    """Landmarks de un frame, con timestamp y dimensiones de imagen."""

    pose: Pose | None
    hands: list[Hand] = field(default_factory=list)
    ts: float = 0.0
    width: int = 0
    height: int = 0
