"""Pruebas deterministas del GestureTracker con frames sintéticos."""

from __future__ import annotations

import math

from exp_brazo.contracts import (
    CameraConfig,
    Gesture,
    GestureSettings,
    PerceptionConfig,
)
from exp_brazo.perception.landmarks import FrameLandmarks, Hand, Landmark, Pose
from exp_brazo.perception.tracker import GestureTracker

FPS = 30
DT = 1.0 / FPS


def make_config(overrides: dict[Gesture, dict] | None = None) -> PerceptionConfig:
    overrides = overrides or {}
    gestures: dict[Gesture, GestureSettings] = {}
    for gesture in Gesture:
        values = {
            "enabled": True,
            "min_confidence": 0.5,
            "hold_ms": 200,
            "cooldown_s": 10.0,
            "window_ms": 2000,
            "min_direction_changes": 3,
            "min_amplitude_ratio": 0.04,
            "min_cycles": 2,
            "min_correlation": 0.5,
            "require_raised": False,
            "require_two_hands": False,
        }
        if gesture is Gesture.HOLA:
            values["min_amplitude_ratio"] = 0.1
        values.update(overrides.get(gesture, {}))
        gestures[gesture] = GestureSettings(**values)
    return PerceptionConfig(gestures=gestures, global_cooldown_s=0.0)


def open_hand(cx: float, cy: float) -> Hand:
    wrist_x, wrist_y = cx, cy + 0.1
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
    landmarks = [Landmark(wrist_x, wrist_y) for _ in range(21)]
    for index, (dx, dy) in offsets.items():
        landmarks[index] = Landmark(wrist_x + dx, wrist_y + dy)
    return Hand(landmarks, label="Right", handedness=0.9)


def make_frame(
    ts: float,
    *,
    nose: Landmark | None = None,
    left_shoulder: Landmark | None = None,
    right_shoulder: Landmark | None = None,
    left_wrist: Landmark | None = None,
    right_wrist: Landmark | None = None,
    hand: Hand | None = None,
    hands: list[Hand] | None = None,
) -> FrameLandmarks:
    points: dict[str, Landmark] = {}
    if nose is not None:
        points["NOSE"] = nose
    if left_shoulder is not None:
        points["LEFT_SHOULDER"] = left_shoulder
    if right_shoulder is not None:
        points["RIGHT_SHOULDER"] = right_shoulder
    if left_wrist is not None:
        points["LEFT_WRIST"] = left_wrist
    if right_wrist is not None:
        points["RIGHT_WRIST"] = right_wrist
    pose = Pose(points) if points else None
    detected_hands = list(hands) if hands is not None else ([hand] if hand is not None else [])
    return FrameLandmarks(pose, detected_hands, ts, 640, 480)


def _shoulders() -> tuple[Landmark, Landmark]:
    return Landmark(0.3, 0.7), Landmark(0.7, 0.7)


def test_hola_emits_once_and_respects_cooldown_window() -> None:
    config = make_config(
        {
            Gesture.HOLA: {
                "hold_ms": 200,
                "cooldown_s": 3.0,
                "min_direction_changes": 3,
                "min_amplitude_ratio": 0.1,
                "min_confidence": 0.1,
            }
        }
    )
    tracker = GestureTracker(config, clock=lambda: 0.0)
    events = []
    t = 0.0
    for _ in range(int(2.5 * FPS)):
        x = 0.5 + 0.1 * math.sin(2 * math.pi * t / 0.6)
        hand = open_hand(x, 0.5)
        events.extend(tracker.update(make_frame(t, hand=hand), now=t))
        t += DT

    assert len(events) == 1
    assert events[0].gesture is Gesture.HOLA
    assert events[0].source == "camera"
    assert 0.0 <= events[0].confidence <= 1.0


def test_hola_respects_hold_ms() -> None:
    config = make_config(
        {
            Gesture.HOLA: {
                "hold_ms": 500,
                "cooldown_s": 0.0,
                "min_direction_changes": 1,
                "min_amplitude_ratio": 0.01,
                "min_confidence": 0.0,
            }
        }
    )
    tracker = GestureTracker(config, clock=lambda: 0.0)
    first_detected: float | None = None
    event_at: float | None = None
    t = 0.0
    for _ in range(int(2.0 * FPS)):
        x = 0.5 + 0.1 * math.sin(2 * math.pi * t / 0.6)
        emitted = tracker.update(make_frame(t, hand=open_hand(x, 0.5)), now=t)
        metrics = tracker.metrics().get(Gesture.HOLA, {})
        if first_detected is None and metrics.get("detected"):
            first_detected = t
        if emitted and event_at is None:
            event_at = t
        t += DT

    assert first_detected is not None
    assert event_at is not None
    assert 0.5 <= event_at - first_detected <= 0.5 + 2 * DT


def test_hola_respects_cooldown_between_events() -> None:
    cooldown = 1.5
    config = make_config(
        {
            Gesture.HOLA: {
                "hold_ms": 0,
                "cooldown_s": cooldown,
                "min_direction_changes": 1,
                "min_amplitude_ratio": 0.01,
                "min_confidence": 0.0,
            }
        }
    )
    tracker = GestureTracker(config, clock=lambda: 0.0)
    event_times: list[float] = []
    t = 0.0
    for _ in range(int(5.0 * FPS)):
        x = 0.5 + 0.1 * math.sin(2 * math.pi * t / 0.6)
        if tracker.update(make_frame(t, hand=open_hand(x, 0.5)), now=t):
            event_times.append(t)
        t += DT

    assert len(event_times) >= 2
    gaps = [b - a for a, b in zip(event_times[:-1], event_times[1:], strict=True)]
    assert all(gap >= cooldown - 1e-9 for gap in gaps)


def test_static_hand_does_not_emit() -> None:
    tracker = GestureTracker(make_config(), clock=lambda: 0.0)
    events = []
    for index in range(int(2.0 * FPS)):
        t = index * DT
        events.extend(tracker.update(make_frame(t, hand=open_hand(0.5, 0.5)), now=t))
    assert events == []
    assert tracker.metrics()[Gesture.HOLA]["detected"] is False


def special_hand(cx: float, cy: float, *, index: bool = False, thumb: bool = False) -> Hand:
    """Mano sintética: índice extendido o pulgar arriba, resto cerrado."""

    wrist_x, wrist_y = cx, cy + 0.1
    landmarks = [Landmark(wrist_x, wrist_y) for _ in range(21)]
    fingers = {
        "INDEX": (6, 8, -0.10),
        "MIDDLE": (10, 12, 0.0),
        "RING": (14, 16, 0.10),
        "PINKY": (18, 20, 0.18),
    }
    for name, (pip, tip, dx) in fingers.items():
        if name == "INDEX" and index:
            landmarks[pip] = Landmark(wrist_x + dx * 0.4, wrist_y - 0.08)
            landmarks[tip] = Landmark(wrist_x + dx * 0.7, wrist_y - 0.22)
        else:
            landmarks[pip] = Landmark(wrist_x + dx * 0.4, wrist_y - 0.05)
            landmarks[tip] = Landmark(wrist_x + dx * 0.45, wrist_y - 0.01)
    landmarks[2] = Landmark(wrist_x, wrist_y - 0.02)
    if thumb:
        landmarks[3] = Landmark(wrist_x, wrist_y - 0.06)
        landmarks[4] = Landmark(wrist_x, wrist_y - 0.15)
    else:
        landmarks[3] = Landmark(wrist_x + 0.02, wrist_y - 0.01)
        landmarks[4] = Landmark(wrist_x + 0.03, wrist_y)
    return Hand(landmarks, label="Right", handedness=0.9)


def test_si_from_thumbs_up() -> None:
    config = make_config(
        {
            Gesture.HOLA: {"enabled": False},
            Gesture.SI: {
                "hold_ms": 200,
                "cooldown_s": 10.0,
                "min_confidence": 0.5,
            },
            Gesture.NO: {"enabled": False},
            Gesture.SEIS_SIETE: {"enabled": False},
        }
    )
    tracker = GestureTracker(config, clock=lambda: 0.0)
    events = []
    t = 0.0
    for _ in range(int(1.5 * FPS)):
        events.extend(
            tracker.update(make_frame(t, hand=special_hand(0.5, 0.4, thumb=True)), now=t)
        )
        t += DT
    assert len(events) == 1
    assert events[0].gesture is Gesture.SI


def test_no_from_index_wag() -> None:
    config = make_config(
        {
            Gesture.HOLA: {"enabled": False},
            Gesture.SI: {"enabled": False},
            Gesture.NO: {
                "hold_ms": 100,
                "cooldown_s": 10.0,
                "window_ms": 3000,
                "min_direction_changes": 3,
                "min_amplitude_ratio": 0.1,
                "min_step_ratio": 0.01,
                "min_samples": 7,
                "min_confidence": 0.1,
            },
            Gesture.SEIS_SIETE: {"enabled": False},
        }
    )
    tracker = GestureTracker(config, clock=lambda: 0.0)
    events = []
    t = 0.0
    for _ in range(int(2.5 * FPS)):
        cx = 0.5 + 0.08 * math.sin(2 * math.pi * t / 0.6)
        events.extend(
            tracker.update(make_frame(t, hand=special_hand(cx, 0.4, index=True)), now=t)
        )
        t += DT
    assert len(events) == 1
    assert events[0].gesture is Gesture.NO


def test_six_seven_from_antiphase_wrists() -> None:
    config = make_config(
        {
            Gesture.HOLA: {"enabled": False},
            Gesture.SI: {"enabled": False},
            Gesture.NO: {"enabled": False},
            Gesture.SEIS_SIETE: {
                "hold_ms": 100,
                "cooldown_s": 10.0,
                "min_cycles": 2,
                "min_correlation": 0.5,
                "min_confidence": 0.1,
            },
        }
    )
    tracker = GestureTracker(config, clock=lambda: 0.0)
    events = []
    t = 0.0
    for _ in range(int(3.0 * FPS)):
        left = Landmark(0.3, 0.5 + 0.05 * math.sin(2 * math.pi * t / 0.5))
        right = Landmark(0.7, 0.5 - 0.05 * math.sin(2 * math.pi * t / 0.5))
        frame = make_frame(t, left_wrist=left, right_wrist=right)
        events.extend(tracker.update(frame, now=t))
        t += DT

    assert len(events) == 1
    assert events[0].gesture is Gesture.SEIS_SIETE


def test_empty_frame_is_ignored() -> None:
    tracker = GestureTracker(make_config(), clock=lambda: 0.0)
    empty = FrameLandmarks(None, [], 0.0, 640, 480)
    assert tracker.update(empty, now=0.0) == []


def test_reset_clears_state() -> None:
    tracker = GestureTracker(make_config(), clock=lambda: 0.0)
    tracker.update(make_frame(0.0, hand=open_hand(0.5, 0.5)), now=0.0)
    tracker.reset()
    assert tracker.metrics() == {}


def test_camera_config_defaults_remain_available() -> None:
    # Salvaguarda: el tracker no debe depender de la cámara.
    assert CameraConfig().index == 0


def test_hola_requires_hand_above_torso() -> None:
    config = make_config(
        {
            Gesture.HOLA: {
                "hold_ms": 0,
                "cooldown_s": 0.0,
                "min_direction_changes": 1,
                "min_amplitude_ratio": 0.01,
                "min_confidence": 0.0,
                "require_raised": True,
            },
            Gesture.SI: {"enabled": False},
            Gesture.NO: {"enabled": False},
            Gesture.SEIS_SIETE: {"enabled": False},
        }
    )
    tracker = GestureTracker(config, clock=lambda: 0.0)
    left, right = _shoulders()
    events = []
    t = 0.0
    for _ in range(int(2.0 * FPS)):
        x = 0.5 + 0.1 * math.sin(2 * math.pi * t / 0.6)
        # Mano a la altura de la cintura (por debajo del torso medio).
        hand = open_hand(x, 0.85)
        frame = make_frame(
            t, hand=hand, left_shoulder=left, right_shoulder=right
        )
        events.extend(tracker.update(frame, now=t))
        t += DT
    assert events == []


def test_hola_raised_hand_emits() -> None:
    config = make_config(
        {
            Gesture.HOLA: {
                "hold_ms": 0,
                "cooldown_s": 10.0,
                "min_direction_changes": 1,
                "min_amplitude_ratio": 0.01,
                "min_confidence": 0.0,
                "require_raised": True,
            },
            Gesture.SI: {"enabled": False},
            Gesture.NO: {"enabled": False},
            Gesture.SEIS_SIETE: {"enabled": False},
        }
    )
    tracker = GestureTracker(config, clock=lambda: 0.0)
    left, right = _shoulders()
    events = []
    t = 0.0
    for _ in range(int(2.0 * FPS)):
        x = 0.5 + 0.1 * math.sin(2 * math.pi * t / 0.6)
        hand = open_hand(x, 0.35)
        frame = make_frame(
            t, hand=hand, left_shoulder=left, right_shoulder=right
        )
        events.extend(tracker.update(frame, now=t))
        t += DT
    assert len(events) == 1
    assert events[0].gesture is Gesture.HOLA


def test_seis_requires_two_hands() -> None:
    config = make_config(
        {
            Gesture.HOLA: {"enabled": False},
            Gesture.SI: {"enabled": False},
            Gesture.NO: {"enabled": False},
            Gesture.SEIS_SIETE: {
                "hold_ms": 0,
                "cooldown_s": 0.0,
                "min_cycles": 2,
                "min_correlation": 0.5,
                "require_two_hands": True,
                "require_raised": False,
            },
        }
    )
    tracker = GestureTracker(config, clock=lambda: 0.0)
    left, right = _shoulders()
    events = []
    t = 0.0
    for _ in range(int(3.0 * FPS)):
        ly = 0.4 + 0.1 * math.sin(2 * math.pi * t / 0.6)
        ry = 0.4 - 0.1 * math.sin(2 * math.pi * t / 0.6)
        # Sin manos detectadas: aunque las muñecas oscilen, no debe emitir.
        frame = make_frame(
            t,
            left_shoulder=left,
            right_shoulder=right,
            left_wrist=Landmark(0.3, ly),
            right_wrist=Landmark(0.7, ry),
        )
        events.extend(tracker.update(frame, now=t))
        t += DT
    assert events == []


def test_nose_jitter_does_not_trigger_si_no() -> None:
    """Ruido pequeño de landmarks no debe contar como asentir/negar."""

    config = make_config(
        {
            Gesture.HOLA: {"enabled": False},
            Gesture.SEIS_SIETE: {"enabled": False},
            Gesture.SI: {
                "hold_ms": 250,
                "cooldown_s": 2.0,
                "min_direction_changes": 3,
                "min_amplitude_ratio": 0.05,
                "min_step_ratio": 0.012,
                "dominance_ratio": 1.5,
                "smoothing_alpha": 0.6,
                "min_samples": 6,
                "min_confidence": 0.7,
            },
            Gesture.NO: {
                "hold_ms": 250,
                "cooldown_s": 2.0,
                "min_direction_changes": 3,
                "min_amplitude_ratio": 0.05,
                "min_step_ratio": 0.012,
                "dominance_ratio": 1.5,
                "smoothing_alpha": 0.6,
                "min_samples": 6,
                "min_confidence": 0.7,
            },
        }
    )
    tracker = GestureTracker(config, clock=lambda: 0.0)
    left, right = _shoulders()
    events = []
    t = 0.0
    for index in range(int(3.0 * FPS)):
        jitter_x = 0.006 if index % 2 == 0 else -0.006
        jitter_y = -0.006 if index % 3 == 0 else 0.005
        nose = Landmark(0.5 + jitter_x, 0.5 + jitter_y)
        frame = make_frame(
            t, nose=nose, left_shoulder=left, right_shoulder=right
        )
        events.extend(tracker.update(frame, now=t))
        t += DT
    assert events == []


def test_diagonal_nose_motion_does_not_trigger_si_or_no() -> None:
    """Movimiento diagonal con ejes similares no debe elegir SI ni NO."""

    config = make_config(
        {
            Gesture.HOLA: {"enabled": False},
            Gesture.SEIS_SIETE: {"enabled": False},
            Gesture.SI: {
                "hold_ms": 100,
                "cooldown_s": 0.0,
                "min_direction_changes": 2,
                "min_amplitude_ratio": 0.02,
                "min_step_ratio": 0.01,
                "dominance_ratio": 1.5,
                "smoothing_alpha": 0.6,
                "min_samples": 6,
                "min_confidence": 0.1,
            },
            Gesture.NO: {
                "hold_ms": 100,
                "cooldown_s": 0.0,
                "min_direction_changes": 2,
                "min_amplitude_ratio": 0.02,
                "min_step_ratio": 0.01,
                "dominance_ratio": 1.5,
                "smoothing_alpha": 0.6,
                "min_samples": 6,
                "min_confidence": 0.1,
            },
        }
    )
    tracker = GestureTracker(config, clock=lambda: 0.0)
    left, right = _shoulders()
    events = []
    t = 0.0
    for _ in range(int(2.5 * FPS)):
        delta = 0.05 * math.sin(2 * math.pi * t / 0.6)
        nose = Landmark(0.5 + delta, 0.5 + delta)
        frame = make_frame(
            t, nose=nose, left_shoulder=left, right_shoulder=right
        )
        events.extend(tracker.update(frame, now=t))
        t += DT
    assert events == []
