"""Hardening del dashboard local: token, Origin, hosts y cabeceras.

El dashboard de feria no tiene usuarios: se protege con un token compartido
generado en cada arranque en loopback (o provisto por ``EXP_BRAZO_TOKEN`` al
exponerlo con ``--allow-remote``). Además se aplican controles de host,
Origin y cabeceras defensivas para reducir CSRF, clickjacking y sniffing.
"""

from __future__ import annotations

import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_ALWAYS_PUBLIC_PATHS = frozenset({"/api/health", "/favicon.ico"})
_PUBLIC_PREFIXES = ("/static/",)
SESSION_COOKIE = "exp_brazo_session"
_MAX_JSON_BYTES = 1_000_000

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; "
        "connect-src 'self' ws://127.0.0.1:* ws://localhost:*; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'self'"
    ),
}


def generate_token() -> str:
    """Genera un token de capacidad para el dashboard."""
    return secrets.token_urlsafe(32)


def is_public_path(path: str, *, require_root_token: bool = False) -> bool:
    """Indica si ``path`` puede consultarse sin token.

    ``/`` solo es público en loopback; en modo remoto exige el token de
    bootstrap para no filtrar el token inyectado en el HTML.
    """

    if path == "/":
        return not require_root_token
    if path in _ALWAYS_PUBLIC_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


def _matches(candidate: str | None, token: str) -> bool:
    if not candidate:
        return False
    try:
        return secrets.compare_digest(candidate.encode("utf-8"), token.encode("utf-8"))
    except (TypeError, ValueError):
        return False


def _header_or_query_token(request: Request) -> str | None:
    header = request.headers.get("x-expbrazo-token")
    if header:
        return header
    return request.query_params.get("token")


def _presented_token(request: Request) -> str | None:
    token = _header_or_query_token(request)
    if token:
        return token
    return request.cookies.get(SESSION_COOKIE)


def _harden(response: Response) -> Response:
    for name, value in _SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


def _error(status_code: int, message: str) -> JSONResponse:
    response = _harden(
        JSONResponse(status_code=status_code, content={"error": message})
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def install_security(
    app: FastAPI,
    *,
    token: str,
    allowed_hosts: list[str],
    allowed_origins: list[str],
    require_root_token: bool = False,
) -> None:
    """Instala host checking, guard HTTP de token/Origin y cabeceras."""
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    origins = frozenset(allowed_origins)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method not in _SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin and origin not in origins:
                return _error(403, "origen no permitido")

        path = request.url.path
        public = is_public_path(path, require_root_token=require_root_token)
        header_or_query = _header_or_query_token(request)
        token_by_header_or_query = _matches(header_or_query, token)
        presented_ok = token_by_header_or_query or _matches(
            request.cookies.get(SESSION_COOKIE), token
        )

        if path == "/" and require_root_token and not presented_ok:
            return _error(401, "token inválido o ausente")
        if not public and not presented_ok:
            return _error(401, "token inválido o ausente")

        if (
            request.method in {"POST", "PUT", "PATCH"}
            and request.headers.get("content-type", "").lower().startswith(
                "application/json"
            )
        ):
            raw_length = request.headers.get("content-length")
            if raw_length is not None:
                try:
                    declared_length = int(raw_length)
                except (TypeError, ValueError):
                    declared_length = None
                if declared_length is not None and declared_length > _MAX_JSON_BYTES:
                    return _error(413, "cuerpo JSON demasiado grande")

        response = await call_next(request)

        if path == "/" and token_by_header_or_query:
            response.set_cookie(
                SESSION_COOKIE,
                token,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
                max_age=12 * 3600,
                path="/",
            )
        if path == "/" or path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        return _harden(response)
