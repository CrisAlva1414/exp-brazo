"""Entrypoint del dashboard local de Exp-brazo.

Uso::

    python -m exp_brazo.main
    python -m exp_brazo.main --mock --no-camera

Levanta el runtime, el servidor FastAPI y el frontend local. No usa
``reload`` para no duplicar procesos ni abrir la cámara dos veces.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import uvicorn

from .dashboard.security import generate_token
from .dashboard.server import create_app
from .logging_setup import setup_logging
from .runtime import Runtime

_LOG_LEVELS = ("debug", "info", "warning", "error", "critical")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_MIN_REMOTE_TOKEN_LEN = 24


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m exp_brazo.main",
        description="Dashboard local del robot de feria Exp-brazo (offline).",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="usar el adaptador de robot simulado (sin brazo físico)",
    )
    parser.add_argument(
        "--no-camera",
        action="store_true",
        help="arrancar sin cámara (modo degradado, útil para probar la UI)",
    )
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="ruta de config.yaml (por defecto: config/config.yaml)",
    )
    parser.add_argument(
        "--behaviors",
        default="config/behaviors.yaml",
        help="ruta de behaviors.yaml (por defecto: config/behaviors.yaml)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="host de escucha (por defecto: dashboard.host de la config)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="puerto de escucha (por defecto: dashboard.port de la config)",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=_LOG_LEVELS,
        help="nivel de logging (por defecto: info)",
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help=(
            "permitir que el dashboard escuche fuera de loopback; exige "
            "EXP_BRAZO_TOKEN"
        ),
    )
    return parser


def _print_banner(host: str, port: int, *, bootstrap_token: str | None = None) -> None:
    print("", flush=True)
    print("=" * 64, flush=True)
    print("  Exp-brazo · Robot de feria", flush=True)
    display_host = "<IP-de-la-notebook>" if host in {"0.0.0.0", "::"} else host
    if bootstrap_token:
        print(
            f"  Bootstrap remoto: http://{display_host}:{port}/?token={bootstrap_token}",
            flush=True,
        )
        if host in {"0.0.0.0", "::"}:
            print("  Averiguá la IP con: hostname -I", flush=True)
    else:
        print(f"  Dashboard local: http://{host}:{port}", flush=True)
    print("=" * 64, flush=True)
    print("  SEGURIDAD: el paro de emergencia físico es la única", flush=True)
    print("  garantía real. La pausa de software es complementaria.", flush=True)
    print("  No dejes el brazo operando sin supervisión.", flush=True)
    print("=" * 64, flush=True)
    print("", flush=True)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    setup_logging(level=getattr(logging, args.log_level.upper(), logging.INFO))

    try:
        runtime = Runtime(
            config_path=Path(args.config),
            behaviors_path=Path(args.behaviors),
            force_adapter="mock" if args.mock else None,
            camera_enabled=not args.no_camera,
        )
    except Exception as exc:
        print(f"No se pudo iniciar Exp-brazo: {exc}", file=sys.stderr)
        return 1

    host = args.host or runtime.config.dashboard.host
    port = int(args.port or runtime.config.dashboard.port)

    if host not in _LOOPBACK_HOSTS and not args.allow_remote:
        print(
            "Por seguridad el dashboard solo escucha en 127.0.0.1. "
            "Para exponerlo usá --allow-remote y EXP_BRAZO_TOKEN",
            file=sys.stderr,
        )
        return 2

    if args.allow_remote:
        token = (os.environ.get("EXP_BRAZO_TOKEN") or "").strip()
        if len(token) < _MIN_REMOTE_TOKEN_LEN:
            print(
                "Con --allow-remote es obligatorio EXP_BRAZO_TOKEN "
                f"(mínimo {_MIN_REMOTE_TOKEN_LEN} caracteres). Generá uno con:\n"
                '  python3 -c "import secrets;print(secrets.token_urlsafe(32))"',
                file=sys.stderr,
            )
            return 2
    else:
        token = (os.environ.get("EXP_BRAZO_TOKEN") or "").strip() or generate_token()

    try:
        runtime.start()
        app = create_app(
            runtime,
            token=token,
            allow_remote=args.allow_remote,
            port=port,
            effective_host=host,
        )
        _print_banner(
            host,
            port,
            bootstrap_token=token if args.allow_remote else None,
        )
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level=args.log_level,
            access_log=False,
        )
    except KeyboardInterrupt:
        print("\nCerrando Exp-brazo...", flush=True)
    finally:
        runtime.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover - entrypoint
    raise SystemExit(main())
