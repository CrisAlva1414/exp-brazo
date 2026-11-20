"""Seguimiento temporal de gestos sobre frames de percepción.

Mantiene buffers deslizantes por gesto, evalúa los detectores puros y emite
``PerceptionEvent`` aplicando ``hold_ms`` y ``cooldown_s``. Todo es
thread-safe; los umbrales son heurísticos y **SIN VALIDAR** con personas
reales.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

from ..contracts import Gesture, PerceptionConfig, PerceptionEvent
from .detectors import (
    DetectionResult,
    clamp01,
    detect_hola,
    detect_shake,
    detect_six_seven,
    hand_open,
    index_only,
    thumbs_up,
)
from .landmarks import HAND_INDEX, FrameLandmarks, Hand, Landmark, Pose

# Buffers por señal: HOLA usa muñeca x; NO usa punta del índice; SEIS ambas muñecas.
_HOLA = "HOLA"
_NO_INDEX = "NO_INDEX"
_SEIS_LEFT = "SEIS_LEFT"
_SEIS_RIGHT = "SEIS_RIGHT"

_WINDOW_DEFAULT_MS = 1500


class GestureTracker:
    """Acumula señales y decide cuándo emitir un gesto."""

    def __init__(
        self,
        perception_config: PerceptionConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.RLock()
        self._clock = clock
        self._config = perception_config
        self._buffers: dict[str, deque[tuple[float, float]]] = {
            key: deque() for key in (_HOLA, _NO_INDEX, _SEIS_LEFT, _SEIS_RIGHT)
        }
        self._candidate_since: dict[Gesture, float] = {}
        self._last_emit: dict[Gesture, float] = {}
        self._pose_latched: dict[Gesture, bool] = {}
        self._metrics: dict[Gesture, dict] = {}
        self._last_hand_size = 1e-6
        self._last_face_size = 1e-6
        self._last_any_emit = float("-inf")

    # -- configuración y estado -------------------------------------------
    def update_config(self, perception_config: PerceptionConfig) -> None:
        """Reemplaza la configuración de percepción en caliente."""

        with self._lock:
            self._config = perception_config

    def reset(self) -> None:
        """Limpia buffers, candidatos, cooldowns y métricas."""

        with self._lock:
            for buffer in self._buffers.values():
                buffer.clear()
            self._candidate_since.clear()
            self._last_emit.clear()
            self._metrics.clear()
            self._pose_latched.clear()
            self._last_hand_size = 1e-6
            self._last_face_size = 1e-6
            self._last_any_emit = float("-inf")

    def metrics(self) -> dict[Gesture, dict]:
        """Última confianza y métricas por gesto (para overlay/dashboard)."""

        with self._lock:
            return {gesture: dict(values) for gesture, values in self._metrics.items()}

    # -- API principal -----------------------------------------------------
    def update(self, frame: FrameLandmarks, now: float | None = None) -> list[PerceptionEvent]:
        """Procesa un frame y devuelve 0..n eventos de gesto."""

        with self._lock:
            t = float(self._clock() if now is None else now)
            if frame.pose is None and not frame.hands:
                return []

            events: list[PerceptionEvent] = []
            self._face_size(frame.pose)

            hand, hand_size = self._select_open_hand(frame.hands)
            hola_settings = self._settings(Gesture.HOLA)
            if hand is not None:
                self._last_hand_size = max(hand_size, 1e-6)
                wrist = hand.landmarks[HAND_INDEX["WRIST"]]
                raised_ok = not (hola_settings and hola_settings.require_raised)
                if hola_settings and hola_settings.require_raised:
                    raised_ok = self._is_raised(frame.pose, wrist)
                if raised_ok:
                    self._append(_HOLA, t, wrist.x, Gesture.HOLA)
                    self._evaluate_hola(t, events)
                else:
                    self._mark_no_signal(Gesture.HOLA)
            else:
                self._mark_no_signal(Gesture.HOLA)

            thumbs_hand, _ = self._select_hand(
                frame.hands, lambda hand: thumbs_up(hand).detected
            )
            if thumbs_hand is not None:
                self._handle_result(Gesture.SI, thumbs_up(thumbs_hand), t, events)
            else:
                self._mark_no_signal(Gesture.SI)

            index_hand, index_size = self._select_hand(
                frame.hands, lambda hand: index_only(hand).detected
            )
            if index_hand is not None:
                tip = index_hand.landmarks[HAND_INDEX["INDEX_TIP"]]
                self._append(_NO_INDEX, t, tip.x, Gesture.NO)
                no_settings = self._settings(Gesture.NO)
                values = self._values(_NO_INDEX, t, Gesture.NO)
                result = detect_shake(
                    values,
                    max(index_size, 1e-6),
                    no_settings.min_direction_changes if no_settings else 3,
                    no_settings.min_amplitude_ratio if no_settings else 0.1,
                    min_step_ratio=no_settings.min_step_ratio if no_settings else 0.012,
                    smoothing_alpha=no_settings.smoothing_alpha if no_settings else 0.6,
                    min_samples=no_settings.min_samples if no_settings else 7,
                )
                self._handle_result(Gesture.NO, result, t, events)
            else:
                self._mark_no_signal(Gesture.NO)

            seis_settings = self._settings(Gesture.SEIS_SIETE)
            seis_visibility = seis_settings.min_visibility if seis_settings else 0.6
            left = self._pose_point(
                frame.pose, "LEFT_WRIST", min_visibility=seis_visibility
            )
            right = self._pose_point(
                frame.pose, "RIGHT_WRIST", min_visibility=seis_visibility
            )
            two_hands_ok = not (seis_settings and seis_settings.require_two_hands) or (
                len(frame.hands) >= 2
            )
            raised_ok = True
            if seis_settings and seis_settings.require_raised:
                raised_ok = (
                    left is not None
                    and right is not None
                    and self._is_raised(frame.pose, left)
                    and self._is_raised(frame.pose, right)
                )
            if left is not None and right is not None and two_hands_ok and raised_ok:
                self._append(_SEIS_LEFT, t, left.y, Gesture.SEIS_SIETE)
                self._append(_SEIS_RIGHT, t, right.y, Gesture.SEIS_SIETE)
                self._evaluate_six_seven(t, events)
            else:
                self._mark_no_signal(Gesture.SEIS_SIETE)

            return events

    # -- evaluación --------------------------------------------------------
    def _settings(self, gesture: Gesture):
        return self._config.gestures.get(gesture)

    def _evaluate_hola(self, t: float, events: list[PerceptionEvent]) -> None:
        settings = self._settings(Gesture.HOLA)
        values = self._values(_HOLA, t, Gesture.HOLA)
        result = detect_hola(
            values,
            self._last_hand_size,
            settings.min_direction_changes if settings else 1,
            settings.min_amplitude_ratio if settings else 0.0,
            min_step_ratio=settings.min_step_ratio if settings else 0.01,
            smoothing_alpha=settings.smoothing_alpha if settings else 0.65,
            min_samples=settings.min_samples if settings else 5,
        )
        self._handle_result(Gesture.HOLA, result, t, events)

    def _evaluate_six_seven(self, t: float, events: list[PerceptionEvent]) -> None:
        settings = self._settings(Gesture.SEIS_SIETE)
        left = self._values(_SEIS_LEFT, t, Gesture.SEIS_SIETE)
        right = self._values(_SEIS_RIGHT, t, Gesture.SEIS_SIETE)
        result = detect_six_seven(
            left,
            right,
            settings.min_cycles if settings else 1,
            settings.min_correlation if settings else 0.5,
            min_step_ratio=settings.min_step_ratio if settings else 0.008,
            smoothing_alpha=settings.smoothing_alpha if settings else 0.6,
            min_samples=settings.min_samples if settings else 8,
        )
        self._handle_result(Gesture.SEIS_SIETE, result, t, events)

    def _handle_result(
        self,
        gesture: Gesture,
        result: DetectionResult,
        t: float,
        events: list[PerceptionEvent],
    ) -> None:
        settings = self._settings(gesture)
        valid = (
            settings is not None
            and settings.enabled
            and result.detected
            and result.confidence >= settings.min_confidence
        )
        self._metrics[gesture] = {
            "confidence": float(result.confidence) if valid else 0.0,
            "raw_confidence": float(result.confidence),
            "detected": bool(valid),
            **result.metrics,
        }

        if not valid:
            self._candidate_since.pop(gesture, None)
            if gesture is Gesture.SI:
                self._pose_latched[gesture] = False
            return

        if gesture is Gesture.SI and self._pose_latched.get(gesture):
            self._candidate_since.pop(gesture, None)
            return

        since = self._candidate_since.setdefault(gesture, t)
        held_s = max(settings.hold_ms, 0) / 1000.0
        last = self._last_emit.get(gesture, float("-inf"))
        global_gap = float(getattr(self._config, "global_cooldown_s", 1.0))
        if global_gap > 0 and (t - self._last_any_emit) < global_gap:
            self._candidate_since.pop(gesture, None)
            return
        if (t - since) >= held_s and (t - last) >= settings.cooldown_s:
            events.append(
                PerceptionEvent(
                    gesture=gesture,
                    confidence=clamp01(result.confidence),
                    timestamp=time.time(),
                    source="camera",
                )
            )
            self._last_emit[gesture] = t
            self._last_any_emit = t
            if gesture is Gesture.SI:
                self._pose_latched[gesture] = True
            self._clear_gesture(gesture)
            self._candidate_since.pop(gesture, None)

    # -- buffers -----------------------------------------------------------
    def _window_ms(self, gesture: Gesture) -> int:
        settings = self._settings(gesture)
        return settings.window_ms if settings else _WINDOW_DEFAULT_MS

    def _append(self, key: str, t: float, value: float, gesture: Gesture) -> None:
        buffer = self._buffers[key]
        buffer.append((t, float(value)))
        cutoff = t - self._window_ms(gesture) / 1000.0
        while buffer and buffer[0][0] < cutoff:
            buffer.popleft()

    def _values(self, key: str, t: float, gesture: Gesture) -> list[float]:
        cutoff = t - self._window_ms(gesture) / 1000.0
        buffer = self._buffers[key]
        while buffer and buffer[0][0] < cutoff:
            buffer.popleft()
        return [value for _, value in buffer]

    def _clear_gesture(self, gesture: Gesture) -> None:
        if gesture is Gesture.SEIS_SIETE:
            self._buffers[_SEIS_LEFT].clear()
            self._buffers[_SEIS_RIGHT].clear()
        elif gesture is Gesture.NO:
            self._buffers[_NO_INDEX].clear()
        elif gesture is Gesture.HOLA:
            self._buffers[_HOLA].clear()

    def _mark_no_signal(self, gesture: Gesture) -> None:
        self._candidate_since.pop(gesture, None)
        self._metrics[gesture] = {"confidence": 0.0, "detected": False, "samples": 0}

    # -- selección de landmarks -------------------------------------------
    @staticmethod
    def _bbox_diagonal(landmarks: list[Landmark]) -> float:
        if not landmarks:
            return 0.0
        xs = [landmark.x for landmark in landmarks]
        ys = [landmark.y for landmark in landmarks]
        return ((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2) ** 0.5

    def _select_hand(self, hands: list[Hand], predicate) -> tuple[Hand | None, float]:
        best: Hand | None = None
        best_size = 0.0
        for hand in hands:
            if len(hand.landmarks) < 21 or not predicate(hand):
                continue
            size = self._bbox_diagonal(hand.landmarks)
            if size >= best_size:
                best = hand
                best_size = size
        return best, best_size

    def _select_open_hand(self, hands: list[Hand]) -> tuple[Hand | None, float]:
        best: Hand | None = None
        best_size = 0.0
        for hand in hands:
            if len(hand.landmarks) < 21 or not hand_open(hand):
                continue
            size = self._bbox_diagonal(hand.landmarks)
            if size >= best_size:
                best = hand
                best_size = size
        if best is None:
            return None, self._last_hand_size
        wrist = best.landmarks[HAND_INDEX["WRIST"]]
        middle = best.landmarks[HAND_INDEX["MIDDLE_MCP"]]
        wrist_middle = (
            (wrist.x - middle.x) ** 2 + (wrist.y - middle.y) ** 2 + (wrist.z - middle.z) ** 2
        ) ** 0.5
        return best, max(best_size, wrist_middle, 1e-6)

    @staticmethod
    def _pose_point(pose: Pose | None, name: str, min_visibility: float = 0.5) -> Landmark | None:
        if pose is None:
            return None
        landmark = pose.landmarks.get(name)
        if landmark is None or landmark.visibility < min_visibility:
            return None
        return landmark

    def _torso_mid_y(self, pose: Pose | None) -> float | None:
        """Altura media del torso en coordenadas de imagen (y crece hacia abajo)."""

        if pose is None:
            return None
        shoulders = [
            self._pose_point(pose, "LEFT_SHOULDER"),
            self._pose_point(pose, "RIGHT_SHOULDER"),
        ]
        hips = [
            self._pose_point(pose, "LEFT_HIP"),
            self._pose_point(pose, "RIGHT_HIP"),
        ]
        shoulder_y = [point.y for point in shoulders if point is not None]
        hip_y = [point.y for point in hips if point is not None]
        if not shoulder_y:
            return None
        if hip_y:
            return (min(shoulder_y) + max(hip_y)) / 2.0
        return min(shoulder_y) + self._last_face_size * 0.5

    def _is_raised(self, pose: Pose | None, point: Landmark) -> bool:
        """Indica si la mano está por encima de la línea media del torso."""

        mid = self._torso_mid_y(pose)
        if mid is None:
            return False
        return point.y <= mid

    def _face_size(self, pose: Pose | None) -> float:
        for left_name, right_name in (
            ("LEFT_SHOULDER", "RIGHT_SHOULDER"),
            ("LEFT_EAR", "RIGHT_EAR"),
        ):
            left = self._pose_point(pose, left_name)
            right = self._pose_point(pose, right_name)
            if left is None or right is None:
                continue
            size = ((left.x - right.x) ** 2 + (left.y - right.y) ** 2) ** 0.5
            if size > 0:
                self._last_face_size = max(size, 1e-6)
                return self._last_face_size
        return self._last_face_size
