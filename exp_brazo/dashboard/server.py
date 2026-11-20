"""Servidor HTTP local del dashboard de Exp-brazo.

Expone un único ``FastAPI`` con API JSON, WebSocket de estado, stream MJPEG
y los archivos estáticos del frontend. Todo se sirve desde la máquina local:
no hay CDN, telemetría ni dependencias de red externa.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import secrets
import socket
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from ..contracts import RunMode
from .security import SESSION_COOKIE, generate_token, install_security

_LOGGER = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
_INDEX_FILE = STATIC_DIR / "index.html"
_UPLOAD_DIR = Path("recordings") / "uploads"

_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_LIMIT = 10_000
_MAX_UPLOAD_BYTES = 200 * 1024 * 1024
_MAX_UPLOAD_TOTAL_BYTES = 1024 * 1024 * 1024
_UPLOAD_SUFFIXES = frozenset({".mp4", ".avi", ".mov", ".mkv", ".webm"})


def _json_error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": message})


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _clean_identifier(value: str, label: str = "identificador") -> str:
    candidate = (value or "").strip()
    if not _ID_RE.match(candidate):
        raise ValueError(f"{label} inválido: usá letras, números, '_', '-', '.'")
    return candidate


def _upload_dir_total() -> int:
    """Suma el tamaño de los archivos regulares en el directorio de uploads."""

    if not _UPLOAD_DIR.is_dir():
        return 0
    total = 0
    try:
        entries = list(_UPLOAD_DIR.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def _safe_filename(raw: str) -> str:
    name = (raw or "").strip().replace("\\", "/").split("/")[-1]
    name = _FILENAME_RE.sub("_", name).strip("._-")
    if not name:
        raise ValueError("nombre de archivo inválido")
    return name[:128]


def _number(value, label: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{label} debe ser un número")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} debe ser un número finito")
    if minimum is not None and number < minimum:
        raise ValueError(f"{label} debe ser mayor o igual a {minimum:g}")
    return number


async def _optional_json(request: Request) -> dict:
    raw = await request.body()
    if not raw:
        return {}
    try:
        payload = await request.json()
    except Exception as exc:  # JSON inválido o cuerpo no parseable
        raise ValueError("cuerpo JSON inválido") from exc
    if not isinstance(payload, dict):
        raise ValueError("se esperaba un objeto JSON")
    return payload


def _local_ip_addresses() -> list[str]:
    """IPs locales del equipo, para permitir el dashboard en LAN."""

    addresses: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = str(info[4][0])
            if ip and ip not in addresses and not ip.startswith("127.") and ip != "::1":
                addresses.append(ip)
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            ip = str(probe.getsockname()[0])
            if ip and ip not in addresses and not ip.startswith("127."):
                addresses.append(ip)
        finally:
            probe.close()
    except OSError:
        pass
    return addresses


def _allowed_hosts(
    runtime, allow_remote: bool, effective_host: str | None = None
) -> list[str]:
    hosts = ["127.0.0.1", "localhost", "[::1]"]
    if allow_remote:
        configured = str(runtime.config.dashboard.host)
        candidates = [configured, effective_host, *(_local_ip_addresses())]
        for host in candidates:
            if not host:
                continue
            host = str(host)
            if host in {"0.0.0.0", "::"}:
                continue
            if host not in hosts:
                hosts.append(host)
        if len(hosts) == 3:
            # Último recurso: bind 0.0.0.0 sin IPs detectables. El token sigue
            # siendo obligatorio, así que no se relaja la autenticación.
            hosts.append("*")
    return hosts


def _allowed_origins(
    runtime, allowed_hosts: list[str], port: int | None = None
) -> list[str]:
    effective_port = int(port) if port else int(runtime.config.dashboard.port)
    origins: list[str] = []
    for host in allowed_hosts:
        if host in {"0.0.0.0", "::", "*"}:
            continue
        for scheme in ("http", "https"):
            origin = f"{scheme}://{host}:{effective_port}"
            if origin not in origins:
                origins.append(origin)
    # El TestClient de Starlette usa ``testserver`` como host y Origin.
    for origin in ("http://testserver", "https://testserver"):
        if origin not in origins:
            origins.append(origin)
    return origins


def create_app(
    runtime,
    *,
    token: str | None = None,
    allow_remote: bool = False,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
    port: int | None = None,
    effective_host: str | None = None,
    require_root_token: bool | None = None,
) -> FastAPI:
    """Construye la aplicación FastAPI atada a un ``Runtime`` ya creado."""

    app = FastAPI(
        title="Exp-brazo · dashboard local",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.runtime = runtime
    app.state.token = token or generate_token()
    app.state.allow_remote = allow_remote
    if require_root_token is None:
        require_root_token = allow_remote
    app.state.require_root_token = bool(require_root_token)

    hosts = (
        list(allowed_hosts)
        if allowed_hosts is not None
        else _allowed_hosts(runtime, allow_remote, effective_host)
    )
    origins = (
        list(allowed_origins)
        if allowed_origins is not None
        else _allowed_origins(runtime, hosts, port=port)
    )
    app.state.allowed_hosts = hosts
    app.state.allowed_origins = origins

    install_security(
        app,
        token=app.state.token,
        allowed_hosts=hosts,
        allowed_origins=origins,
        require_root_token=app.state.require_root_token,
    )

    @app.exception_handler(ValueError)
    async def _handle_value_error(request: Request, exc: ValueError) -> JSONResponse:
        return _json_error(400, str(exc) or "solicitud inválida")

    @app.exception_handler(ValidationError)
    async def _handle_validation_error(request: Request, exc: ValidationError) -> JSONResponse:
        return _json_error(400, f"validación falló: {exc}")

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", ()))
            details.append(f"{location or 'solicitud'}: {error.get('msg', 'inválido')}")
        return _json_error(400, "; ".join(details) or "solicitud inválida")

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        _LOGGER.exception("error no controlado atendiendo %s", request.url.path)
        return _json_error(500, "error interno del servidor")

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> Response:
        if not _INDEX_FILE.is_file():
            raise HTTPException(status_code=500, detail="falta index.html del dashboard")
        # Se lee en cada request para no cachear el token en el HTML servido.
        html = _INDEX_FILE.read_text(encoding="utf-8")
        html = html.replace("__EXP_BRAZO_TOKEN__", app.state.token)
        return HTMLResponse(content=html)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    # -- salud y estado ----------------------------------------------------
    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/api/cameras")
    def cameras() -> dict:
        devices: list[dict] = []
        try:
            for path in sorted(Path("/dev").glob("video*")):
                match = re.search(r"(\d+)$", path.name)
                if not match:
                    continue
                index = int(match.group(1))
                devices.append(
                    {
                        "index": index,
                        "path": str(path),
                        "active": index == int(runtime.config.camera.index),
                    }
                )
        except OSError:
            pass
        return {
            "devices": devices,
            "active_index": int(runtime.config.camera.index),
        }

    @app.get("/api/status")
    def status() -> dict:
        return runtime.status()

    # -- eventos -----------------------------------------------------------
    @app.get("/api/events")
    def events(
        limit: int = 200,
        gesture: str | None = None,
        result: str | None = None,
    ) -> dict:
        if limit < 0:
            raise ValueError("limit debe ser mayor o igual a 0")
        bounded = min(limit, _MAX_LIMIT)
        return {
            "events": runtime.events(
                limit=bounded,
                gesture=_clean_optional(gesture),
                result=_clean_optional(result),
            )
        }

    @app.get("/api/events/export")
    def export_events(format: str = "json") -> Response:
        fmt = (format or "").strip().lower()
        if fmt not in ("json", "csv"):
            raise ValueError("formato de exportación inválido: usá 'json' o 'csv'")
        body = runtime.export_events(fmt)
        media_type = "application/json" if fmt == "json" else "text/csv"
        return Response(
            content=body,
            media_type=media_type,
            headers={
                "Content-Disposition": f"attachment; filename=eventos.{fmt}",
                "Cache-Control": "no-store",
            },
        )

    # -- control del robot -------------------------------------------------
    @app.post("/api/control/trigger/{behavior_id}")
    def control_trigger(behavior_id: str) -> dict:
        return runtime.trigger(_clean_identifier(behavior_id, "comportamiento"))

    @app.post("/api/control/home")
    def control_home() -> dict:
        return runtime.home()

    @app.post("/api/control/pause")
    async def control_pause(request: Request) -> dict:
        body = await _optional_json(request)
        if body and "paused" in body:
            paused = body["paused"]
            if not isinstance(paused, bool):
                raise ValueError("'paused' debe ser booleano")
        else:
            current = await run_in_threadpool(runtime.status)
            paused = not bool(current.get("paused", False))
        await run_in_threadpool(runtime.set_paused, paused)
        current = await run_in_threadpool(runtime.status)
        return {"paused": paused, "status": current}

    @app.post("/api/control/mode")
    async def control_mode(request: Request) -> dict:
        body = await _optional_json(request)
        mode = body.get("mode")
        if not isinstance(mode, str):
            raise ValueError("falta el modo ('auto', 'manual', 'ambient' o 'simulation')")
        try:
            parsed = RunMode(mode.strip().lower())
        except ValueError as exc:
            raise ValueError(
                "modo inválido: usá 'auto', 'manual', 'ambient' o 'simulation'"
            ) from exc
        await run_in_threadpool(runtime.set_mode, parsed)
        current = await run_in_threadpool(runtime.status)
        return {"mode": parsed.value, "status": current}

    # -- configuración -----------------------------------------------------
    @app.get("/api/config")
    def get_config() -> dict:
        version = runtime.status().get("config_version")
        try:
            versions = [path.name for path in runtime.config_store.version_files()]
        except Exception:
            versions = []
        return {
            "config": runtime.get_config_dict(),
            "version": version,
            "versions": versions,
        }

    @app.put("/api/config")
    async def put_config(request: Request) -> dict:
        body = await _optional_json(request)
        payload = body.get("config") if isinstance(body.get("config"), dict) else body
        if not isinstance(payload, dict):
            raise ValueError("la configuración debe ser un objeto")
        updated = await run_in_threadpool(runtime.update_config, payload)
        return {"status": updated}

    @app.post("/api/config/rollback")
    def rollback_config() -> dict:
        return {"status": runtime.rollback_config()}

    # -- comportamientos ---------------------------------------------------
    @app.get("/api/behaviors")
    def get_behaviors() -> dict:
        return runtime.behaviors_dict()

    @app.put("/api/behaviors")
    async def put_behaviors(request: Request) -> dict:
        payload = await request.json()
        updated = await run_in_threadpool(runtime.update_behaviors, payload)
        return {"behaviors": updated["behaviors"]}

    @app.post("/api/behaviors/validate")
    async def validate_behaviors(request: Request) -> dict:
        payload = await request.json()
        errors = await run_in_threadpool(runtime.validate_behaviors, payload)
        return {"errors": errors}

    @app.get("/api/behaviors/{behavior_id}/preview")
    def preview_behavior(behavior_id: str) -> dict:
        identifier = _clean_identifier(behavior_id, "comportamiento")
        try:
            return runtime.preview_behavior(identifier)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/behaviors/{behavior_id}/test-mock")
    def test_behavior_mock(behavior_id: str) -> dict:
        return runtime.test_behavior_mock(_clean_identifier(behavior_id, "comportamiento"))

    # -- diagnóstico -------------------------------------------------------
    @app.get("/api/troubleshooting")
    def troubleshooting_get() -> dict:
        return runtime.troubleshooting()

    @app.post("/api/troubleshooting/run")
    def troubleshooting_run() -> dict:
        return runtime.troubleshooting()

    # -- grabación y replay ------------------------------------------------
    @app.post("/api/replay/start")
    async def replay_start(request: Request) -> dict:
        body = await _optional_json(request)
        path = body.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("falta la ruta del video a reproducir")
        if "\x00" in path or len(path) > 4096:
            raise ValueError("ruta de video inválida")
        return await run_in_threadpool(runtime.replay_start, path.strip())

    @app.post("/api/replay/upload")
    async def replay_upload(request: Request, filename: str | None = None) -> dict:
        if not (filename or "").strip():
            raise ValueError("falta el nombre del archivo")
        safe_name = _safe_filename(filename or "")
        suffix = Path(safe_name).suffix.lower()
        if suffix not in _UPLOAD_SUFFIXES:
            allowed = ", ".join(sorted(_UPLOAD_SUFFIXES))
            raise ValueError(f"extensión de video no permitida: usá una de {allowed}")

        content_length = request.headers.get("content-length")
        declared = None
        if content_length is not None:
            try:
                declared = int(content_length)
            except (TypeError, ValueError):
                declared = None
            if declared is not None and declared > _MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="archivo demasiado grande")

        existing_total = _upload_dir_total()
        if (
            declared is not None
            and existing_total + declared > _MAX_UPLOAD_TOTAL_BYTES
        ):
            raise HTTPException(
                status_code=413, detail="cuota total de uploads excedida"
            )

        _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        target = _UPLOAD_DIR / f"{int(time.time())}_{safe_name}"
        tmp = _UPLOAD_DIR / f".part-{uuid.uuid4().hex}"
        written = 0
        try:
            with open(tmp, "wb") as handle:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > _MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            status_code=413, detail="archivo demasiado grande"
                        )
                    await run_in_threadpool(handle.write, chunk)
                await run_in_threadpool(handle.flush)
            if written == 0:
                raise ValueError("el archivo está vacío")
            if existing_total + written > _MAX_UPLOAD_TOTAL_BYTES:
                raise HTTPException(
                    status_code=413, detail="cuota total de uploads excedida"
                )
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except BaseException:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            raise
        return await run_in_threadpool(runtime.replay_start, target)

    @app.get("/api/replay/status")
    def replay_status() -> dict:
        return runtime.replay_status()

    @app.post("/api/record/start")
    async def record_start(request: Request) -> dict:
        body = await _optional_json(request)
        seconds = _number(body.get("seconds", 10), "'seconds'", minimum=0.1)
        if seconds > 3600:
            raise ValueError("'seconds' no puede superar 3600")
        return await run_in_threadpool(runtime.record_start, seconds)

    @app.post("/api/record/stop")
    def record_stop() -> dict:
        return runtime.record_stop()

    # -- imágenes ----------------------------------------------------------
    @app.get("/api/snapshot.jpg")
    def snapshot() -> Response:
        jpeg = runtime.snapshot_jpeg()
        if not jpeg:
            raise HTTPException(status_code=404, detail="no hay frame de cámara disponible")
        return Response(
            content=jpeg,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/video.mjpeg")
    def video_mjpeg() -> StreamingResponse:
        async def frames():
            try:
                while True:
                    jpeg = runtime.snapshot_jpeg()
                    if jpeg:
                        yield (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
                        )
                    await asyncio.sleep(1 / 15)
            except (asyncio.CancelledError, ClientDisconnect):
                return

        return StreamingResponse(
            frames(),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    # -- WebSocket de estado ----------------------------------------------
    @app.websocket("/ws")
    async def websocket_status(websocket: WebSocket) -> None:
        presented = (
            websocket.query_params.get("token")
            or websocket.headers.get("x-expbrazo-token")
            or websocket.cookies.get(SESSION_COOKIE)
        )
        origin = websocket.headers.get("origin")
        token_ok = bool(presented) and secrets.compare_digest(
            presented.encode("utf-8"), app.state.token.encode("utf-8")
        )
        origin_ok = not origin or origin in app.state.allowed_origins
        if not token_ok or not origin_ok:
            await websocket.close(code=1008)
            return

        await websocket.accept()
        last_events = 0.0
        try:
            while True:
                try:
                    current = await run_in_threadpool(runtime.status)
                    await websocket.send_json({"type": "status", "data": current})
                    now = time.monotonic()
                    if now - last_events >= 1.0:
                        last_events = now
                        events = await run_in_threadpool(runtime.events, 40)
                        await websocket.send_json(
                            {"type": "events", "data": events}
                        )
                except (WebSocketDisconnect, ClientDisconnect, asyncio.CancelledError):
                    return
                except Exception:
                    return
                await asyncio.sleep(0.5)
        finally:
            try:
                await websocket.close()
            except Exception:
                pass

    return app
