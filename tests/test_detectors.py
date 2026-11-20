"""Pruebas de los detectores puros de gestos (sin cv2 ni mediapipe)."""

from __future__ import annotations

import math

from exp_brazo.perception.detectors import (
    amplitude,
    count_cycles,
    detect_hola,
    detect_nod,
    detect_shake,
    detect_six_seven,
    direction_changes,
    distance,
    hand_open,
    pearson,
    velocities,
)
from exp_brazo.perception.landmarks import HAND_INDEX, Hand, Landmark

_BASE = (0.5, 0.6)


def _open_hand() -> Hand:
    base_x, base_y = _BASE
    offsets = {
        0: (0.0, 0.0),
        9: (0.0, -0.06),
        6: (-0.08, -0.12),
        8: (-0.10, -0.24),
        10: (0.0, -0.13),
        12: (0.0, -0.27),
        14: (0.08, -0.12),
        16: (0.10, -0.24),
        18: (0.15, -0.09),
        20: (0.19, -0.19),
    }
    landmarks = [Landmark(base_x, base_y) for _ in range(21)]
    for index, (dx, dy) in offsets.items():
        landmarks[index] = Landmark(base_x + dx, base_y + dy)
    return Hand(landmarks, label="Right", handedness=0.9)


def _fist() -> Hand:
    base_x, base_y = _BASE
    tips = {
        8: (0.49, 0.55),
        12: (0.50, 0.53),
        16: (0.52, 0.53),
        20: (0.56, 0.54),
    }
    landmarks = list(_open_hand().landmarks)
    for index, (x, y) in tips.items():
        landmarks[index] = Landmark(x, y)
    return Hand(landmarks, label="Right", handedness=0.9)


def test_distance_2d_and_3d() -> None:
    assert distance(Landmark(0, 0), Landmark(3, 4)) == 5.0
    assert distance(Landmark(0, 0, 0), Landmark(1, 2, 2)) == 3.0


def test_hand_open_detects_open_hand_and_fist() -> None:
    assert hand_open(_open_hand()) is True
    assert hand_open(_fist()) is False
    assert hand_open(Hand([Landmark(0, 0)] * 10)) is False


def test_direction_changes_ignores_small_steps() -> None:
    assert direction_changes([0, 1, 2, 1, 0]) == 1
    assert direction_changes([0, 1, 0, 1, 0]) == 3
    assert direction_changes([0, 1, 1.0001, 2], min_step=0.1) == 0
    assert direction_changes([]) == 0


def test_amplitude_and_velocities() -> None:
    assert amplitude([1.0, 5.0, 2.0]) == 4.0
    assert amplitude([]) == 0.0
    assert velocities([1.0, 3.0, 6.0]) == [2.0, 3.0]
    assert count_cycles([0, 1, 0, 1, 0], 0.0) == 1


def test_pearson_limits() -> None:
    assert pearson([1, 2, 3], [2, 4, 6]) > 0.99
    assert pearson([1, 2, 3], [3, 2, 1]) < -0.99
    assert pearson([1, 1, 1], [1, 2, 3]) == 0.0
    assert pearson([1], [1]) == 0.0
    assert pearson([1, 2], [1, 2, 3]) == 0.0


def test_detect_hola_oscillating_and_static() -> None:
    moving = [0.5 + 0.1 * math.sin(i * 0.5) for i in range(40)]
    result = detect_hola(
        moving, hand_size=0.3, min_direction_changes=3, min_amplitude_ratio=0.15
    )
    assert result.detected is True
    assert 0.0 <= result.confidence <= 1.0
    assert result.metrics["direction_changes"] >= 3

    static = [0.5] * 40
    assert detect_hola(
        static, hand_size=0.3, min_direction_changes=3, min_amplitude_ratio=0.15
    ).detected is False
    assert detect_hola(
        moving[:3], hand_size=0.3, min_direction_changes=3, min_amplitude_ratio=0.15
    ).detected is False


def test_detect_nod_and_shake() -> None:
    vertical = [0.5 + 0.05 * math.sin(i * 0.5) for i in range(40)]
    horizontal = [0.5 + 0.05 * math.cos(i * 0.5) for i in range(40)]
    assert detect_nod(
        vertical, face_size=0.4, min_direction_changes=3, min_amplitude_ratio=0.04
    ).detected is True
    assert detect_shake(
        horizontal, face_size=0.4, min_direction_changes=3, min_amplitude_ratio=0.04
    ).detected is True
    assert detect_nod(
        [0.5] * 40, face_size=0.4, min_direction_changes=3, min_amplitude_ratio=0.04
    ).detected is False


def test_detect_six_seven_antiphase_vs_inphase() -> None:
    left = [0.5 + 0.05 * math.sin(i * 0.5) for i in range(60)]
    right = [0.5 - 0.05 * math.sin(i * 0.5) for i in range(60)]
    result = detect_six_seven(left, right, min_cycles=3, min_correlation=0.5)
    assert result.detected is True
    assert result.metrics["correlation"] < -0.5
    assert 0.0 <= result.confidence <= 1.0

    inphase = detect_six_seven(left, list(left), min_cycles=3, min_correlation=0.5)
    assert inphase.detected is False
    short = detect_six_seven(left[:5], right[:5], min_cycles=3, min_correlation=0.5)
    assert short.detected is False


def test_hand_index_constants_match_mediapipe_layout() -> None:
    assert HAND_INDEX["WRIST"] == 0
    assert HAND_INDEX["INDEX_TIP"] == 8
    assert HAND_INDEX["PINKY_TIP"] == 20
    assert HAND_INDEX["MIDDLE_MCP"] == 9
