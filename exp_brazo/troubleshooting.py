"""Diagnóstico guiado por capas para el operador.

Cada chequeo se ejecuta de forma independiente, con su propio ``try`` y un
timeout efectivo. El reporte resultante es copiable y está pensado para que
una persona sin conocimientos técnicos sepa qué revisar.
"""

from __future__ import annotations

import concurrent.futures
import glob
import ipaddress
import shutil
import socket
import subprocess
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from .contracts import RobotState

if TYPE_CHECKING:  # pragma: no cover - solo para type checking
    from .runtime import Runtime


def _result(
    check_id: str,
    name: str,
    *,
    ok: bool | None,
    detail: str,
    probable_cause: str = "",
    suggested_action: str = "",
    duration_ms: float = 0.0,
) -> dict:
    return {
        "id": check_id,
        "name": name,
        "ok": ok,
        "detail": detail,
        "probable_cause": probable_cause,
        "suggested_action": suggested_action,
        "duration_ms": round(float(duration_ms), 2),
    }


class Troubleshooting:
    """Ejecuta chequeos de cámara, red, robot, MediaPipe y configuración."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    # -- API pública -------------------------------------------------------
    _BLOCKING_CHECKS = frozenset(
        {"camera", "light_framing", "controller_ping", "controller_ports"}
    )
    _MAX_WORKERS = 4
    _TOTAL_TIMEOUT_S = 4.0

    def run_all(self) -> list[dict]:
        """Ejecuta todos los chequeos en paralelo y devuelve una lista ordenada."""

        checks: list[tuple[str, str, Callable[[], dict]]] = [
            ("camera", "Cámara", self._check_camera),
            ("light_framing", "Luz y encuadre", self._check_light_framing),
            ("controller_ping", "Ping al controlador", self._check_controller_ping),
            ("controller_ports", "Puertos del controlador", self._check_controller_ports),
            ("robot_state", "Estado del robot", self._check_robot_state),
            ("mediapipe_models", "MediaPipe y modelos", self._check_mediapipe),
            ("performance", "Rendimiento de percepción", self._check_performance),
            ("config", "Configuración y comportamientos", self._check_config),
        ]

        # Los chequeos rápidos se encolan primero para no quedar detrás de los
        # que pueden bloquear; el orden de salida se conserva por ``checks``.
        submission_order = sorted(checks, key=lambda item: item[0] in self._BLOCKING_CHECKS)
        started = time.perf_counter()
        deadline = started + self._TOTAL_TIMEOUT_S
        results: dict[str, dict] = {}
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self._MAX_WORKERS,
            thread_name_prefix="diagnostico",
        )
        futures = {
            check_id: executor.submit(self._run_check, func)
            for check_id, _name, func in submission_order
        }
        try:
            for check_id, name, _func in checks:
                future = futures[check_id]
                remaining = deadline - time.perf_counter()
                if future.done():
                    try:
                        payload, duration_ms = future.result(timeout=0)
                    except Exception as exc:
                        payload, duration_ms = self._error_payload(exc), self._elapsed_ms(started)
                elif remaining <= 0:
                    payload, duration_ms = self._timeout_payload(), self._elapsed_ms(started)
                else:
                    try:
                        payload, duration_ms = future.result(timeout=remaining)
                    except concurrent.futures.TimeoutError:
                        payload, duration_ms = self._timeout_payload(), self._elapsed_ms(started)
                    except Exception as exc:
                        payload, duration_ms = self._error_payload(exc), self._elapsed_ms(started)
                payload.pop("duration_ms", None)
                payload.setdefault("probable_cause", "")
                payload.setdefault("suggested_action", "")
                results[check_id] = _result(
                    check_id, name, duration_ms=duration_ms, **payload
                )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return [results[check_id] for check_id, _name, _func in checks]

    def to_text(self, results: list[dict] | None = None) -> str:
        """Devuelve un reporte de texto copiable."""

        items = self.run_all() if results is None else results
        lines = [
            "REPORTE DE DIAGNÓSTICO — Exp-brazo",
            "=" * 48,
        ]
        for item in items:
            ok = item.get("ok")
            label = "OK" if ok is True else "FALLA" if ok is False else "SALTADO"
            lines.append(
                f"[{label}] {item.get('id')} — {item.get('name')}: {item.get('detail')}"
            )
            cause = item.get("probable_cause")
            action = item.get("suggested_action")
            if cause:
                lines.append(f"    Causa probable: {cause}")
            if action:
                lines.append(f"    Acción sugerida: {action}")
            lines.append(f"    Duración: {item.get('duration_ms', 0)} ms")
        lines.append("=" * 48)
        return "\n".join(lines)

    # -- ejecución con timeout --------------------------------------------
    def _run_check(self, func: Callable[[], dict]) -> tuple[dict, float]:
        """Ejecuta un chequeo y devuelve su payload junto a la duración."""

        started = time.perf_counter()
        try:
            payload = func()
        except Exception as exc:
            payload = self._error_payload(exc)
        if not isinstance(payload, dict):
            payload = self._error_payload(RuntimeError("resultado de chequeo inválido"))
        return payload, self._elapsed_ms(started)

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return (time.perf_counter() - started) * 1000.0

    @staticmethod
    def _timeout_payload() -> dict:
        return {
            "ok": False,
            "detail": "timeout",
            "probable_cause": "el dispositivo o servicio no responde",
            "suggested_action": "revisá conexiones y volvé a intentar",
        }

    @staticmethod
    def _error_payload(exc: Exception) -> dict:
        return {
            "ok": False,
            "detail": f"error inesperado: {exc}",
            "probable_cause": "falla interna del chequeo",
            "suggested_action": "revisá los logs y volvé a ejecutar el diagnóstico",
        }

    # -- chequeos ----------------------------------------------------------
    def _check_camera(self) -> dict:
        camera_cfg = self.runtime.config.camera
        pipeline = getattr(self.runtime, "pipeline", None)
        pipeline_camera_ok = False
        if pipeline is not None:
            try:
                status = pipeline.status() or {}
            except Exception:
                status = {}
            pipeline_camera_ok = bool(status.get("camera_ok"))
            if pipeline_camera_ok and self._has_pipeline_snapshot(pipeline):
                return self._camera_in_use_payload(camera_cfg.index, recent=True)

        try:
            import cv2
        except Exception:
            if pipeline_camera_ok:
                return self._camera_in_use_payload(camera_cfg.index, recent=False)
            return {
                "ok": False,
                "detail": "OpenCV no está instalado",
                "probable_cause": "dependencias incompletas",
                "suggested_action": "ejecutá ./install.sh",
            }

        devices = sorted(glob.glob("/dev/video*"))
        capture = None
        try:
            capture = self._open_capture_short(cv2)
            opened = True
            if hasattr(capture, "isOpened"):
                opened = bool(capture.isOpened())
            if not opened:
                if pipeline_camera_ok:
                    return self._camera_in_use_payload(camera_cfg.index, recent=False)
                if not devices:
                    return {
                        "ok": False,
                        "detail": "no se encontró ningún dispositivo de video",
                        "probable_cause": "cámara desconectada o índice incorrecto",
                        "suggested_action": (
                            "conectá la cámara y verificá camera.index en la configuración"
                        ),
                    }
                return {
                    "ok": False,
                    "detail": f"no se pudo abrir la cámara índice {camera_cfg.index}",
                    "probable_cause": "cámara ocupada por otra aplicación o sin permisos",
                    "suggested_action": (
                        "cerrá otras apps que usen la cámara y revisá los permisos del usuario"
                    ),
                }
            ok, frame = capture.read()
            if not ok or frame is None:
                if pipeline_camera_ok:
                    return self._camera_in_use_payload(camera_cfg.index, recent=False)
                return {
                    "ok": False,
                    "detail": "la cámara abrió pero no entrega imágenes",
                    "probable_cause": "cámara ocupada, tapada o con falla de driver",
                    "suggested_action": (
                        "cerrá otras apps, revisá el cable y probá otro índice de cámara"
                    ),
                }
            return {
                "ok": True,
                "detail": f"cámara índice {camera_cfg.index} entregó un frame",
                "probable_cause": "",
                "suggested_action": "",
            }
        except PermissionError as exc:
            if pipeline_camera_ok:
                return self._camera_in_use_payload(camera_cfg.index, recent=False)
            return {
                "ok": False,
                "detail": f"sin permisos para usar la cámara: {exc}",
                "probable_cause": "el usuario no pertenece al grupo de video",
                "suggested_action": "agregá el usuario al grupo video y reiniciá la sesión",
            }
        except Exception as exc:
            if pipeline_camera_ok:
                return self._camera_in_use_payload(camera_cfg.index, recent=False)
            return {
                "ok": False,
                "detail": f"error abriendo la cámara: {exc}",
                "probable_cause": "índice equivocado, cámara ocupada o driver con problemas",
                "suggested_action": "revisá camera.index y cerrá otras aplicaciones de cámara",
            }
        finally:
            if capture is not None:
                try:
                    capture.release()
                except Exception:
                    pass

    @staticmethod
    def _camera_in_use_payload(index: int, *, recent: bool) -> dict:
        suffix = "frames recientes" if recent else "sin snapshot reciente"
        return {
            "ok": True,
            "detail": f"cámara en uso por el sistema (índice {index}), {suffix}",
            "probable_cause": "",
            "suggested_action": "",
        }

    @staticmethod
    def _pipeline_frame(pipeline):
        if pipeline is None:
            return None
        for name in ("snapshot_frame", "snapshot"):
            method = getattr(pipeline, name, None)
            if not callable(method):
                continue
            try:
                frame = method()
            except Exception:
                continue
            if frame is not None:
                return frame
        return None

    def _has_pipeline_snapshot(self, pipeline) -> bool:
        if self._pipeline_frame(pipeline) is not None:
            return True
        for name in ("snapshot_jpeg", "get_jpeg"):
            method = getattr(pipeline, name, None)
            if not callable(method):
                continue
            try:
                if method() is not None:
                    return True
            except Exception:
                continue
        return False

    def _open_capture_short(self, cv2_module):
        """Abre ``VideoCapture`` con timeout corto cuando OpenCV lo soporta."""

        index = self.runtime.config.camera.index
        open_timeout = getattr(cv2_module, "CAP_PROP_OPEN_TIMEOUT_MSEC", None)
        if open_timeout is not None:
            try:
                return cv2_module.VideoCapture(
                    index,
                    cv2_module.CAP_ANY,
                    [open_timeout, 1000],
                )
            except Exception:
                pass

        capture = cv2_module.VideoCapture(index)
        read_timeout = getattr(cv2_module, "CAP_PROP_READ_TIMEOUT_MSEC", None)
        if read_timeout is not None:
            try:
                capture.set(read_timeout, 1000)
            except Exception:
                pass
        return capture

    def _grab_frame(self):
        pipeline = getattr(self.runtime, "pipeline", None)
        frame = self._pipeline_frame(pipeline)
        if frame is not None:
            return frame

        snapshot_frame = getattr(self.runtime, "snapshot_frame", None)
        if callable(snapshot_frame):
            try:
                frame = snapshot_frame()
            except Exception:
                frame = None
            if frame is not None:
                return frame

        try:
            import cv2
        except Exception:
            return None
        capture = None
        try:
            capture = self._open_capture_short(cv2)
            ok, frame = capture.read()
            return frame if ok and frame is not None else None
        except Exception:
            return None
        finally:
            if capture is not None:
                try:
                    capture.release()
                except Exception:
                    pass

    def _check_light_framing(self) -> dict:
        try:
            import cv2
        except Exception:
            return {
                "ok": None,
                "detail": "OpenCV no está instalado; no se puede medir brillo",
                "probable_cause": "",
                "suggested_action": "ejecutá ./install.sh para habilitar el análisis de imagen",
            }

        frame = self._grab_frame()
        if frame is None:
            return {
                "ok": False,
                "detail": "no hay imagen disponible",
                "probable_cause": "cámara sin frames, persona fuera de cuadro o lente tapada",
                "suggested_action": "ubicá a la persona frente a la cámara y revisá la iluminación",
            }

        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            brightness = float(gray.mean())
            shape = getattr(frame, "shape", (0, 0))
            height = int(shape[0]) if len(shape) > 0 else 0
            width = int(shape[1]) if len(shape) > 1 else 0
        except Exception as exc:
            return {
                "ok": None,
                "detail": f"no se pudo analizar el frame: {exc}",
                "probable_cause": "",
                "suggested_action": "",
            }

        if brightness < 40:
            return {
                "ok": False,
                "detail": f"brillo medio {brightness:.1f} ({width}x{height})",
                "probable_cause": "poca luz o lente tapada",
                "suggested_action": "mejorá la iluminación frontal y limpiá la lente",
            }
        return {
            "ok": True,
            "detail": f"brillo medio {brightness:.1f} ({width}x{height})",
            "probable_cause": "",
            "suggested_action": "",
        }

    def _check_controller_ping(self) -> dict:
        robot = self.runtime.config.robot
        try:
            ip = str(ipaddress.ip_address(robot.ip))
        except (TypeError, ValueError):
            return {
                "ok": False,
                "detail": f"IP del controlador inválida: {robot.ip!r}",
                "probable_cause": "la configuración robot.ip no es una IP literal",
                "suggested_action": (
                    "corregí robot.ip con una dirección IPv4 o IPv6 (por ejemplo 192.168.5.1)"
                ),
            }

        ping = shutil.which("ping")
        if ping:
            try:
                completed = subprocess.run(
                    ["ping", "-c", "1", "-W", "1", "--", ip],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                    shell=False,
                )
                if completed.returncode != 0:
                    # Algunos ping no aceptan "--"; se reintenta sin el separador
                    # únicamente porque ``ip`` ya fue validada como literal.
                    completed = subprocess.run(
                        ["ping", "-c", "1", "-W", "1", ip],
                        capture_output=True,
                        text=True,
                        timeout=3,
                        check=False,
                        shell=False,
                    )
                if completed.returncode == 0:
                    return {
                        "ok": True,
                        "detail": f"{ip} responde a ping",
                        "probable_cause": "",
                        "suggested_action": "",
                    }
                return {
                    "ok": False,
                    "detail": f"{ip} no responde a ping",
                    "probable_cause": "cable desconectado, IP distinta o subred incorrecta",
                    "suggested_action": (
                        "revisá el cable, la IP fija del Dobot y la subred de la computadora"
                    ),
                }
            except subprocess.TimeoutExpired:
                return {
                    "ok": False,
                    "detail": f"ping a {ip} agotó el tiempo",
                    "probable_cause": "equipo apagado, cable desconectado o subred incorrecta",
                    "suggested_action": "verificá el cable, la IP fija y la subred",
                }
            except Exception as exc:
                return {
                    "ok": False,
                    "detail": f"ping no disponible o falló: {exc}",
                    "probable_cause": "herramienta ping ausente en el sistema",
                    "suggested_action": "revisá la conexión o instalá iputils-ping",
                }

        # Sin ping: al menos comprobar que el puerto del dashboard abra.
        try:
            with socket.create_connection((ip, robot.dashboard_port), timeout=1.5):
                return {
                    "ok": True,
                    "detail": f"{ip}:{robot.dashboard_port} acepta conexiones (ping ausente)",
                    "probable_cause": "",
                    "suggested_action": "",
                }
        except Exception as exc:
            return {
                "ok": False,
                "detail": f"sin ping y sin conexión TCP a {ip}:{robot.dashboard_port}: {exc}",
                "probable_cause": "cable desconectado, IP distinta o subred incorrecta",
                "suggested_action": (
                    "revisá el cable, la IP fija del Dobot y la subred de la computadora"
                ),
            }

    def _check_controller_ports(self) -> dict:
        robot = self.runtime.config.robot
        try:
            ip = str(ipaddress.ip_address(robot.ip))
        except (TypeError, ValueError):
            return {
                "ok": False,
                "detail": f"IP del controlador inválida: {robot.ip!r}",
                "probable_cause": "la configuración robot.ip no es una IP literal",
                "suggested_action": (
                    "corregí robot.ip con una dirección IPv4 o IPv6 (por ejemplo 192.168.5.1)"
                ),
            }

        raw_ports = {
            "dashboard": robot.dashboard_port,
            "movimiento": robot.move_port,
            "feedback": robot.feedback_port,
        }
        ports: dict[str, int] = {}
        for label, raw_port in raw_ports.items():
            try:
                port = int(raw_port)
            except (TypeError, ValueError):
                port = 0
            if not 1 <= port <= 65535:
                return {
                    "ok": False,
                    "detail": f"puerto inválido para {label}: {raw_port!r}",
                    "probable_cause": "la configuración de puertos del robot está fuera de rango",
                    "suggested_action": "usá puertos entre 1 y 65535 para el Dobot",
                }
            ports[label] = port

        def probe(item: tuple[str, int]) -> tuple[str, int, bool]:
            label, port = item
            try:
                with socket.create_connection((ip, port), timeout=0.6):
                    return label, port, True
            except Exception:
                return label, port, False

        outcomes: list[tuple[str, int, bool]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            outcomes = list(executor.map(probe, ports.items()))

        opened: list[str] = []
        closed: list[str] = []
        for label, port, is_open in outcomes:
            if is_open:
                opened.append(f"{label}:{port}")
            else:
                closed.append(f"{label}:{port}")

        if not closed:
            return {
                "ok": True,
                "detail": f"abren los 3 puertos: {', '.join(opened)}",
                "probable_cause": "",
                "suggested_action": "",
            }
        return {
            "ok": False,
            "detail": f"abren: {', '.join(opened) or 'ninguno'}; cerrados: {', '.join(closed)}",
            "probable_cause": "modo TCP/IP desactivado, firewall o robot apagado",
            "suggested_action": (
                "activá el modo TCP/IP remoto en DobotStudio Pro y revisá el firewall"
            ),
        }

    def _check_robot_state(self) -> dict:
        adapter = self.runtime.adapter
        name = str(getattr(adapter, "name", "") or "")
        state = getattr(adapter, "state", RobotState.DISCONNECTED)

        if name == "mock":
            return {
                "ok": None,
                "detail": "adaptador mock activo; no hay robot físico",
                "probable_cause": "",
                "suggested_action": "para probar el robot real, cambiá robot.adapter a dobot",
            }

        if name == "dobot":
            return self._check_dobot_robot_state(adapter)

        if state not in (RobotState.IDLE, RobotState.BUSY):
            return {
                "ok": False,
                "detail": f"adaptador en estado {getattr(state, 'value', state)}",
                "probable_cause": "robot desconectado o en falla",
                "suggested_action": (
                    "revisá el modo TCP/IP remoto en DobotStudio Pro y la conexión de red"
                ),
            }

        parts: list[str] = [f"estado {getattr(state, 'value', state)}"]
        try:
            status = adapter.status()
            if isinstance(status, dict):
                parts.append(f"status={status}")
        except Exception as exc:
            parts.append(f"status falló: {exc}")

        for method_name in ("GetErrorID", "RobotMode", "GetAngle"):
            method = getattr(adapter, method_name, None)
            if not callable(method):
                continue
            try:
                parts.append(f"{method_name}={method()}")
            except Exception as exc:
                parts.append(f"{method_name} falló: {exc}")

        return {
            "ok": True,
            "detail": "; ".join(parts),
            "probable_cause": "",
            "suggested_action": "",
        }

    def _check_dobot_robot_state(self, adapter) -> dict:
        """Consulta el adaptador Dobot real con timeouts cortos (status())."""

        try:
            status = adapter.status()
        except Exception as exc:
            return {
                "ok": False,
                "detail": f"status del adaptador Dobot falló: {exc}",
                "probable_cause": "conexión TCP/IP inestable o adaptador sin inicializar",
                "suggested_action": (
                    "revisá el modo TCP/IP remoto en DobotStudio Pro, la IP y los puertos"
                ),
            }

        if not isinstance(status, dict):
            status = {}

        state_value = str(status.get("state", "desconocido"))
        parts = [f"estado {state_value}"]
        if status.get("last_error"):
            parts.append(f"último error: {status['last_error']}")
        if status.get("feedback_ok") is False:
            parts.append("feedback 30004 sin datos")
        if status.get("robot_mode") is not None:
            parts.append(f"robot_mode={status['robot_mode']}")
        if status.get("error_id"):
            parts.append(f"error_id={status['error_id']}")
        if status.get("angles"):
            parts.append(f"angles={status['angles']}")
        detail = "; ".join(parts)

        if state_value not in (RobotState.IDLE.value, RobotState.BUSY.value):
            return {
                "ok": False,
                "detail": detail,
                "probable_cause": "robot desconectado, en falla o no habilitado",
                "suggested_action": (
                    "revisá el modo TCP/IP remoto en DobotStudio Pro, la conexión de red "
                    "y que el robot esté habilitado"
                ),
            }

        return {
            "ok": True,
            "detail": detail,
            "probable_cause": "",
            "suggested_action": "",
        }

    def _check_mediapipe(self) -> dict:
        from .perception.engine import REQUIRED_MODELS, mediapipe_available, models_dir

        try:
            import cv2  # noqa: F401
        except Exception as exc:
            cv2_error: str | None = str(exc)
        else:
            cv2_error = None

        tasks_available = mediapipe_available()

        directory = models_dir()
        found: list[str] = []
        missing_files: list[str] = []
        for name in REQUIRED_MODELS:
            path = directory / name
            if path.is_file():
                found.append(f"{name} ({path.stat().st_size} bytes)")
            else:
                missing_files.append(str(path))
        files_detail = f"modelos encontrados: {', '.join(found) or 'ninguno'}"
        if missing_files:
            files_detail += f"; faltantes: {', '.join(missing_files)}"
        files_detail += "; modelos vendorizados; no se descargan en runtime"

        fallbacks: list[str] = []
        pipeline = getattr(self.runtime, "pipeline", None)
        engine = getattr(pipeline, "_engine", None)
        for attr in ("model_fallback", "hand_model_fallback"):
            reason = getattr(engine, attr, None)
            if reason:
                fallbacks.append(str(reason))
        if pipeline is not None:
            try:
                status = pipeline.status() or {}
            except Exception:
                status = {}
            for key in ("model_fallback", "hand_model_fallback"):
                reason = status.get(key)
                if reason:
                    fallbacks.append(str(reason))
        fallbacks = list(dict.fromkeys(fallbacks))
        fallback_detail = ""
        if fallbacks:
            fallback_detail = f"; fallback del engine: {', '.join(fallbacks)}"

        if cv2_error is not None:
            return {
                "ok": False,
                "detail": f"OpenCV no está disponible: {cv2_error}; {files_detail}",
                "probable_cause": "dependencias de percepción incompletas",
                "suggested_action": "ejecutá ./install.sh",
            }

        if tasks_available and not missing_files:
            return {
                "ok": True,
                "detail": (
                    "mediapipe.tasks (Pose + Hands) disponible; "
                    f"{files_detail}{fallback_detail}"
                ),
                "probable_cause": "",
                "suggested_action": "",
            }

        if tasks_available and missing_files:
            return {
                "ok": False,
                "detail": (
                    f"faltan archivos de modelo vendorizados: {', '.join(missing_files)}; "
                    f"ejecutá ./install.sh --fetch-models{fallback_detail}"
                ),
                "probable_cause": "modelos .task ausentes en assets/models/",
                "suggested_action": "ejecutá ./install.sh --fetch-models (requiere internet)",
            }

        return {
            "ok": False,
            "detail": (
                f"mediapipe.tasks no disponible; {files_detail}{fallback_detail}"
            ),
            "probable_cause": "versión de MediaPipe sin Tasks API o instalación incompleta",
            "suggested_action": "ejecutá ./install.sh para reinstalar la versión fijada",
        }

    def _check_performance(self) -> dict:
        pipeline = getattr(self.runtime, "pipeline", None)
        if pipeline is None:
            return {
                "ok": None,
                "detail": "percepción deshabilitada; no hay métricas de rendimiento",
                "probable_cause": "",
                "suggested_action": "",
            }
        try:
            status = pipeline.status() or {}
        except Exception as exc:
            return {
                "ok": False,
                "detail": f"no se pudo leer el estado del pipeline: {exc}",
                "probable_cause": "pipeline de cámara con problemas",
                "suggested_action": "revisá los logs y reiniciá el sistema",
            }

        frames = int(status.get("frames", 0) or 0)
        fps = float(status.get("fps", 0.0) or 0.0)
        latency = float(status.get("latency_ms", 0.0) or 0.0)
        if frames <= 0:
            return {
                "ok": None,
                "detail": "todavía no hay frames procesados",
                "probable_cause": "",
                "suggested_action": "",
            }
        if fps < 10 or latency > 500:
            return {
                "ok": False,
                "detail": f"fps={fps:.1f}, latencia={latency:.1f}ms",
                "probable_cause": "equipo lento, resolución alta o apps consumiendo CPU",
                "suggested_action": (
                    "bajá la resolución de cámara, cerrá aplicaciones pesadas "
                    "y revisá el rendimiento del equipo"
                ),
            }
        return {
            "ok": True,
            "detail": f"fps={fps:.1f}, latencia={latency:.1f}ms",
            "probable_cause": "",
            "suggested_action": "",
        }

    def _check_config(self) -> dict:
        try:
            config = self.runtime.config_store.get()
        except Exception as exc:
            return {
                "ok": False,
                "detail": f"la configuración no carga: {exc}",
                "probable_cause": "YAML inválido o archivo faltante",
                "suggested_action": "revisá config/config.yaml o ejecutá ./start.sh --check",
            }

        try:
            behaviors = self.runtime.behavior_store.get_all()
        except Exception as exc:
            return {
                "ok": False,
                "detail": f"el catálogo no carga: {exc}",
                "probable_cause": "behaviors.yaml inválido",
                "suggested_action": "revisá config/behaviors.yaml y los límites del robot",
            }
        if not behaviors:
            return {
                "ok": False,
                "detail": "el catálogo de comportamientos está vacío",
                "probable_cause": "archivo de comportamientos sin entradas",
                "suggested_action": (
                    "agregá al menos un comportamiento o restaurá el archivo semilla"
                ),
            }

        try:
            errors = self.runtime.behavior_store.validate(behaviors)
        except Exception as exc:
            return {
                "ok": False,
                "detail": f"no se pudieron validar los comportamientos: {exc}",
                "probable_cause": "configuración inconsistente",
                "suggested_action": "revisá límites, velocidades y deltas",
            }
        if errors:
            return {
                "ok": False,
                "detail": "; ".join(errors),
                "probable_cause": "comportamientos fuera de los límites configurados",
                "suggested_action": "ajustá los deltas o los límites del robot",
            }

        return {
            "ok": True,
            "detail": (
                f"configuración v{config.version} válida, "
                f"{len(behaviors)} comportamientos cargados"
            ),
            "probable_cause": "",
            "suggested_action": "",
        }
