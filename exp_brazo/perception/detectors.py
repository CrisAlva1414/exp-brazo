"""Detectores puros de gestos (sin cv2 ni mediapipe).

Todas las funciones trabajan sobre series de valores normalizados y son
deterministas. Los umbrales son heurísticos y **SIN VALIDAR** con personas
reales; sirven como punto de partida ajustable desde ``PerceptionConfig``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .landmarks import HAND_INDEX, Hand, Landmark


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """Resultado de un detector: disparo, confianza y métricas crudas."""

    detected: bool
    confidence: float
    metrics: dict[str, float | int] = field(default_factory=dict)


def clamp01(value: float) -> float:
    """Acota ``value`` al intervalo ``[0, 1]``."""

    return max(0.0, min(1.0, float(value)))


def distance(a: Landmark, b: Landmark) -> float:
    """Distancia euclidiana 3D entre dos landmarks (z=0 => 2D)."""

    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)


def hand_open(hand: Hand) -> bool:
    """Indica si una mano tiene al menos 4 dedos extendidos.

    Un dedo se considera extendido si la punta está más lejos de la muñeca
    que la articulación PIP por un factor de 1.08. Umbral heurístico
    **SIN VALIDAR** con personas reales.
    """

    if len(hand.landmarks) < 21:
        return False
    wrist = hand.landmarks[HAND_INDEX["WRIST"]]
    pairs = (
        ("INDEX_PIP", "INDEX_TIP"),
        ("MIDDLE_PIP", "MIDDLE_TIP"),
        ("RING_PIP", "RING_TIP"),
        ("PINKY_PIP", "PINKY_TIP"),
    )
    extended = 0
    for pip_name, tip_name in pairs:
        pip = hand.landmarks[HAND_INDEX[pip_name]]
        tip = hand.landmarks[HAND_INDEX[tip_name]]
        if distance(tip, wrist) > distance(pip, wrist) * 1.08:
            extended += 1
    return extended >= 4


def _finger_extended(hand: Hand, pip_name: str, tip_name: str, wrist: Landmark) -> bool:
    pip = hand.landmarks[HAND_INDEX[pip_name]]
    tip = hand.landmarks[HAND_INDEX[tip_name]]
    return distance(tip, wrist) > distance(pip, wrist) * 1.08


def _finger_folded(hand: Hand, pip_name: str, tip_name: str, wrist: Landmark) -> bool:
    pip = hand.landmarks[HAND_INDEX[pip_name]]
    tip = hand.landmarks[HAND_INDEX[tip_name]]
    return distance(tip, wrist) <= distance(pip, wrist) * 1.05


def thumbs_up(hand: Hand) -> DetectionResult:
    """Detecta pulgar arriba: pulgar extendido y los otros dedos cerrados."""

    if len(hand.landmarks) < 21:
        return DetectionResult(False, 0.0, {})
    wrist = hand.landmarks[HAND_INDEX["WRIST"]]
    thumb_mcp = hand.landmarks[HAND_INDEX["THUMB_MCP"]]
    thumb_ip = hand.landmarks[HAND_INDEX["THUMB_IP"]]
    thumb_tip = hand.landmarks[HAND_INDEX["THUMB_TIP"]]
    thumb_ratio = distance(thumb_tip, wrist) / max(distance(thumb_ip, wrist), 1e-9)
    # Cadena vertical: señal estable para un pulgar arriba estático.
    vertical = thumb_tip.y < thumb_ip.y < thumb_mcp.y
    thumb_extended = thumb_ratio > 1.05 or vertical
    folded = sum(
        1
        for pip_name, tip_name in (
            ("INDEX_PIP", "INDEX_TIP"),
            ("MIDDLE_PIP", "MIDDLE_TIP"),
            ("RING_PIP", "RING_TIP"),
            ("PINKY_PIP", "PINKY_TIP"),
        )
        if _finger_folded(hand, pip_name, tip_name, wrist)
    )
    detected = thumb_extended and folded >= 3
    thumb_score = clamp01((thumb_ratio - 1.0) / 0.4)
    fold_score = folded / 4.0
    confidence = 0.6 * thumb_score + 0.3 * fold_score + 0.1 * (1.0 if vertical else 0.0)
    return DetectionResult(
        detected=detected,
        confidence=confidence,
        metrics={
            "thumb_extended": 1 if thumb_extended else 0,
            "thumb_vertical": 1 if vertical else 0,
            "folded_fingers": folded,
        },
    )


def index_only(hand: Hand) -> DetectionResult:
    """Detecta índice extendido con medio/anular/meñique cerrados."""

    if len(hand.landmarks) < 21:
        return DetectionResult(False, 0.0, {})
    wrist = hand.landmarks[HAND_INDEX["WRIST"]]
    index_extended = _finger_extended(hand, "INDEX_PIP", "INDEX_TIP", wrist)
    folded = sum(
        1
        for pip_name, tip_name in (
            ("MIDDLE_PIP", "MIDDLE_TIP"),
            ("RING_PIP", "RING_TIP"),
            ("PINKY_PIP", "PINKY_TIP"),
        )
        if _finger_folded(hand, pip_name, tip_name, wrist)
    )
    detected = index_extended and folded >= 2
    ratio = distance(hand.landmarks[HAND_INDEX["INDEX_TIP"]], wrist) / max(
        distance(hand.landmarks[HAND_INDEX["INDEX_PIP"]], wrist), 1e-9
    )
    confidence = 0.5 * clamp01((ratio - 1.0) / 0.6) + 0.5 * (folded / 3.0)
    return DetectionResult(
        detected=detected,
        confidence=confidence,
        metrics={
            "index_extended": 1 if index_extended else 0,
            "folded_fingers": folded,
        },
    )


def velocities(values: list[float]) -> list[float]:
    """Diferencias consecutivas de una serie."""

    return [float(values[i + 1]) - float(values[i]) for i in range(len(values) - 1)]


def smooth(values: list[float], alpha: float = 0.65) -> list[float]:
    """Suaviza una serie con media móvil exponencial (EMA).

    Reduce el jitter de landmarks que, a bajos FPS, se confunde con
    oscilaciones reales. ``alpha`` es el peso de la muestra nueva.
    """

    if not values:
        return []
    a = max(0.05, min(1.0, float(alpha)))
    out = [float(values[0])]
    for value in values[1:]:
        out.append(a * float(value) + (1.0 - a) * out[-1])
    return out


def direction_changes(values: list[float], min_step: float = 0.0) -> int:
    """Cuenta cambios de signo entre diferencias consecutivas.

    Las diferencias con ``abs(diff) < min_step`` (y las nulas) se ignoran
    para no contar ruido como oscilación. Umbral heurístico **SIN VALIDAR**.
    """

    changes = 0
    previous_sign = 0
    for diff in velocities(values):
        if abs(diff) < min_step or diff == 0:
            continue
        sign = 1 if diff > 0 else -1
        if previous_sign != 0 and sign != previous_sign:
            changes += 1
        previous_sign = sign
    return changes


def amplitude(values: list[float]) -> float:
    """Amplitud pico a pico de una serie (0 si está vacía)."""

    if not values:
        return 0.0
    return float(max(values) - min(values))


def pearson(a: list[float], b: list[float]) -> float:
    """Correlación de Pearson; 0.0 si no hay datos o varianza nula."""

    n = len(a)
    if n < 2 or n != len(b):
        return 0.0
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b, strict=True))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)
    if var_a <= 0 or var_b <= 0:
        return 0.0
    return cov / math.sqrt(var_a * var_b)


def count_cycles(values: list[float], min_step: float = 0.0) -> int:
    """Ciclos completos estimados como ``direction_changes // 2``."""

    return direction_changes(values, min_step) // 2


def _oscillation_result(
    values: list[float],
    size: float,
    min_direction_changes: int,
    min_amplitude_ratio: float,
    *,
    min_step_ratio: float = 0.01,
    smoothing_alpha: float = 0.65,
    min_samples: int = 6,
    other_amplitude: float | None = None,
    dominance_ratio: float = 1.3,
) -> DetectionResult:
    raw_amp = amplitude(values)
    filtered = smooth(values, smoothing_alpha)
    amp = amplitude(filtered)
    min_amp = min_amplitude_ratio * max(float(size), 1e-6)
    min_step = max(1e-4, min_step_ratio * max(float(size), 1e-6))
    changes = direction_changes(filtered, min_step)
    dominant = True
    if other_amplitude is not None:
        dominant = amp >= dominance_ratio * max(float(other_amplitude), 1e-9)
    detected = (
        len(filtered) >= min_samples
        and amp >= min_amp
        and changes >= min_direction_changes
        and dominant
    )
    amp_score = clamp01(amp / max(min_amp * 2.0, 1e-9))
    change_score = clamp01(changes / max(min_direction_changes + 1, 1))
    sample_score = clamp01(len(filtered) / max(min_samples + 2, 1))
    confidence = 0.45 * amp_score + 0.35 * change_score + 0.20 * sample_score
    if not dominant:
        confidence *= 0.5
    metrics = {
        "raw_amplitude": raw_amp,
        "amplitude": amp,
        "direction_changes": changes,
        "min_amplitude": min_amp,
        "min_step": min_step,
        "samples": len(filtered),
        "dominant": 1 if dominant else 0,
    }
    return DetectionResult(detected=detected, confidence=confidence, metrics=metrics)


def detect_hola(
    wrist_x: list[float],
    hand_size: float,
    min_direction_changes: int,
    min_amplitude_ratio: float,
    *,
    min_step_ratio: float = 0.01,
    smoothing_alpha: float = 0.65,
    min_samples: int = 5,
    other_amplitude: float | None = None,
    dominance_ratio: float = 1.5,
) -> DetectionResult:
    """Detecta saludo por oscilación horizontal dominante de la muñeca.

    Exige que el eje X domine al Y para no confundir el 6-7 (sube/baja con
    dos manos) con un saludo. **SIN VALIDAR** con personas reales.
    """

    return _oscillation_result(
        wrist_x,
        hand_size,
        min_direction_changes,
        min_amplitude_ratio,
        min_step_ratio=min_step_ratio,
        smoothing_alpha=smoothing_alpha,
        min_samples=min_samples,
        other_amplitude=other_amplitude,
        dominance_ratio=dominance_ratio,
    )


def detect_nod(
    nose_y: list[float],
    face_size: float,
    min_direction_changes: int,
    min_amplitude_ratio: float,
    *,
    min_step_ratio: float = 0.012,
    smoothing_alpha: float = 0.6,
    min_samples: int = 6,
    other_amplitude: float | None = None,
    dominance_ratio: float = 1.5,
) -> DetectionResult:
    """Detecta "sí" por oscilación vertical dominante de la nariz.

    Requiere que la amplitud vertical domine a la horizontal para no
    confundir movimientos diagonales con asentimientos. **SIN VALIDAR**.
    """

    return _oscillation_result(
        nose_y,
        face_size,
        min_direction_changes,
        min_amplitude_ratio,
        min_step_ratio=min_step_ratio,
        smoothing_alpha=smoothing_alpha,
        min_samples=min_samples,
        other_amplitude=other_amplitude,
        dominance_ratio=dominance_ratio,
    )


def detect_shake(
    nose_x: list[float],
    face_size: float,
    min_direction_changes: int,
    min_amplitude_ratio: float,
    *,
    min_step_ratio: float = 0.012,
    smoothing_alpha: float = 0.6,
    min_samples: int = 6,
    other_amplitude: float | None = None,
    dominance_ratio: float = 1.5,
) -> DetectionResult:
    """Detecta "no" por oscilación horizontal dominante de la nariz.

    Requiere que la amplitud horizontal domine a la vertical. **SIN VALIDAR**.
    """

    return _oscillation_result(
        nose_x,
        face_size,
        min_direction_changes,
        min_amplitude_ratio,
        min_step_ratio=min_step_ratio,
        smoothing_alpha=smoothing_alpha,
        min_samples=min_samples,
        other_amplitude=other_amplitude,
        dominance_ratio=dominance_ratio,
    )


def detect_six_seven(
    left_y: list[float],
    right_y: list[float],
    min_cycles: int,
    min_correlation: float,
    *,
    min_step_ratio: float = 0.008,
    smoothing_alpha: float = 0.6,
    min_samples: int = 8,
) -> DetectionResult:
    """Detecta el gesto "6/7" por movimiento antifase de ambas muñecas.

    Requiere muestras suficientes, correlación negativa y ciclos mínimos.
    Umbrales heurísticos **SIN VALIDAR** con personas reales.
    """

    left = smooth(left_y, smoothing_alpha)
    right = smooth(right_y, smoothing_alpha)
    samples = min(len(left), len(right))
    corr = pearson(velocities(left), velocities(right))
    min_step = max(1e-4, min_step_ratio)
    cycles = count_cycles(left, min_step)
    amp = amplitude(left)
    detected = (
        samples >= min_samples
        and corr <= -min_correlation
        and cycles >= min_cycles
        and amp >= 0.02
    )
    cycles_score = clamp01(cycles / max(min_cycles, 1))
    corr_score = clamp01(-corr)
    confidence = 0.5 * cycles_score + 0.5 * corr_score
    metrics = {
        "correlation": corr,
        "cycles": cycles,
        "amplitude": amp,
        "samples": samples,
    }
    return DetectionResult(detected=detected, confidence=confidence, metrics=metrics)
