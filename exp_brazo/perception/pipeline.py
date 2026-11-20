"""Pipeline de cámara: captura, inferencia, tracking y anotación.

La captura corre en un hilo daemon. Si la cámara falla, el hilo no muere:
marca ``camera_ok=False``, expone ``last_error`` y reintenta con backoff.
``cv2`` y ``mediapipe`` se importan de forma perezosa; sin ellos el pipeline
sigue operando con el engine inyectado (por ejemplo ``NullEngine``).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from ..contracts import CameraConfig, Gesture, PerceptionConfig, PerceptionEvent
from .engine import MediaPipeEngine, PerceptionEngine
from .landmarks import FrameLandmarks
from .tracker import GestureTracker

_LOGGER = logging.getLogger(__name__)


class CameraPipeline:
    """Orquesta captura, detección, tracking y publicación de frames."""

    def __init__(
        self,
        camera: CameraConfig,
        perception: PerceptionConfig,
        on_event: Callable[[PerceptionEvent], None],
        *,
        engine: PerceptionEngine | None = None,
        capture_factory: Callable[[CameraConfig], object] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.RLock()
        self.camera = camera
        self.perception = perception
        self._on_event = on_event
        self._engine = engine
        self._engine_provided = engine is not None
        self._capture_factory = capture_factory
        self._clock = clock
        self._tracker = GestureTracker(perception, clock=clock)

        self.camera_ok = False
        self.last_error: str | None = None
        # Resolución y FPS realmente reportados por el driver al abrir.
        self.camera_actual: dict | None = None
        self.fps = 0.0
        self.process_fps = 0.0
        self.latency_ms = 0.0
        self.frames = 0
        self.processed_frames = 0
        self.last_frame_ts: float | None = None
        self.heartbeat_ts = clock()
        self.running = False

        self._latest_jpeg: bytes | None = None
        self._latest_frame = None
        self._last_loop_ts: float | None = None
        self._last_detect_ts: float | None = None
        self._last_process_ts: float | None = None
        self._stop_event = threading.Event()
        self._first_frame = threading.Event()
        self._thread: threading.Thread | None = None

    # -- ciclo de vida -----------------------------------------------------
    def start(self) -> None:
        """Arranca el hilo de captura si no está activo."""

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self.running = True
            self.heartbeat_ts = self._clock()
            self._thread = threading.Thread(
                target=self._loop, name="camera-pipeline", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """Detiene el hilo y espera su finalización (con timeout)."""

        self._stop_event.set()
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=2.0)
        with self._lock:
            self.running = False
            engine = self._engine
            if engine is not None and not self._engine_provided:
                # El engine lo creó el pipeline: se libera para que start/restart
                # lo reconstruyan. Un engine inyectado pertenece al llamador.
                try:
                    engine.close()
                except Exception:
                    pass
                self._engine = None

    def restart(self) -> None:
        """Reinicia el pipeline conservando la configuración actual."""

        self.stop()
        self.start()

    def update_config(self, camera: CameraConfig, perception: PerceptionConfig) -> None:
        """Actualiza configuraciones de cámara y percepción en caliente."""

        with self._lock:
            self.camera = camera
            self.perception = perception
            self._tracker.update_config(perception)

    # -- consulta ----------------------------------------------------------
    def status(self) -> dict:
        """Estado resumido apto para JSON (dashboard/overlay)."""

        with self._lock:
            gestures = {
                gesture.value: dict(values) for gesture, values in self._tracker.metrics().items()
            }
            heartbeat_age = max(0.0, self._clock() - self.heartbeat_ts)
            return {
                "camera_ok": self.camera_ok,
                "running": self.running,
                "fps": round(self.fps, 2),
                "process_fps": round(self.process_fps, 2),
                "detection_fps": int(self.perception.detection_fps),
                "latency_ms": round(self.latency_ms, 2),
                "frames": self.frames,
                "processed_frames": self.processed_frames,
                "last_error": self.last_error,
                "heartbeat_age_s": round(heartbeat_age, 2),
                "camera_actual": dict(self.camera_actual) if self.camera_actual else None,
                "model_fallback": getattr(self._engine, "model_fallback", None),
                "hand_model_fallback": getattr(self._engine, "hand_model_fallback", None),
                "gestures": gestures,
            }

    def get_jpeg(self) -> bytes | None:
        """Último frame anotado codificado en JPEG, o ``None``."""

        with self._lock:
            return self._latest_jpeg

    def snapshot(self):
        """Último frame BGR sin codificar, o ``None``."""

        with self._lock:
            return self._latest_frame

    def wait_first_frame(self, timeout: float = 2.0) -> bool:
        """Espera hasta que se procese el primer frame."""

        return self._first_frame.wait(timeout)

    # -- hilo de captura ---------------------------------------------------
    def _loop(self) -> None:
        engine_backoff = 0.5
        try:
            while not self._stop_event.is_set():
                engine = self._ensure_engine()
                if engine is None:
                    self._stop_event.wait(engine_backoff)
                    engine_backoff = min(engine_backoff * 2.0, 5.0)
                    continue
                engine_backoff = 0.5

                capture = self._open_capture()
                if capture is None:
                    self._stop_event.wait(2.0)
                    continue

                try:
                    self._capture_loop(capture, engine)
                finally:
                    self._release_capture(capture)
        finally:
            with self._lock:
                self.running = False

    def _ensure_engine(self) -> PerceptionEngine | None:
        if self._engine is not None:
            return self._engine
        try:
            self._engine = MediaPipeEngine(self.camera, self.perception)
        except Exception as exc:
            self._set_camera_failure(f"MediaPipe no disponible: {exc}")
            return None
        return self._engine

    def _open_capture(self):
        if self._capture_factory is not None:
            try:
                return self._capture_factory(self.camera)
            except Exception as exc:
                self._set_camera_failure(f"no se pudo abrir el origen de video: {exc}")
                return None

        try:
            import cv2
        except Exception as exc:
            self._set_camera_failure(f"OpenCV no disponible: {exc}")
            return None

        try:
            capture = cv2.VideoCapture(self.camera.index)
            self._apply_capture_settings(capture, cv2)
            if hasattr(capture, "isOpened") and not capture.isOpened():
                capture.release()
                self._set_camera_failure("cámara no disponible")
                return None
            return capture
        except Exception as exc:
            self._set_camera_failure(f"error abriendo la cámara: {exc}")
            return None

    def _apply_capture_settings(self, capture, cv2_module) -> None:
        """Aplica resolución, FPS, FOURCC y buffer sin romper si el backend no lo admite."""

        try:
            capture.set(cv2_module.CAP_PROP_FRAME_WIDTH, self.camera.width)
            capture.set(cv2_module.CAP_PROP_FRAME_HEIGHT, self.camera.height)
            capture.set(cv2_module.CAP_PROP_FPS, self.camera.fps)
        except Exception as exc:
            _LOGGER.warning("no se pudo configurar resolución/FPS de la cámara: %s", exc)

        fourcc = (self.camera.fourcc or "").strip()
        if fourcc:
            if len(fourcc) != 4:
                _LOGGER.warning(
                    "fourcc inválido %r (se esperan 4 caracteres); se usa el del driver",
                    fourcc,
                )
            else:
                try:
                    code = cv2_module.VideoWriter_fourcc(*fourcc)
                    capture.set(cv2_module.CAP_PROP_FOURCC, code)
                except Exception as exc:
                    _LOGGER.warning("no se pudo aplicar fourcc %s: %s", fourcc, exc)

        try:
            accepted = capture.set(cv2_module.CAP_PROP_BUFFERSIZE, 1)
            if accepted is False:
                _LOGGER.debug("el backend de cámara no aceptó CAP_PROP_BUFFERSIZE=1")
        except Exception as exc:
            _LOGGER.debug("CAP_PROP_BUFFERSIZE no soportado por el backend: %s", exc)

        actual: dict = {"fourcc": fourcc or None}
        try:
            actual["width"] = int(capture.get(cv2_module.CAP_PROP_FRAME_WIDTH))
            actual["height"] = int(capture.get(cv2_module.CAP_PROP_FRAME_HEIGHT))
            actual["fps"] = round(float(capture.get(cv2_module.CAP_PROP_FPS)), 2)
        except Exception as exc:
            _LOGGER.warning("no se pudo leer la resolución/FPS reales de la cámara: %s", exc)
            actual["width"] = None
            actual["height"] = None
            actual["fps"] = None

        with self._lock:
            self.camera_actual = actual
        _LOGGER.info("cámara configurada: solicitado=%s actual=%s", self.camera, actual)

    def _capture_loop(self, capture, engine: PerceptionEngine) -> None:
        while not self._stop_event.is_set():
            try:
                ok, frame = capture.read()
            except Exception as exc:
                self._set_camera_failure(f"error leyendo la cámara: {exc}")
                self._stop_event.wait(0.5)
                return

            if not ok or frame is None:
                self._set_camera_failure("cámara no disponible")
                self._stop_event.wait(2.0)
                return

            self._handle_frame(frame, engine)

    def _should_detect(self, moment: float) -> bool:
        """Indica si corresponde inferir en este frame según ``detection_fps``."""

        detection_fps = int(self.perception.detection_fps)
        if detection_fps <= 0 or self._last_detect_ts is None:
            return True
        # Epsilon para no perder frames por redondeo en el borde del intervalo.
        return (moment - self._last_detect_ts) >= ((1.0 / detection_fps) - 1e-9)

    def _handle_frame(self, frame, engine: PerceptionEngine) -> None:
        moment = self._clock()
        should_detect = self._should_detect(moment)

        with self._lock:
            self.camera_ok = True
            self.last_error = None
            self.frames += 1
            self.last_frame_ts = moment
            self.heartbeat_ts = moment
            if self._last_loop_ts is not None:
                delta = moment - self._last_loop_ts
                if delta > 0:
                    instant = 1.0 / delta
                    self.fps = instant if self.fps <= 0 else 0.9 * self.fps + 0.1 * instant
            self._last_loop_ts = moment
            if not should_detect:
                # Frame salteado: se publica crudo (sin overlay) para que la
                # vista no se congele, y no se ejecuta MediaPipe.
                self._latest_frame = frame

        if should_detect:
            self._process_frame(frame, engine, moment)

    def _process_frame(self, frame, engine: PerceptionEngine, moment: float) -> None:
        started = time.perf_counter()
        frame_landmarks = engine.process(frame, ts=moment)
        events = self._tracker.update(frame_landmarks, now=moment)
        latency_ms = (time.perf_counter() - started) * 1000.0

        annotated = self._annotate(frame, frame_landmarks)
        jpeg = self._encode_jpeg(annotated)

        with self._lock:
            self.camera_ok = True
            self.last_error = None
            self.processed_frames += 1
            self._last_detect_ts = moment
            if self._last_process_ts is not None:
                delta = moment - self._last_process_ts
                if delta > 0:
                    instant = 1.0 / delta
                    self.process_fps = (
                        instant if self.process_fps <= 0 else 0.9 * self.process_fps + 0.1 * instant
                    )
            self._last_process_ts = moment
            self.latency_ms = latency_ms
            self._latest_frame = frame
            if jpeg is not None:
                self._latest_jpeg = jpeg

        self._first_frame.set()
        for event in events:
            try:
                self._on_event(event)
            except Exception:
                # Un callback defectuoso no debe tumbar el hilo de captura.
                continue

    def _set_camera_failure(self, message: str) -> None:
        with self._lock:
            self.camera_ok = False
            self.last_error = message
            self.heartbeat_ts = self._clock()

    @staticmethod
    def _release_capture(capture) -> None:
        try:
            capture.release()
        except Exception:
            return None

    # -- anotación ---------------------------------------------------------
    def _annotate(self, frame, frame_landmarks: FrameLandmarks):
        try:
            import cv2
        except Exception:
            return frame

        try:
            image = frame.copy() if hasattr(frame, "copy") else frame
            shape = getattr(frame, "shape", None)
            if shape is not None and len(shape) >= 2:
                height, width = int(shape[0]), int(shape[1])
            else:
                height, width = frame_landmarks.height, frame_landmarks.width
            if width <= 0 or height <= 0:
                return frame

            if frame_landmarks.pose is not None:
                for landmark in frame_landmarks.pose.landmarks.values():
                    center = (int(landmark.x * width), int(landmark.y * height))
                    cv2.circle(image, center, 3, (0, 220, 0), -1)
            for hand in frame_landmarks.hands:
                for landmark in hand.landmarks:
                    center = (int(landmark.x * width), int(landmark.y * height))
                    cv2.circle(image, center, 2, (0, 180, 255), -1)

            panel_height = 82
            cv2.rectangle(image, (0, 0), (width, panel_height), (24, 24, 24), -1)
            camera_text = "CAM ok" if self.camera_ok else "CAM fail"
            cv2.putText(
                image,
                f"{camera_text}  FPS {self.fps:4.1f}  LAT {self.latency_ms:4.1f}ms",
                (8, 20),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (255, 255, 255),
                1,
                cv2.LINE_AA,
            )
            cv2.putText(
                image,
                "SIN VALIDAR",
                (max(width - 110, 8), 20),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                (0, 200, 255),
                1,
                cv2.LINE_AA,
            )

            metrics = self._tracker.metrics()
            y = 40
            for gesture in (Gesture.HOLA, Gesture.SI, Gesture.NO, Gesture.SEIS_SIETE):
                confidence = float(metrics.get(gesture, {}).get("confidence", 0.0))
                confidence = max(0.0, min(1.0, confidence))
                cv2.putText(
                    image,
                    gesture.value,
                    (8, y),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.4,
                    (220, 220, 220),
                    1,
                    cv2.LINE_AA,
                )
                cv2.rectangle(image, (110, y - 8), (210, y + 2), (70, 70, 70), -1)
                cv2.rectangle(
                    image,
                    (110, y - 8),
                    (110 + int(100 * confidence), y + 2),
                    (0, 200, 0),
                    -1,
                )
                y += 18
            return image
        except Exception:
            return frame

    def _encode_jpeg(self, frame) -> bytes | None:
        try:
            import cv2
        except Exception:
            return None
        try:
            ok, buffer = cv2.imencode(".jpg", frame)
            if ok:
                return buffer.tobytes()
        except Exception:
            return None
        return None
