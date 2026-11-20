"""Supervisor de capas: heartbeat, degradación y modo ambiental.

El supervisor corre en un hilo daemon y no debe tumbar el proceso si una
capa falla: toda excepción se captura y se refleja en ``last_error``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from .contracts import RobotState, RunMode, SupervisorConfig

_LOGGER = logging.getLogger(__name__)
_DEFAULT_BACKOFF = (1.0, 2.0, 4.0, 8.0, 15.0)


class Supervisor:
    """Vigila cámara, percepción, decisión, adaptador y su propio hilo."""

    def __init__(
        self,
        *,
        pipeline,
        adapter,
        controller,
        config: SupervisorConfig,
        on_ambient: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.pipeline = pipeline
        self.adapter = adapter
        self.controller = controller
        self.config = config
        self.on_ambient = on_ambient
        self.clock = clock

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error: str | None = None

        self._camera_down_since: float | None = None
        self._last_restart_ts: float | None = None
        self._restart_count = 0
        self._last_ambient_ts = self.clock()

        self._adapter_down_since: float | None = None
        self._adapter_restart_ts: float | None = None
        self._adapter_restart_count = 0
        self._adapter_reconnecting = False

    # -- ciclo de vida -----------------------------------------------------
    def start(self) -> None:
        """Arranca el hilo de supervisión si no está activo."""

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="exp-brazo-supervisor",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """Detiene el hilo de supervisión y espera su finalización."""

        self._stop_event.set()
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=2.0)
        with self._lock:
            self._thread = None

    # -- consulta y diagnóstico -------------------------------------------
    def status(self) -> dict:
        """Estado de cada capa y último error del supervisor."""

        layers = self._evaluate()
        return {
            "camera": layers["camera"],
            "perception": layers["perception"],
            "decision": layers["decision"],
            "adapter": layers["adapter"],
            "supervisor": layers["supervisor"],
            "layers": layers,
            "last_error": self._last_error,
            "adapter_reconnect_count": self._adapter_restart_count,
            "adapter_reconnecting": self._adapter_reconnecting,
        }

    def force_check(self) -> dict:
        """Ejecuta un ciclo completo sin esperar al intervalo del hilo.

        Pensado para pruebas deterministas con ``clock`` inyectado.
        """

        try:
            layers = self._evaluate()
            self._apply_degradation(layers)
        except Exception as exc:  # el supervisor nunca debe propagar
            self._set_error(f"error en force_check: {exc}")
        return self._evaluate()

    # -- hilo -------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop_event.wait(0.5):
            try:
                layers = self._evaluate()
                self._apply_degradation(layers)
            except Exception as exc:  # pragma: no cover - salvaguarda
                self._set_error(f"error en ciclo del supervisor: {exc}")

    def _evaluate(self) -> dict[str, dict]:
        layers: dict[str, dict] = {}
        pipeline_status: dict = {}

        if self.pipeline is None:
            layers["camera"] = {"ok": False, "detail": "sin pipeline de cámara"}
            layers["perception"] = {"ok": False, "detail": "sin pipeline de cámara"}
        else:
            try:
                pipeline_status = self.pipeline.status() or {}
            except Exception as exc:
                pipeline_status = {}
                self._set_error(f"pipeline.status() falló: {exc}")

            camera_ok = bool(pipeline_status.get("camera_ok", False))
            camera_detail = str(
                pipeline_status.get("last_error")
                or ("cámara disponible" if camera_ok else "cámara no disponible")
            )
            layers["camera"] = {"ok": camera_ok, "detail": camera_detail}

            running = bool(pipeline_status.get("running", False))
            try:
                heartbeat_age = float(pipeline_status.get("heartbeat_age_s", 0.0) or 0.0)
            except (TypeError, ValueError):
                heartbeat_age = float("inf")
            perception_ok = running and heartbeat_age < self.config.heartbeat_timeout_s
            if perception_ok:
                perception_detail = f"heartbeat {heartbeat_age:.2f}s"
            elif not running:
                perception_detail = "pipeline detenido"
            else:
                perception_detail = f"heartbeat vencido ({heartbeat_age:.2f}s)"
            layers["perception"] = {"ok": perception_ok, "detail": perception_detail}

        try:
            controller_status = self.controller.status()
            layers["decision"] = {
                "ok": True,
                "detail": (
                    f"modo {controller_status.get('mode', '?')} "
                    f"pausa={'sí' if controller_status.get('paused') else 'no'}"
                ),
            }
        except Exception as exc:
            layers["decision"] = {"ok": False, "detail": f"controlador sin respuesta: {exc}"}

        state = getattr(self.adapter, "state", RobotState.DISCONNECTED)
        adapter_ok = state in (RobotState.IDLE, RobotState.BUSY)
        layers["adapter"] = {
            "ok": adapter_ok,
            "detail": f"estado {getattr(state, 'value', state)}",
        }

        thread = self._thread
        supervisor_ok = thread is not None and thread.is_alive()
        layers["supervisor"] = {
            "ok": supervisor_ok,
            "detail": "hilo activo" if supervisor_ok else "hilo detenido",
        }
        return layers

    # -- degradación -------------------------------------------------------
    def _apply_degradation(self, layers: dict[str, dict]) -> None:
        now = self.clock()
        self._handle_camera(layers["camera"]["ok"], now)
        self._handle_ambient(layers["camera"]["ok"], now)
        self._handle_adapter(layers["adapter"]["ok"], now)

    def _handle_adapter(self, adapter_ok: bool, now: float) -> None:
        """Reconecta el adaptador Dobot en un hilo aparte si se cayó.

        El hilo del supervisor nunca se bloquea esperando al robot: el
        ``reconnect(attempts=1)`` corre en un thread daemon y el backoff se
        controla con ``_adapter_restart_ts``.
        """

        if getattr(self.adapter, "name", "") != "dobot":
            return

        if adapter_ok:
            self._adapter_down_since = None
            self._adapter_restart_ts = None
            self._adapter_restart_count = 0
            self._adapter_reconnecting = False
            return

        if self._adapter_down_since is None:
            self._adapter_down_since = now

        down_for = now - self._adapter_down_since
        if down_for < self.config.heartbeat_timeout_s:
            return
        if self._adapter_reconnecting:
            return

        backoff = list(self.config.restart_backoff_s) or list(_DEFAULT_BACKOFF)
        index = min(self._adapter_restart_count, len(backoff) - 1)
        interval = max(float(backoff[index]), 0.0)
        if self._adapter_restart_ts is not None and now - self._adapter_restart_ts < interval:
            return

        self._adapter_restart_ts = now
        self._adapter_restart_count += 1
        self._adapter_reconnecting = True
        try:
            threading.Thread(
                target=self._reconnect_adapter,
                name="exp-brazo-dobot-reconnect",
                daemon=True,
            ).start()
        except Exception as exc:
            self._adapter_reconnecting = False
            self._set_error(f"no se pudo lanzar la reconexión del adaptador: {exc}")

    def _reconnect_adapter(self) -> None:
        try:
            self.adapter.reconnect(attempts=1)
            _LOGGER.warning("reconexión del adaptador Dobot solicitada")
        except Exception as exc:
            self._set_error(f"no se pudo reconectar el adaptador Dobot: {exc}")
        finally:
            self._adapter_reconnecting = False

    def _handle_camera(self, camera_ok: bool, now: float) -> None:
        if camera_ok:
            self._camera_down_since = None
            self._last_restart_ts = None
            self._restart_count = 0
            return

        if self.pipeline is None:
            return

        if self._camera_down_since is None:
            self._camera_down_since = now
            return

        down_for = now - self._camera_down_since
        if down_for < self.config.heartbeat_timeout_s:
            return
        if not self._restart_due(now):
            return

        try:
            self.pipeline.restart()
            _LOGGER.warning("reinicio de cámara solicitado tras %.1fs caída", down_for)
        except Exception as exc:
            self._set_error(f"no se pudo reiniciar la cámara: {exc}")
        finally:
            self._restart_count += 1
            self._last_restart_ts = now

    def _restart_due(self, now: float) -> bool:
        if self._last_restart_ts is None:
            return True
        backoff = list(self.config.restart_backoff_s) or list(_DEFAULT_BACKOFF)
        index = min(max(self._restart_count - 1, 0), len(backoff) - 1)
        interval = max(float(backoff[index]), 0.0)
        return (now - self._last_restart_ts) >= interval

    def _handle_ambient(self, camera_ok: bool, now: float) -> None:
        if camera_ok or self.on_ambient is None:
            return
        try:
            controller_status = self.controller.status()
        except Exception:
            return
        if controller_status.get("paused"):
            return
        if controller_status.get("mode") != RunMode.AUTO.value:
            return
        interval = max(float(self.config.ambient_interval_s), 0.0)
        if now - self._last_ambient_ts < interval:
            return

        self._last_ambient_ts = now
        try:
            self.on_ambient()
            _LOGGER.info("modo ambiental: comportamiento suave disparado")
        except Exception as exc:
            self._set_error(f"falló el modo ambiental: {exc}")

    # -- utilidades --------------------------------------------------------
    def _set_error(self, message: str) -> None:
        with self._lock:
            self._last_error = message
        _LOGGER.warning("%s", message)
