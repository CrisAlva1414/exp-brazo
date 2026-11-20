"""Runtime que une percepción, decisión, actuación y supervisión.

Incluye los adaptadores mock y Dobot real. ``cv2``/``mediapipe`` se importan
de forma perezosa a través de los módulos de percepción, por lo que el
runtime arranca degradado cuando no están instalados.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .behaviors import BehaviorCatalogStore, validate_behavior_set
from .config_store import ConfigError, ConfigStore
from .contracts import (
    JOINT_NAMES,
    AppConfig,
    Behavior,
    PerceptionEvent,
    RobotState,
)
from .perception.pipeline import CameraPipeline
from .perception.replay import CameraRecorder, VideoReplay
from .robot.mock import MockAdapter
from .state_machine import DecisionEngine, RobotController
from .supervisor import Supervisor

_LOGGER = logging.getLogger(__name__)

_CONTROLLER_LOG_KEYS = [
    "ts",
    "event_id",
    "gesture",
    "confidence",
    "decision",
    "behavior_id",
    "result",
    "error",
    "mode",
    "duration_ms",
]

_CSV_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: Any) -> str:
    """Neutraliza celdas CSV que una planilla podría interpretar como fórmula."""

    if value is None:
        return ""
    text = str(value)
    if text.startswith(_CSV_DANGEROUS_PREFIXES):
        return f"'{text}"
    return text


class Runtime:
    """Contenedor de componentes y operaciones de alto nivel."""

    def __init__(
        self,
        config_path: Path = Path("config/config.yaml"),
        behaviors_path: Path = Path("config/behaviors.yaml"),
        *,
        force_adapter: str | None = None,
        camera_enabled: bool = True,
        engine=None,
        adapter_factory=None,
        pipeline_factory=None,
        execute_inline: bool = False,
    ) -> None:
        self._lock = threading.RLock()
        self.config_path = Path(config_path)
        self.behaviors_path = Path(behaviors_path)

        self.config_store = ConfigStore(self.config_path, self._versions_dir())
        config = self.config_store.get()
        if force_adapter is not None:
            if force_adapter not in ("mock", "dobot"):
                raise ValueError("force_adapter debe ser 'mock' o 'dobot'")
            config.robot.adapter = force_adapter  # type: ignore[assignment]
        self.config = config
        self.force_adapter = force_adapter
        self.camera_enabled = bool(camera_enabled)
        self.execute_inline = bool(execute_inline)
        self._injected_engine = engine
        self.adapter_factory = adapter_factory
        self.pipeline_factory = pipeline_factory

        self.behavior_store = BehaviorCatalogStore(self.behaviors_path, self.config.robot)
        self.behavior_store.load()

        self.decision = DecisionEngine(self.behavior_store.get_all(), self.config.decision)
        self.adapter = self._build_adapter(self.config.robot)
        self.controller = RobotController(
            self.adapter,
            self.decision,
            busy_policy=self.config.decision.busy_policy,
            max_log=self.config.dashboard.log_max_entries,
            execute_inline=self.execute_inline,
        )

        self.pipeline = self._build_pipeline()
        self.supervisor = Supervisor(
            pipeline=self.pipeline,
            adapter=self.adapter,
            controller=self.controller,
            config=self.config.supervisor,
            on_ambient=self._trigger_ambient,
        )

        self.started_at: float | None = None
        self.replay: dict | None = None
        self.recorder: CameraRecorder | None = None
        self._events_count = 0
        self._last_error: str | None = None
        self._started = False

    # -- construcción ------------------------------------------------------
    def _versions_dir(self) -> Path:
        parent = self.config_path.parent
        if parent.name == "config":
            return parent.parent / "config_versions"
        return parent / "config_versions"

    def _build_adapter(self, robot_config):
        if self.adapter_factory is not None:
            return self.adapter_factory(robot_config)
        if robot_config.adapter == "mock":
            return MockAdapter(robot_config)
        if robot_config.adapter == "dobot":
            # Import perezoso: numpy/socket solo se cargan si se usa el robot real.
            from .robot.dobot import DobotAdapter

            return DobotAdapter(robot_config)
        raise RuntimeError(f"Adaptador de robot desconocido: {robot_config.adapter}")

    def _build_pipeline(self):
        if not self.camera_enabled:
            return None
        if self.pipeline_factory is not None:
            return self.pipeline_factory(
                self.config.camera,
                self.config.perception,
                self._on_perception_event,
            )
        return CameraPipeline(
            self.config.camera,
            self.config.perception,
            self._on_perception_event,
            engine=self._injected_engine,
        )

    # -- ciclo de vida -----------------------------------------------------
    def start(self) -> dict:
        with self._lock:
            if self._started:
                return self.status()

            try:
                connected = bool(self.adapter.connect())
            except Exception as exc:
                connected = False
                self._set_last_error(f"no se pudo conectar el adaptador: {exc}")
            if not connected:
                _LOGGER.warning("el adaptador %s no confirmó conexión", self.adapter.name)

            self.controller.start()
            if self.pipeline is not None:
                self.pipeline.start()
            self.supervisor.start()
            self.started_at = time.time()
            self._started = True
            _LOGGER.info(
                "runtime iniciado (adaptador=%s, cámara=%s)",
                self.adapter.name,
                "sí" if self.pipeline is not None else "no",
            )
        return self.status()

    def stop(self) -> None:
        with self._lock:
            if not self._started and self.started_at is None:
                # Igual hacemos un stop defensivo de los componentes.
                pass
            self._started = False
        for label, action in (
            ("supervisor", self.supervisor.stop),
            ("pipeline", self.pipeline.stop if self.pipeline is not None else None),
            ("controller", self.controller.stop),
            ("adapter", self.adapter.disconnect),
        ):
            if action is None:
                continue
            try:
                action()
            except Exception as exc:  # un stop no debe tumbar el proceso
                self._set_last_error(f"error deteniendo {label}: {exc}")
        with self._lock:
            self.started_at = None
        _LOGGER.info("runtime detenido")

    # -- percepción --------------------------------------------------------
    def _on_perception_event(self, event: PerceptionEvent) -> None:
        with self._lock:
            self._events_count += 1
        try:
            self.controller.submit_event(event)
        except Exception as exc:
            self._set_last_error(f"error procesando evento de percepción: {exc}")

    def _trigger_ambient(self) -> None:
        self.controller.trigger_manual("ambiental", allow_disabled=True)

    # -- estado ------------------------------------------------------------
    def status(self) -> dict:
        try:
            controller_status = self.controller.status()
        except Exception as exc:
            controller_status = {
                "robot_state": "unknown",
                "mode": "unknown",
                "paused": False,
                "logs": 0,
                "last_error": str(exc),
            }

        adapter = self.adapter
        try:
            adapter_status = adapter.status()
        except Exception as exc:
            adapter_status = {"error": str(exc)}
        state = getattr(adapter, "state", RobotState.DISCONNECTED)

        if self.pipeline is not None:
            try:
                pipeline_status = self.pipeline.status()
            except Exception as exc:
                pipeline_status = {
                    "camera_ok": False,
                    "running": False,
                    "fps": 0.0,
                    "latency_ms": 0.0,
                    "frames": 0,
                    "last_error": str(exc),
                    "heartbeat_age_s": 0.0,
                    "gestures": {},
                }
        else:
            pipeline_status = {
                "camera_ok": False,
                "running": False,
                "fps": 0.0,
                "latency_ms": 0.0,
                "frames": 0,
                "last_error": None,
                "heartbeat_age_s": 0.0,
                "gestures": {},
            }

        try:
            supervisor_status = self.supervisor.status()
        except Exception as exc:
            supervisor_status = {
                "layers": {},
                "last_error": str(exc),
            }

        perception = {
            "camera_ok": bool(pipeline_status.get("camera_ok", False)),
            "fps": float(pipeline_status.get("fps", 0.0) or 0.0),
            "process_fps": float(pipeline_status.get("process_fps", 0.0) or 0.0),
            "latency_ms": float(pipeline_status.get("latency_ms", 0.0) or 0.0),
            "frames": int(pipeline_status.get("frames", 0) or 0),
            "last_error": pipeline_status.get("last_error"),
            "heartbeat_age_s": float(pipeline_status.get("heartbeat_age_s", 0.0) or 0.0),
            "camera_actual": pipeline_status.get("camera_actual"),
            "model_fallback": pipeline_status.get("model_fallback"),
            "hand_model_fallback": pipeline_status.get("hand_model_fallback"),
            "gestures": dict(pipeline_status.get("gestures", {}) or {}),
        }

        last_error = (
            self._last_error
            or supervisor_status.get("last_error")
            or perception.get("last_error")
            or controller_status.get("last_error")
        )

        uptime = 0.0
        if self.started_at is not None:
            uptime = max(0.0, time.time() - self.started_at)

        with self._lock:
            events_count = self._events_count
            logs = list(self.controller.logs)[-50:]

        return {
            "uptime_s": round(uptime, 2),
            "config_version": self.config.version,
            "mode": controller_status.get("mode", "unknown"),
            "paused": bool(controller_status.get("paused", False)),
            "robot": {
                "adapter": getattr(adapter, "name", "unknown"),
                "state": getattr(state, "value", str(state)),
                "last_error": controller_status.get("last_error"),
                "status": adapter_status,
            },
            "perception": perception,
            "decision": {
                "last_behavior_id": self.decision.last_behavior_id,
                "logs": logs,
            },
            "supervisor": {
                "layers": supervisor_status.get("layers", {}),
                "last_error": supervisor_status.get("last_error"),
            },
            "layers": supervisor_status.get("layers", {}),
            "last_error": last_error,
            "events_count": events_count,
        }

    # -- eventos y exportación --------------------------------------------
    def events(
        self,
        limit: int = 200,
        gesture: str | None = None,
        result: str | None = None,
    ) -> list[dict]:
        wanted_gesture = getattr(gesture, "value", gesture)
        wanted_result = getattr(result, "value", result)
        records = list(self.controller.logs)
        if wanted_gesture is not None:
            records = [r for r in records if r.get("gesture") == wanted_gesture]
        if wanted_result is not None:
            records = [r for r in records if r.get("result") == wanted_result]
        records.reverse()
        if limit is not None and limit >= 0:
            records = records[:limit]
        return [dict(record) for record in records]

    def export_events(self, fmt: str = "json") -> str:
        fmt = (fmt or "json").lower()
        records = self.events(limit=10_000)
        if fmt == "json":
            return json.dumps(records, ensure_ascii=False, default=str)
        if fmt == "csv":
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=_CONTROLLER_LOG_KEYS, extrasaction="ignore")
            writer.writeheader()
            for record in records:
                writer.writerow(
                    {key: _csv_safe(record.get(key)) for key in _CONTROLLER_LOG_KEYS}
                )
            return buffer.getvalue()
        raise ValueError("formato de exportación no soportado: usá 'json' o 'csv'")

    # -- comandos de operador ---------------------------------------------
    def set_mode(self, mode) -> None:
        self.controller.set_mode(mode)

    def set_paused(self, paused: bool) -> None:
        self.controller.set_paused(bool(paused))

    def trigger(self, behavior_id: str) -> dict:
        return self.controller.trigger_manual(behavior_id)

    def home(self) -> dict:
        return self.controller.home()

    # -- configuración -----------------------------------------------------
    def get_config_dict(self) -> dict:
        return self.config.model_dump(mode="json")

    def update_config(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("la configuración debe ser un objeto (dict)")
        with self._lock:
            old_config = self.config.model_copy(deep=True)
            old_adapter = self.adapter
            try:
                candidate = AppConfig.model_validate(payload)
            except ValidationError as exc:
                raise ValueError(self._format_validation_error(exc)) from exc

            errors = validate_behavior_set(self.behavior_store.get_all(), candidate.robot)
            if errors:
                raise ValueError("; ".join(errors))

            try:
                saved = self.config_store.save(candidate)
            except Exception as exc:
                raise ValueError(f"no se pudo guardar la configuración: {exc}") from exc

            self.config = saved
            try:
                self._apply_config(old_config, saved)
            except Exception:
                self._restore_config(old_config, old_adapter)
                raise
            return self.status()

    def rollback_config(self) -> dict:
        with self._lock:
            old_config = self.config.model_copy(deep=True)
            old_adapter = self.adapter
            try:
                restored = self.config_store.rollback()
            except ConfigError as exc:
                raise ValueError(str(exc)) from exc

            errors = validate_behavior_set(self.behavior_store.get_all(), restored.robot)
            if errors:
                raise ValueError("; ".join(errors))

            self.config = restored
            try:
                self._apply_config(old_config, restored)
            except Exception:
                self._restore_config(old_config, old_adapter)
                raise
            return self.status()

    def _apply_config(self, old: AppConfig, new: AppConfig) -> None:
        self.decision.set_config(new.decision)
        self.controller.busy_policy = new.decision.busy_policy
        self.behavior_store.robot = new.robot

        robot_changed = (
            old.robot.adapter != new.robot.adapter
            or old.robot.ip != new.robot.ip
            or old.robot.dashboard_port != new.robot.dashboard_port
            or old.robot.move_port != new.robot.move_port
            or old.robot.feedback_port != new.robot.feedback_port
            or old.robot.connect_timeout_s != new.robot.connect_timeout_s
        )

        old_adapter = self.adapter
        if robot_changed:
            new_adapter = self._build_adapter(new.robot)
            try:
                new_adapter.connect()
            except Exception:
                try:
                    new_adapter.disconnect()
                except Exception:
                    pass
                raise
            self.adapter = new_adapter
            self.controller.adapter = new_adapter
            self.supervisor.adapter = new_adapter
            try:
                old_adapter.disconnect()
            except Exception:
                pass
        else:
            try:
                old_adapter.robot_config = new.robot
            except Exception:
                pass

        if self.pipeline is not None:
            self.pipeline.update_config(new.camera, new.perception)
            old_camera = (old.camera.index, old.camera.width, old.camera.height)
            new_camera = (new.camera.index, new.camera.width, new.camera.height)
            if old_camera != new_camera:
                self.pipeline.restart()

        self.supervisor.config = new.supervisor

    def _restore_config(self, old: AppConfig, old_adapter) -> None:
        self.config = old
        self.decision.set_config(old.decision)
        self.controller.busy_policy = old.decision.busy_policy
        self.behavior_store.robot = old.robot

        if self.adapter is not old_adapter:
            current = self.adapter
            self.adapter = old_adapter
            self.controller.adapter = old_adapter
            self.supervisor.adapter = old_adapter
            try:
                current.disconnect()
            except Exception:
                pass
            try:
                old_adapter.connect()
            except Exception:
                pass

        try:
            old_adapter.robot_config = old.robot
        except Exception:
            pass

        if self.pipeline is not None:
            try:
                self.pipeline.update_config(old.camera, old.perception)
            except Exception:
                pass

        self.supervisor.config = old.supervisor

    @staticmethod
    def _format_validation_error(exc: ValidationError) -> str:
        details = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", ()))
            details.append(f"{location or 'config'}: {error.get('msg', 'inválido')}")
        return "; ".join(details) or "configuración inválida"

    # -- comportamientos ---------------------------------------------------
    def behaviors_dict(self) -> dict:
        return {
            "behaviors": [
                behavior.model_dump(mode="json")
                for behavior in self.behavior_store.get_all()
            ]
        }

    def _parse_behaviors(self, payload: Any) -> tuple[list[Behavior], list[str]]:
        items = payload
        if isinstance(payload, dict):
            if "behaviors" not in payload:
                return [], ["el objeto debe contener la clave 'behaviors'"]
            items = payload["behaviors"]
        if not isinstance(items, list):
            return [], ["se esperaba una lista de comportamientos"]

        behaviors: list[Behavior] = []
        errors: list[str] = []
        for index, item in enumerate(items):
            if isinstance(item, Behavior):
                behaviors.append(item.model_copy(deep=True))
                continue
            try:
                behaviors.append(Behavior.model_validate(item))
            except ValidationError as exc:
                location = f"comportamiento {index}"
                for error in exc.errors():
                    field = ".".join(str(part) for part in error.get("loc", ()))
                    errors.append(f"{location}.{field}: {error.get('msg', 'inválido')}")
        return behaviors, errors

    def validate_behaviors(self, payload: Any) -> list[str]:
        behaviors, errors = self._parse_behaviors(payload)
        if errors:
            return errors
        try:
            return list(self.behavior_store.validate(behaviors))
        except Exception as exc:
            return [f"no se pudieron validar los comportamientos: {exc}"]

    def update_behaviors(self, payload: Any) -> dict:
        behaviors, errors = self._parse_behaviors(payload)
        if errors:
            raise ValueError("; ".join(errors))
        # El cliente no puede declarar un comportamiento como validado: la
        # validación real es contra hardware y no se puede falsificar por API.
        behaviors = [
            behavior.model_copy(update={"validated": False})
            for behavior in behaviors
        ]
        with self._lock:
            try:
                self.behavior_store.save(behaviors)
            except Exception as exc:
                details = getattr(exc, "errors", None) or [str(exc)]
                raise ValueError("; ".join(str(item) for item in details)) from exc
            self.decision.set_catalog(behaviors)
        return self.behaviors_dict()

    def preview_behavior(self, behavior_id: str) -> dict:
        behavior = self.behavior_store.get(behavior_id)
        if behavior is None:
            raise ValueError(f"comportamiento inexistente: {behavior_id}")

        home = [float(value) for value in self.config.robot.home_joints]
        steps: list[dict] = []
        cursor_ms = 0
        for index, step in enumerate(behavior.steps):
            deltas = {joint: float(step.deltas.get(joint, 0.0)) for joint in JOINT_NAMES}
            targets = {
                joint: home[joint_index] + deltas[joint]
                for joint_index, joint in enumerate(JOINT_NAMES)
            }
            steps.append(
                {
                    "index": index,
                    "t_start_ms": cursor_ms,
                    "duration_ms": int(step.duration_ms),
                    "pause_ms": int(step.pause_ms),
                    "target_joints": targets,
                    "deltas": deltas,
                }
            )
            cursor_ms += int(step.duration_ms) + int(step.pause_ms)

        result = {
            "behavior_id": behavior.id,
            "validated": bool(behavior.validated),
            "speed_pct": behavior.speed_pct,
            "acceleration_pct": behavior.acceleration_pct,
            "total_ms": cursor_ms,
            "steps": steps,
            "final_joints": home,
        }
        if not behavior.validated:
            result["warnings"] = ["SIN VALIDAR"]
        return result

    def test_behavior_mock(self, behavior_id: str) -> dict:
        behavior = self.behavior_store.get(behavior_id)
        if behavior is None:
            raise ValueError(f"comportamiento inexistente: {behavior_id}")

        preview = self.preview_behavior(behavior_id)
        adapter = MockAdapter(self.config.robot, time_scale=0.05)
        try:
            adapter.connect()
            result = adapter.execute_behavior(behavior, uuid.uuid4().hex)
        finally:
            try:
                adapter.disconnect()
            except Exception:
                pass
        return {
            "ok": bool(result.ok),
            "result": result.model_dump(mode="json"),
            "preview": preview,
        }

    # -- diagnóstico -------------------------------------------------------
    def troubleshooting(self) -> dict:
        from .troubleshooting import Troubleshooting

        runner = Troubleshooting(self)
        results = runner.run_all()
        return {"results": results, "report": runner.to_text(results)}

    # -- replay y grabación ------------------------------------------------
    @staticmethod
    def _validate_replay_path(path: str | Path) -> Path:
        """Confina el replay a videos regulares dentro de ``recordings/``."""

        allowed_suffixes = {".mp4", ".avi", ".mov", ".mkv", ".webm"}
        recordings_root = (Path("recordings")).expanduser().resolve()
        source = Path(path).expanduser().resolve()

        if not source.is_relative_to(recordings_root):
            raise ValueError(
                f"el video debe estar dentro de {recordings_root}; ruta rechazada: {source}"
            )
        if not source.is_file():
            raise ValueError(
                f"no existe un archivo de video regular en la ruta: {source}"
            )
        if source.suffix.lower() not in allowed_suffixes:
            allowed = ", ".join(sorted(allowed_suffixes))
            raise ValueError(
                f"extensión de video no permitida: {source.suffix or '(sin extensión)'}. "
                f"Usá una de: {allowed}."
            )
        return source

    def replay_start(self, path: str | Path) -> dict:
        from .perception.engine import MediaPipeEngine
        from .perception.tracker import GestureTracker

        source = self._validate_replay_path(path)
        state = {
            "running": False,
            "frames": 0,
            "events": [],
            "error": None,
            "started_at": time.time(),
        }
        with self._lock:
            if self.replay is not None and bool(self.replay.get("running")):
                raise ValueError("ya hay una reproducción en curso")
            self.replay = state

        try:
            engine = MediaPipeEngine(self.config.camera, self.config.perception)
            tracker = GestureTracker(self.config.perception)
        except Exception as exc:
            state["error"] = f"MediaPipe no disponible para replay: {exc}"
            return self.replay_status()

        replay = VideoReplay(
            source,
            engine,
            tracker,
            source="replay",
            on_event=self._on_replay_event,
        )

        def run_replay() -> None:
            try:
                summary = replay.run()
                with self._lock:
                    state["frames"] = int(summary.get("frames", 0) or 0)
                    state["error"] = summary.get("error")
            except Exception as exc:
                with self._lock:
                    state["error"] = f"error en replay: {exc}"
            finally:
                try:
                    engine.close()
                except Exception:
                    pass
                with self._lock:
                    state["running"] = False

        state["running"] = True
        threading.Thread(
            target=run_replay,
            name="exp-brazo-replay",
            daemon=True,
        ).start()
        return self.replay_status()

    def _on_replay_event(self, event: PerceptionEvent) -> None:
        with self._lock:
            if self.replay is not None:
                events = self.replay["events"]
                events.append(event.model_dump(mode="json"))
                if len(events) > 5000:
                    del events[: len(events) - 5000]

    def replay_status(self) -> dict:
        with self._lock:
            if self.replay is None:
                return {
                    "running": False,
                    "frames": 0,
                    "events": [],
                    "error": None,
                    "started_at": None,
                }
            state = self.replay
            return {
                "running": bool(state.get("running", False)),
                "frames": int(state.get("frames", 0) or 0),
                "events": list(state.get("events", [])),
                "error": state.get("error"),
                "started_at": state.get("started_at"),
            }

    def record_start(self, seconds: float = 10) -> dict:
        previous = None
        with self._lock:
            if self.recorder is not None:
                try:
                    running = bool(self.recorder.status().get("running"))
                except Exception:
                    running = True
                if running:
                    raise ValueError("ya hay una grabación en curso")
                # Recorder inactivo: se cierra antes de reemplazar la referencia.
                previous = self.recorder
                self.recorder = None
        if previous is not None:
            try:
                previous.stop()
            except Exception:
                pass

        directory = Path("recordings")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = directory / f"replay_{stamp}.mp4"
        if path.exists():
            path = directory / f"replay_{stamp}_{int(time.time() * 1000)}.mp4"

        recorder = CameraRecorder()
        started = recorder.start(path, self.config.camera, float(seconds))
        if path.exists():
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        with self._lock:
            self.recorder = recorder
        status = recorder.status()
        status["ok"] = bool(started)
        status["path"] = str(path)
        if not started:
            self._set_last_error(f"no se pudo grabar: {status.get('error')}")
        return status

    def record_stop(self) -> dict:
        with self._lock:
            recorder = self.recorder
            self.recorder = None
        if recorder is None:
            return {"ok": False, "error": "no hay grabación en curso"}
        recorder.stop()
        return recorder.status()

    # -- snapshots ---------------------------------------------------------
    def snapshot_jpeg(self) -> bytes | None:
        if self.pipeline is None:
            return None
        try:
            return self.pipeline.get_jpeg()
        except Exception:
            return None

    def snapshot_frame(self):
        if self.pipeline is None:
            return None
        try:
            return self.pipeline.snapshot()
        except Exception:
            return None

    def overlay_metrics(self) -> dict:
        if self.pipeline is not None:
            try:
                status = self.pipeline.status()
            except Exception:
                status = {}
        else:
            status = {}
        try:
            controller_status = self.controller.status()
        except Exception:
            controller_status = {}
        return {
            "camera_ok": bool(status.get("camera_ok", False)),
            "fps": float(status.get("fps", 0.0) or 0.0),
            "latency_ms": float(status.get("latency_ms", 0.0) or 0.0),
            "gestures": dict(status.get("gestures", {}) or {}),
            "mode": controller_status.get("mode", "unknown"),
            "paused": bool(controller_status.get("paused", False)),
        }

    # -- utilidades internas ----------------------------------------------
    def _set_last_error(self, message: str) -> None:
        with self._lock:
            self._last_error = message
        _LOGGER.warning("%s", message)
