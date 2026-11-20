"""Configuración de logging local de Exp-brazo.

El logger raíz del proyecto es ``exp_brazo``: escribe a consola y a un
archivo rotativo dentro de ``logs/``. La función es idempotente para que
pueda invocarse desde varios puntos de arranque sin duplicar handlers.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOGGER_NAME = "exp_brazo"
_MARKER = "_exp_brazo_managed"


def setup_logging(
    log_dir: Path = Path("logs"),
    level: int = logging.INFO,
) -> logging.Logger:
    """Configura y devuelve el logger del proyecto.

    Crea ``log_dir`` si no existe y agrega un ``RotatingFileHandler``
    (2 MB por archivo, 5 backups) y un ``StreamHandler``. Si el logger ya
    fue configurado por esta función, no vuelve a agregar handlers.
    """

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    if any(getattr(handler, _MARKER, False) for handler in logger.handlers):
        return logger

    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        directory / "exp_brazo.log",
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    setattr(file_handler, _MARKER, True)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(level)
    setattr(stream_handler, _MARKER, True)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger
