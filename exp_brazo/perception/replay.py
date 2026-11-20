"""Reproducción de video y grabación de cámara para pruebas/diagnóstico.

Ambas clases importan ``cv2`` de forma perezosa. ``VideoReplay`` permite
validar detectores sobre grabaciones sin cámara; ``CameraRecorder`` graba
clips para reproducirlos después. No se usan en la suite automatizada.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from ..contracts import CameraConfig, PerceptionEvent
from .engine import PerceptionEngine
from .tracker import GestureTracker


class VideoReplay:
    """Reproduce un video aplicando engine y tracker por frame."""

    def __init__(
        self,
        path: Path,
        engine: PerceptionEngine,
        tracker: GestureTracker,
        *,
        source: str = "replay",
        on_event: Callable[[PerceptionEvent], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self.engine = engine
        self.tracker = tracker
        self.source = source
        self.on_event = on_event

    def run(self) -> dict:
        """Procesa el video completo y devuelve un resumen serializable."""

        try:
            import cv2
        except Exception as exc:
            return {
                "frames": 0,
                "events": [],
                "duration_s": 0.0,
                "fps": 0.0,
                "error": f"OpenCV no disponible: {exc}",
            }

        capture = cv2.VideoCapture(str(self.path))
        if not capture.isOpened():
            capture.release()
            return {
                "frames": 0,
                "events": [],
                "duration_s": 0.0,
                "fps": 0.0,
                "error": "no se pudo abrir el video",
            }

        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        frames = 0
        events: list[PerceptionEvent] = []
        error: str | None = None
        try:
            while True:
                ok, frame = capture.read()
                if not ok or frame is None:
                    break
                if fps > 0:
                    ts = float(capture.get(cv2.CAP_PROP_POS_MSEC)) / 1000.0
                else:
                    ts = frames / 30.0
                landmarks = self.engine.process(frame, ts=ts)
                new_events = self.tracker.update(landmarks, now=ts)
                for event in new_events:
                    events.append(event)
                    if self.on_event is not None:
                        try:
                            self.on_event(event)
                        except Exception:
                            continue
                frames += 1
        except Exception as exc:
            error = f"error procesando video: {exc}"
        finally:
            capture.release()

        duration_s = frames / fps if fps > 0 else 0.0
        return {
            "frames": frames,
            "events": [event.model_dump(mode="json") for event in events],
            "duration_s": duration_s,
            "fps": fps,
            "error": error,
        }


class CameraRecorder:
    """Graba video desde una cámara en un hilo daemon."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.RLock()
        self._clock = clock
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._path: Path | None = None
        self._frames = 0
        self._error: str | None = None
        self._started_ts: float | None = None
        self._running = False

    def start(self, path: Path, camera: CameraConfig, seconds: float) -> bool:
        """Inicia una grabación de hasta ``seconds`` segundos."""

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._error = "ya hay una grabación en curso"
                return False

            try:
                import cv2
            except Exception as exc:
                self._error = f"OpenCV no disponible: {exc}"
                return False

            self._path = Path(path)
            self._frames = 0
            self._error = None
            self._started_ts = self._clock()

            capture = cv2.VideoCapture(camera.index)
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, camera.width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, camera.height)
            capture.set(cv2.CAP_PROP_FPS, camera.fps)
            if hasattr(capture, "isOpened") and not capture.isOpened():
                capture.release()
                self._error = "cámara no disponible"
                return False

            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(
                str(self._path), fourcc, float(camera.fps), (camera.width, camera.height)
            )
            if hasattr(writer, "isOpened") and not writer.isOpened():
                capture.release()
                writer.release()
                self._error = "no se pudo crear el archivo de video"
                return False

            self._stop_event.clear()
            self._running = True
            self._thread = threading.Thread(
                target=self._record,
                args=(capture, writer, max(float(seconds), 0.0)),
                name="camera-recorder",
                daemon=True,
            )
            self._thread.start()
            return True

    def _record(self, capture, writer, seconds: float) -> None:
        try:
            while not self._stop_event.is_set():
                elapsed = self._clock() - (self._started_ts or self._clock())
                if seconds > 0 and elapsed >= seconds:
                    break
                ok, frame = capture.read()
                if not ok or frame is None:
                    with self._lock:
                        self._error = "cámara no disponible"
                    break
                writer.write(frame)
                with self._lock:
                    self._frames += 1
        finally:
            capture.release()
            writer.release()
            with self._lock:
                self._running = False

    def stop(self) -> None:
        """Detiene la grabación y espera al hilo."""

        self._stop_event.set()
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=2.0)
        with self._lock:
            self._running = False

    def status(self) -> dict:
        """Estado de la grabación."""

        with self._lock:
            elapsed = 0.0
            if self._started_ts is not None:
                elapsed = max(0.0, self._clock() - self._started_ts)
            return {
                "running": self._running,
                "frames": self._frames,
                "error": self._error,
                "path": str(self._path) if self._path is not None else None,
                "elapsed_s": round(elapsed, 2),
            }
