"""Adaptador real para la familia Dobot CR/E6/Nova (SDK TCP-IP Python V4).

Este módulo usa exclusivamente los comandos y el layout de feedback
documentados en ``Dobot-Arm/TCP-IP-Python-V4`` (licencia MIT):

- Dashboard TCP ``29999``: comandos y también ``MovJ``.
- Feedback TCP ``30004``: paquete binario de 1440 bytes, ~8 ms.

El puerto ``30003`` existe en documentación de la API V3, pero el SDK V4
Python manda los movimientos por el dashboard. ``move_port`` queda solo
como dato de diagnóstico y no se usa para mover.

Los detalles de finalización de movimiento y el formato exacto de algunas
respuestas dependen del firmware del robot: están marcados ``SIN VALIDAR``.
"""

from __future__ import annotations

import math
import re
import socket
import threading
import time

import numpy as np

from ..contracts import JOINT_NAMES, Behavior, ExecutionResult, RobotConfig, RobotState
from .base import RobotAdapter

# ---------------------------------------------------------------------------
# Layout binario del puerto de feedback (30004).
#
# Copiado tal cual de Dobot-Arm/TCP-IP-Python-V4 (MIT License), archivo
# ``dobot_api.py``, variable ``MyType``. No modificar el layout: el paquete
# que publica el controlador mide exactamente 1440 bytes.
# ---------------------------------------------------------------------------
MyType = np.dtype(
    [
        ("len", np.uint16),
        ("reserve", np.byte, (6,)),
        ("DigitalInputs", np.uint64),
        ("DigitalOutputs", np.uint64),
        ("RobotMode", np.uint64),
        ("TimeStamp", np.uint64),
        ("RunTime", np.uint64),
        ("TestValue", np.uint64),
        ("reserve2", np.byte, (8,)),
        ("SpeedScaling", np.float64),
        ("reserve3", np.byte, (16,)),
        ("VRobot", np.float64),
        ("IRobot", np.float64),
        ("ProgramState", np.float64),
        ("SafetyOIn", np.uint16),
        ("SafetyOOut", np.uint16),
        ("reserve4", np.byte, (76,)),
        ("QTarget", np.float64, (6,)),
        ("QDTarget", np.float64, (6,)),
        ("QDDTarget", np.float64, (6,)),
        ("ITarget", np.float64, (6,)),
        ("MTarget", np.float64, (6,)),
        ("QActual", np.float64, (6,)),
        ("QDActual", np.float64, (6,)),
        ("IActual", np.float64, (6,)),
        ("ActualTCPForce", np.float64, (6,)),
        ("ToolVectorActual", np.float64, (6,)),
        ("TCPSpeedActual", np.float64, (6,)),
        ("TCPForce", np.float64, (6,)),
        ("ToolVectorTarget", np.float64, (6,)),
        ("TCPSpeedTarget", np.float64, (6,)),
        ("MotorTemperatures", np.float64, (6,)),
        ("JointModes", np.float64, (6,)),
        ("VActual", np.float64, (6,)),
        ("HandType", np.byte, (4,)),
        ("User", np.byte),
        ("Tool", np.byte),
        ("RunQueuedCmd", np.byte),
        ("PauseCmdFlag", np.byte),
        ("VelocityRatio", np.byte),
        ("AccelerationRatio", np.byte),
        ("reserve5", np.byte),
        ("XYZVelocityRatio", np.byte),
        ("RVelocityRatio", np.byte),
        ("XYZAccelerationRatio", np.byte),
        ("RAccelerationRatio", np.byte),
        ("reserve6", np.byte, (2,)),
        ("BrakeStatus", np.byte),
        ("EnableStatus", np.byte),
        ("DragStatus", np.byte),
        ("RunningStatus", np.byte),
        ("ErrorStatus", np.byte),
        ("JogStatusCR", np.byte),
        ("CRRobotType", np.byte),
        ("DragButtonSignal", np.byte),
        ("EnableButtonSignal", np.byte),
        ("RecordButtonSignal", np.byte),
        ("ReappearButtonSignal", np.byte),
        ("JawButtonSignal", np.byte),
        ("SixForceOnline", np.byte),
        ("CollisionState", np.byte),
        ("ArmApproachState", np.byte),
        ("J4ApproachState", np.byte),
        ("J5ApproachState", np.byte),
        ("J6ApproachState", np.byte),
        ("reserve7", np.byte, (61,)),
        ("VibrationDisZ", np.float64),
        ("CurrentCommandId", np.uint64),
        ("MActual", np.float64, (6,)),
        ("Load", np.float64),
        ("CenterX", np.float64),
        ("CenterY", np.float64),
        ("CenterZ", np.float64),
        ("UserValue[6]", np.float64, (6,)),
        ("ToolValue[6]", np.float64, (6,)),
        ("reserve8", np.byte, (8,)),
        ("SixForceValue", np.float64, (6,)),
        ("TargetQuaternion", np.float64, (4,)),
        ("ActualQuaternion", np.float64, (4,)),
        ("AutoManualMode", np.uint16),
        ("ExportStatus", np.uint16),
        ("SafetyState", np.byte),
        ("reserve9", np.byte, (19,)),
    ]
)

FEEDBACK_PACKET_SIZE = 1440
FEEDBACK_MAGIC = 0x123456789ABCDEF
ROBOT_MODE_ENABLED_IDLE = 5
ROBOT_MODE_SINGLE_MOVE = 8
_NOT_TCP_HINT = (
    "el robot no está en modo TCP/IP remoto (Not Tcp). "
    "Activá el modo TCP/IP en DobotStudio Pro y verificá la red."
)


class DobotError(RuntimeError):
    """Error de comunicación o de comando contra el controlador Dobot."""


class DobotAdapter(RobotAdapter):
    """Actuación real sobre el dashboard (29999) y el feedback (30004)."""

    def __init__(
        self,
        robot_config: RobotConfig,
        *,
        clock=time.monotonic,
        socket_factory=None,
        start_feedback_thread: bool = True,
    ) -> None:
        super().__init__("dobot", robot_config)
        self.clock = clock
        self._socket_factory = socket_factory or socket.create_connection
        self.start_feedback_thread = bool(start_feedback_thread)

        self.last_error: str | None = None
        self._dashboard = None
        self._feedback = None
        self._reader: threading.Thread | None = None

        self._latest_feedback: dict = {}
        self._feedback_ts: float | None = None
        self._feedback_ok = False
        self._feedback_error_count = 0
        self._seen: dict[str, float] = {}
        self.command_log: list[dict] = []

        self._exec_lock = threading.RLock()
        self._cmd_lock = threading.RLock()
        self._fb_lock = threading.Lock()
        self._stop_event = threading.Event()

    # -- ciclo de vida -----------------------------------------------------
    def connect(self) -> bool:
        """Abre dashboard/feedback, arranca el reader y habilita el robot."""

        self._close_connections()
        self._stop_event.clear()
        with self._fb_lock:
            self._latest_feedback = {}
            self._feedback_ok = False
            self._feedback_ts = None
            self._feedback_error_count = 0

        robot = self.robot_config
        try:
            self._dashboard = self._socket_factory(
                (robot.ip, robot.dashboard_port), robot.connect_timeout_s
            )
            self._feedback = self._socket_factory(
                (robot.ip, robot.feedback_port), robot.connect_timeout_s
            )
            self._feedback.settimeout(1.0)

            if self.start_feedback_thread:
                self._reader = threading.Thread(
                    target=self._feedback_loop,
                    name="exp-brazo-dobot-feedback",
                    daemon=True,
                )
                self._reader.start()

            response = self._send("EnableRobot()")
            error_id, _result_id, _raw = self._parse(response)
            if error_id != 0:
                self.state = RobotState.FAULT
                self.last_error = (
                    f"EnableRobot falló (ErrorID={error_id}): {response.strip() or 'sin respuesta'}"
                )
                self._close_connections()
                return False

            self.state = RobotState.IDLE
            self.last_error = None
            return True
        except DobotError as exc:
            message = str(exc)
            self.state = RobotState.FAULT if "Not Tcp" in message else RobotState.DISCONNECTED
            self.last_error = message
            self._close_connections()
            return False
        except Exception as exc:
            self.state = RobotState.DISCONNECTED
            self.last_error = f"no se pudo conectar al Dobot: {exc}"
            self._close_connections()
            return False

    def disconnect(self) -> None:
        """Intenta detener/deshabilitar y cierra ambas conexiones."""

        if self._dashboard is not None:
            try:
                self._send("Stop()")
            except Exception:
                pass
            try:
                self._send("DisableRobot()")
            except Exception:
                pass
        self._close_connections()
        self.state = RobotState.DISCONNECTED
        self._feedback_ok = False

    def reconnect(self, max_delay: float = 15.0, attempts: int = 0) -> bool:
        """Reconecta con backoff exponencial (1, 2, 4, 8, 15...).

        ``attempts=0`` reintenta indefinidamente. El supervisor también puede
        llamar a este método; no bloquea al proceso principal por sí mismo.
        """

        try:
            self.disconnect()
        except Exception as exc:
            self.last_error = f"error al desconectar antes de reconectar: {exc}"

        delay = 1.0
        count = 0
        while attempts <= 0 or count < attempts:
            if self.connect():
                return True
            count += 1
            if attempts > 0 and count >= attempts:
                break
            time.sleep(min(delay, max_delay))
            delay = min(delay * 2.0, max_delay)
        return False

    # -- comandos ----------------------------------------------------------
    def _send(self, cmd: str) -> str:
        """Envía un comando por el dashboard y devuelve la respuesta cruda."""

        with self._cmd_lock:
            if self._dashboard is None:
                message = "dashboard no conectado"
                self.state = RobotState.DISCONNECTED
                self.last_error = message
                self._log_command(cmd, "", message)
                raise DobotError(message)
            try:
                self._dashboard.sendall(cmd.encode("utf-8"))
                raw = self._dashboard.recv(1024)
                response = raw.decode("utf-8", errors="replace") if raw else ""
                self._log_command(cmd, response, None)
                if not response.strip():
                    raise DobotError(f"respuesta vacía para {cmd!r}")
                return response
            except DobotError:
                raise
            except Exception as exc:
                self.state = RobotState.DISCONNECTED
                self.last_error = f"fallo de comunicación con el dashboard ({cmd!r}): {exc}"
                self._log_command(cmd, "", self.last_error)
                raise DobotError(self.last_error) from exc

    def _parse(self, response: str) -> tuple[int, int, str]:
        """Extrae ``(ErrorID, ResultID/CommandID, raw)`` de una respuesta."""

        if "Not Tcp" in response:
            raise DobotError(_NOT_TCP_HINT)
        numbers = [int(item) for item in re.findall(r"-?\d+", response)]
        if not numbers:
            raise DobotError(f"respuesta no reconocida del controlador: {response!r}")
        if len(numbers) >= 2:
            return numbers[0], numbers[1], response
        return numbers[0], 0, response

    def _log_command(self, cmd: str, response: str, error: str | None) -> None:
        self.command_log.append(
            {
                "cmd": cmd,
                "resp": response,
                "ts": time.time(),
                "error": error,
            }
        )
        if len(self.command_log) > 500:
            del self.command_log[:-500]

    # -- feedback ----------------------------------------------------------
    def _feedback_loop(self) -> None:
        sock = self._feedback
        while not self._stop_event.is_set() and sock is not None:
            try:
                packet = self._recv_packet(sock)
            except TimeoutError:
                continue
            except OSError as exc:
                if self._stop_event.is_set():
                    break
                self._set_feedback_error(
                    f"feedback TCP cerrado o con error: {exc}", fatal=True
                )
                return
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                self._set_feedback_error(f"error leyendo feedback: {exc}")
                return

            if packet is None:
                self._set_feedback_error("paquete de feedback incompleto")
                continue

            try:
                feedback = np.frombuffer(packet, dtype=MyType)
            except ValueError as exc:
                self._set_feedback_error(f"feedback con tamaño inválido: {exc}")
                continue

            if int(feedback["TestValue"][0]) != FEEDBACK_MAGIC:
                self._set_feedback_error("feedback con magic TestValue inválido")
                continue

            moment = self.clock()
            payload = {
                "robot_mode": int(feedback["RobotMode"][0]),
                "current_command_id": int(feedback["CurrentCommandId"][0]),
                "q_actual": [float(value) for value in feedback["QActual"][0]],
                "enable_status": int(feedback["EnableStatus"][0]),
                "error_status": int(feedback["ErrorStatus"][0]),
                "running_status": int(feedback["RunningStatus"][0]),
                "program_state": float(feedback["ProgramState"][0]),
                "ts": moment,
            }
            with self._fb_lock:
                self._latest_feedback = payload
                self._feedback_ts = moment
                self._feedback_ok = True
                self._feedback_error_count = 0

    def _recv_packet(self, sock) -> bytes | None:
        """Lee un paquete completo de 1440 bytes, con hasta 5 reintentos."""

        buffer = bytearray()
        attempts = 0
        while len(buffer) < FEEDBACK_PACKET_SIZE and attempts < 5:
            try:
                data = sock.recv(FEEDBACK_PACKET_SIZE - len(buffer))
            except TimeoutError:
                attempts += 1
                continue
            if not data:
                raise ConnectionError("socket de feedback cerrado")
            buffer.extend(data)
        if len(buffer) < FEEDBACK_PACKET_SIZE:
            return None
        return bytes(buffer[:FEEDBACK_PACKET_SIZE])

    def _set_feedback_error(self, message: str, fatal: bool = False) -> None:
        with self._fb_lock:
            self._feedback_ok = False
            self._feedback_error_count += 1
            if fatal or self._feedback_error_count >= 3:
                self.state = RobotState.FAULT
                self.last_error = f"feedback perdido: {message}"
            else:
                self.last_error = message

    def _latest(self) -> dict:
        with self._fb_lock:
            return dict(self._latest_feedback)

    def inject_feedback(self, payload: dict) -> None:
        """Inyecta feedback para pruebas sin abrir el puerto 30004."""

        def pick(*keys, default=None):
            for key in keys:
                if key in payload:
                    return payload[key]
            return default

        moment = float(pick("ts", "timestamp", default=self.clock()))
        data = {
            "robot_mode": int(pick("robot_mode", "RobotMode", default=ROBOT_MODE_ENABLED_IDLE)),
            "current_command_id": int(pick("current_command_id", "CurrentCommandId", default=0)),
            "q_actual": [
                float(value)
                for value in pick("q_actual", "QActual", default=[0.0] * 6)
            ],
            "enable_status": int(pick("enable_status", "EnableStatus", default=1)),
            "error_status": int(pick("error_status", "ErrorStatus", default=0)),
            "running_status": int(pick("running_status", "RunningStatus", default=0)),
            "program_state": float(pick("program_state", "ProgramState", default=0.0)),
            "ts": moment,
        }
        with self._fb_lock:
            self._latest_feedback = data
            self._feedback_ts = moment
            self._feedback_ok = True
            self._feedback_error_count = 0

    # -- movimiento --------------------------------------------------------
    def _wait_for_command(self, cmd_id: int, timeout_s: float) -> bool:
        """Espera a que el feedback confirme el comando.

        Condición principal copiada del demo oficial: ``RobotMode == 5`` y
        ``CurrentCommandId == cmd_id``. Como el firmware puede variar, se
        acepta ``CurrentCommandId >= cmd_id`` con modo 5 u 8 y un fallback por
        ``GetCurrentCommandID()``. **SIN VALIDAR** contra hardware real.
        """

        if cmd_id <= 0:
            raise DobotError("CommandID inválido")

        with self._fb_lock:
            feedback_ok = self._feedback_ok
            feedback_error_count = self._feedback_error_count
        if self.state == RobotState.FAULT or (
            not feedback_ok and feedback_error_count >= 3
        ):
            # Sin feedback confiable no se puede confirmar el movimiento; el
            # fallback por dashboard podría dar por terminado un comando que
            # el robot nunca aplicó.
            return False

        deadline = time.monotonic() + max(0.1, float(timeout_s))
        next_dashboard_check = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            feedback = self._latest()
            current = feedback.get("current_command_id")
            mode = feedback.get("robot_mode")
            if current is not None and current >= cmd_id and mode in (
                ROBOT_MODE_ENABLED_IDLE,
                ROBOT_MODE_SINGLE_MOVE,
            ):
                return True

            now = time.monotonic()
            if now >= next_dashboard_check:
                next_dashboard_check = now + 0.2
                try:
                    response = self._send("GetCurrentCommandID()")
                    numbers = [int(item) for item in re.findall(r"-?\d+", response)]
                    if len(numbers) >= 2 and numbers[-1] >= cmd_id:
                        return True
                except DobotError:
                    return False

            time.sleep(0.02)
        return False

    def _move_joint(
        self,
        joints: list[float],
        a: int,
        v: int,
        timeout_s: float = 10.0,
    ) -> bool:
        """Envía un ``MovJ`` por el dashboard y espera su finalización."""

        if self.state == RobotState.FAULT:
            raise DobotError(
                "adaptador en FAULT: feedback perdido; no se envía MovJ"
            )
        if len(joints) != len(JOINT_NAMES):
            raise DobotError(f"MovJ requiere {len(JOINT_NAMES)} ángulos, recibió {len(joints)}")
        if not all(math.isfinite(float(value)) for value in joints):
            raise DobotError("MovJ recibió un ángulo no finito")
        errors = self.validate_joint_target([float(x) for x in joints], where="MovJ")
        if errors:
            raise DobotError("; ".join(errors))
        accel = int(a)
        speed = int(v)
        if not (1 <= accel <= 100) or not (1 <= speed <= 100):
            raise DobotError(
                f"MovJ con aceleración/velocidad inválidas (a={accel}, v={speed}); "
                "el SDK acepta (0,100]."
            )

        command = (
            "MovJ(joint={"
            + ",".join(f"{float(value):.6f}" for value in joints)
            + f"}},a={accel},v={speed})"
        )
        response = self._send(command)
        error_id, command_id, raw = self._parse(response)
        if error_id != 0:
            raise DobotError(f"MovJ falló (ErrorID={error_id}): {raw.strip()}")
        if command_id <= 0:
            raise DobotError("respuesta sin CommandID; no se puede confirmar el movimiento")

        if not self._wait_for_command(command_id, timeout_s):
            # SIN VALIDAR: el criterio de fin depende del firmware.
            raise DobotError(
                f"timeout esperando finalización de MovJ (CommandID={command_id}); "
                "el firmware no confirmó RobotMode=5/8 con el comando aplicado "
                "(criterio SIN VALIDAR contra hardware real)."
            )
        return True

    def _home_joints(self) -> list[float]:
        return [float(value) for value in self.robot_config.home_joints]

    def _best_effort_home(self, acceleration_pct: int, speed_pct: int) -> bool:
        try:
            self._move_joint(
                self._home_joints(),
                acceleration_pct,
                min(speed_pct, 10),
                10.0,
            )
            return True
        except Exception as exc:
            self.last_error = f"no se pudo volver a home: {exc}"
            return False

    # -- contrato RobotAdapter --------------------------------------------
    def execute_behavior(self, behavior: Behavior, event_id: str) -> ExecutionResult:
        with self._exec_lock:
            if self.state in (RobotState.DISCONNECTED, RobotState.FAULT):
                return ExecutionResult(
                    ok=False,
                    behavior_id=behavior.id,
                    event_id=event_id,
                    error=f"adaptador no disponible: {self.state.value}",
                )

            now = self.clock()
            if event_id:
                seen_at = self._seen.get(event_id)
                if (
                    seen_at is not None
                    and now - seen_at < self.robot_config.dedupe_ttl_s
                ):
                    return ExecutionResult(
                        ok=True,
                        behavior_id=behavior.id,
                        event_id=event_id,
                        deduplicated=True,
                        steps_executed=0,
                    )
                self._seen[event_id] = now
                self._prune_seen(now)

            limited = self._check_limits(behavior, event_id)
            if limited is not None:
                return limited

            self.state = RobotState.BUSY
            duration_ms = 0.0
            executed = 0
            try:
                home = self._home_joints()
                for step in behavior.steps:
                    if self.state == RobotState.FAULT:
                        raise DobotError(
                            "feedback perdido; abortando comportamiento"
                        )
                    target = [
                        home[index] + float(step.deltas.get(joint, 0.0))
                        for index, joint in enumerate(JOINT_NAMES)
                    ]
                    timeout_s = max(3.0, step.duration_ms / 1000.0 + 2.0)
                    self._move_joint(
                        target,
                        behavior.acceleration_pct,
                        behavior.speed_pct,
                        timeout_s,
                    )
                    if step.pause_ms:
                        time.sleep(step.pause_ms / 1000.0)
                    executed += 1
                    duration_ms += float(step.duration_ms + step.pause_ms)

                # Idempotencia: siempre volver a home al terminar.
                self._move_joint(
                    self._home_joints(),
                    behavior.acceleration_pct,
                    min(behavior.speed_pct, 10),
                    10.0,
                )
                self.state = RobotState.IDLE
                return ExecutionResult(
                    ok=True,
                    behavior_id=behavior.id,
                    event_id=event_id,
                    duration_ms=duration_ms,
                    steps_executed=executed,
                )
            except Exception as exc:
                self._best_effort_home(behavior.acceleration_pct, behavior.speed_pct)
                self.state = RobotState.FAULT
                self.last_error = str(exc)
                return ExecutionResult(
                    ok=False,
                    behavior_id=behavior.id,
                    event_id=event_id,
                    error=str(exc),
                    duration_ms=duration_ms,
                    steps_executed=executed,
                )

    def home(self) -> ExecutionResult:
        with self._exec_lock:
            if self.state in (RobotState.DISCONNECTED, RobotState.FAULT):
                return ExecutionResult(
                    ok=False,
                    behavior_id="__home__",
                    error=f"adaptador no disponible: {self.state.value}",
                )

            self.state = RobotState.BUSY
            try:
                self._move_joint(
                    self._home_joints(),
                    self.robot_config.max_accel_pct,
                    min(self.robot_config.max_speed_pct, 10),
                    10.0,
                )
                self.state = RobotState.IDLE
                return ExecutionResult(
                    ok=True,
                    behavior_id="__home__",
                    steps_executed=0,
                )
            except Exception as exc:
                self.state = RobotState.FAULT
                self.last_error = str(exc)
                return ExecutionResult(
                    ok=False,
                    behavior_id="__home__",
                    error=str(exc),
                )

    def stop(self) -> None:
        try:
            if self._dashboard is not None:
                self._send("Stop()")
        except Exception:
            pass
        if self.state == RobotState.BUSY:
            self.state = RobotState.IDLE

    def status(self) -> dict:
        if self.state == RobotState.DISCONNECTED:
            return {
                "name": "dobot",
                "state": RobotState.DISCONNECTED.value,
                "last_error": self.last_error,
            }

        with self._fb_lock:
            feedback = dict(self._latest_feedback)
            feedback_ok = self._feedback_ok
            feedback_ts = self._feedback_ts

        robot_mode = feedback.get("robot_mode")
        current_command_id = feedback.get("current_command_id")
        angles = list(feedback.get("q_actual") or [])
        error_id = 0

        age = None
        if feedback_ts is not None:
            age = max(0.0, self.clock() - feedback_ts)

        feedback_fresh = feedback_ok and age is not None and age < 2.0
        # Si el feedback está fresco o el adaptador está en FAULT, no consultar
        # el dashboard: status() no debe colgarse. En FAULT, además, el estado
        # no se puede recuperar sin reconectar.
        if self.state != RobotState.FAULT and not feedback_fresh:
            try:
                response = self._query("RobotMode()", timeout=0.5)
                numbers = [int(item) for item in re.findall(r"-?\d+", response)]
                if len(numbers) >= 3:
                    robot_mode = numbers[-1]
            except Exception:
                pass

            try:
                response = self._query("GetErrorID()", timeout=0.5)
                numbers = [int(item) for item in re.findall(r"-?\d+", response)]
                # SIN VALIDAR: el formato exacto de GetErrorID depende del firmware.
                # Se descartan ErrorID y ResultID del encabezado de la respuesta.
                active = numbers[2:] if len(numbers) > 2 else []
                error_id = active[0] if active else 0
            except Exception:
                pass

            try:
                response = self._query("GetAngle()", timeout=0.5)
                parsed = self._parse_angles(response)
                if parsed:
                    angles = parsed
            except Exception:
                pass

        robot = self.robot_config
        return {
            "name": "dobot",
            "state": self.state.value,
            "last_error": self.last_error,
            "feedback_ok": bool(feedback_ok),
            "robot_mode": robot_mode,
            "error_id": error_id,
            "angles": angles,
            "current_command_id": current_command_id,
            "heartbeat_age_s": age,
            "ip": robot.ip,
            "ports": {
                "dashboard": robot.dashboard_port,
                "feedback": robot.feedback_port,
                "move": robot.move_port,
            },
        }

    # -- utilidades internas ----------------------------------------------
    def _query(self, command: str, timeout: float = 1.0) -> str:
        with self._cmd_lock:
            previous_timeout = None
            timeout_changed = False
            if self._dashboard is not None:
                try:
                    previous_timeout = self._dashboard.gettimeout()
                    self._dashboard.settimeout(timeout)
                    timeout_changed = True
                except Exception:
                    timeout_changed = False
            try:
                return self._send(command)
            finally:
                if timeout_changed:
                    try:
                        self._dashboard.settimeout(previous_timeout)
                    except Exception:
                        pass

    @staticmethod
    def _parse_angles(response: str) -> list[float] | None:
        match = re.search(r"GetAngle\(([^)]*)\)", response)
        body = match.group(1) if match else ""
        values = [float(item) for item in re.findall(r"-?\d+(?:\.\d+)?", body)]
        if len(values) < len(JOINT_NAMES):
            return None
        return values[: len(JOINT_NAMES)]

    def _prune_seen(self, now: float) -> None:
        ttl = self.robot_config.dedupe_ttl_s
        expired = [key for key, seen_at in self._seen.items() if now - seen_at >= ttl]
        for key in expired:
            del self._seen[key]

    def _close_connections(self) -> None:
        self._stop_event.set()

        feedback = self._feedback
        self._feedback = None
        if feedback is not None:
            try:
                feedback.close()
            except Exception:
                pass

        dashboard = self._dashboard
        self._dashboard = None
        if dashboard is not None:
            try:
                dashboard.close()
            except Exception:
                pass

        reader = self._reader
        self._reader = None
        if reader is not None and reader.is_alive() and reader is not threading.current_thread():
            reader.join(timeout=1.0)

        self._feedback_ok = False
