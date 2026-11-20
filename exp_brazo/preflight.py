"""Preflight ejecutable antes de arrancar Exp-brazo.

Uso::

    python -m exp_brazo.preflight [--json]

No inicia el servidor ni mantiene la cámara abierta: solo valida el
entorno, la configuración, el catálogo, el puerto y, opcionalmente, la
cámara. El sistema debe poder arrancar degradado sin cámara.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import shutil
import socket
import sys
from pathlib import Path

import yaml

from .behaviors import BehaviorCatalogStore
from .contracts import AppConfig

_REQUIRED_DEPS = ("fastapi", "uvicorn", "pydantic", "yaml", "numpy")
_OPTIONAL_DEPS = ("cv2", "mediapipe")
_CRITICAL_DEPS = ("fastapi", "uvicorn", "pydantic", "yaml")
_DIST_NAMES = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "pydantic": "pydantic",
    "yaml": "PyYAML",
    "numpy": "numpy",
    "cv2": "opencv-python",
    "mediapipe": "mediapipe",
}


def _probe_dependency(module_name: str) -> dict:
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    version = getattr(module, "__version__", None)
    if version is None:
        try:
            version = importlib.metadata.version(_DIST_NAMES.get(module_name, module_name))
        except Exception:
            version = None
    return {"ok": True, "version": str(version) if version else "desconocida"}


def _probe_camera(index: int) -> dict:
    try:
        import cv2
    except Exception:
        return {
            "ok": False,
            "index": index,
            "detail": "OpenCV no está instalado (el sistema puede arrancar degradado)",
        }

    capture = None
    try:
        capture = cv2.VideoCapture(index)
        if hasattr(capture, "isOpened") and not capture.isOpened():
            return {
                "ok": False,
                "index": index,
                "detail": "la cámara no abrió (índice, permiso u ocupada)",
            }
        ok, frame = capture.read()
        if not ok or frame is None:
            return {
                "ok": False,
                "index": index,
                "detail": "la cámara abrió pero no entregó imagen",
            }
        return {"ok": True, "index": index, "detail": "cámara operativa"}
    except Exception as exc:
        return {"ok": False, "index": index, "detail": f"error de cámara: {exc}"}
    finally:
        if capture is not None:
            try:
                capture.release()
            except Exception:
                pass


def _probe_mediapipe_models(config: AppConfig | None) -> dict:
    """Verifica los modelos ``.task`` vendorizados en ``assets/models/``.

    No importa ``mediapipe`` ni descarga nada: los modelos son archivos
    locales versionados. Si falta uno, se reporta para que el operador
    ejecute ``./install.sh`` (o ``./install.sh --fetch-models`` con internet).
    """

    from .perception.engine import REQUIRED_MODELS, models_dir

    directory = models_dir()
    checked: list[dict] = []
    missing: list[str] = []
    for name in REQUIRED_MODELS:
        path = directory / name
        exists = path.is_file()
        size_bytes = path.stat().st_size if exists else None
        checked.append(
            {"name": name, "path": str(path), "exists": exists, "size_bytes": size_bytes}
        )
        if not exists:
            missing.append(str(path))

    if not missing:
        names = ", ".join(item["name"] for item in checked)
        return {
            "ok": True,
            "missing": [],
            "checked": checked,
            "detail": (
                f"modelos .task vendorizados presentes ({names}); "
                "no se descargan en runtime"
            ),
        }

    return {
        "ok": False,
        "missing": missing,
        "checked": checked,
        "detail": (
            f"faltan modelos .task vendorizados: {', '.join(missing)}; "
            "ejecutá ./install.sh (o ./install.sh --fetch-models)"
        ),
    }


def _port_free(host: str, port: int) -> dict:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            sock.bind((host, port))
        return {"ok": True, "detail": f"{host}:{port} libre"}
    except OSError as exc:
        return {"ok": False, "detail": f"{host}:{port} no disponible: {exc}"}


def _venv_info() -> dict:
    import os

    path = os.environ.get("VIRTUAL_ENV")
    if not path:
        candidate = Path.cwd() / ".venv"
        if candidate.exists():
            path = str(candidate)
        elif sys.prefix != sys.base_prefix:
            path = sys.prefix
    return {"ok": bool(path), "path": path}


def run_preflight(
    config_path: Path = Path("config/config.yaml"),
    behaviors_path: Path = Path("config/behaviors.yaml"),
    probe_camera: bool = True,
) -> dict:
    """Ejecuta todos los chequeos y devuelve un reporte serializable."""

    config_path = Path(config_path)
    behaviors_path = Path(behaviors_path)

    python_info = {
        "version": sys.version.split()[0],
        "ok": sys.version_info >= (3, 11),
    }

    deps: dict[str, dict] = {}
    for name in _REQUIRED_DEPS + _OPTIONAL_DEPS:
        deps[name] = _probe_dependency(name)

    errors: list[str] = []
    warnings: list[str] = []

    config_section: dict = {"ok": False, "error": None}
    config: AppConfig | None = None
    try:
        if not config_path.exists():
            raise FileNotFoundError(f"no existe {config_path}")
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config = AppConfig.model_validate(raw)
        config_section = {"ok": True, "error": None}
    except Exception as exc:
        config_section = {"ok": False, "error": str(exc)}

    behaviors_section: dict = {"ok": False, "count": 0, "errors": []}
    if config is not None:
        try:
            store = BehaviorCatalogStore(behaviors_path, config.robot)
            catalog = store.load()
            behaviors_section = {"ok": True, "count": len(catalog), "errors": []}
        except Exception as exc:
            errors_detail = list(getattr(exc, "errors", [])) or [str(exc)]
            behaviors_section = {"ok": False, "count": 0, "errors": errors_detail}
    else:
        behaviors_section = {
            "ok": False,
            "count": 0,
            "errors": ["no se pueden validar comportamientos sin configuración válida"],
        }

    if config is not None:
        camera_cfg = config.camera
    else:
        camera_cfg = None

    if not probe_camera:
        camera_section = {
            "ok": None,
            "index": camera_cfg.index if camera_cfg else None,
            "detail": "sondeo de cámara desactivado",
        }
    else:
        index = camera_cfg.index if camera_cfg else 0
        camera_section = _probe_camera(index)

    mediapipe_models = _probe_mediapipe_models(config)

    if config is not None:
        host = config.dashboard.host
        port = config.dashboard.port
    else:
        host, port = "127.0.0.1", 8000
    port_section = _port_free(host, port)

    try:
        disk_free_mb = int(shutil.disk_usage(".").free // (1024 * 1024))
    except Exception:
        disk_free_mb = 0

    venv_section = _venv_info()

    for name in _CRITICAL_DEPS:
        if not deps.get(name, {}).get("ok"):
            errors.append(f"dependencia crítica ausente: {name}")
    for name in _OPTIONAL_DEPS:
        if not deps.get(name, {}).get("ok"):
            warnings.append(
                f"dependencia opcional ausente: {name} (el sistema arranca degradado)"
            )
            if name == "cv2":
                warnings.append("sin OpenCV no hay cámara ni análisis de imagen")
    if not python_info["ok"]:
        errors.append("se requiere Python 3.11 o superior")
    if not config_section["ok"]:
        errors.append(f"configuración inválida: {config_section['error']}")
    if not behaviors_section["ok"]:
        detail = "; ".join(behaviors_section["errors"]) or "catálogo inválido"
        errors.append(f"comportamientos inválidos: {detail}")
    if not port_section["ok"]:
        errors.append(f"puerto ocupado: {port_section['detail']}")
    if probe_camera and camera_section.get("ok") is False:
        warnings.append(f"cámara: {camera_section['detail']} (arranque degradado)")
    if mediapipe_models["ok"] is False:
        warnings.append(f"modelos MediaPipe: {mediapipe_models['detail']}")
    if disk_free_mb and disk_free_mb < 500:
        warnings.append(f"poco espacio libre en disco: {disk_free_mb} MB")
    if not venv_section["ok"]:
        warnings.append("no se detectó entorno virtual; ejecutá ./install.sh")

    can_start = (
        all(deps.get(name, {}).get("ok") for name in _CRITICAL_DEPS)
        and bool(config_section["ok"])
        and bool(behaviors_section["ok"])
        and bool(port_section["ok"])
    )

    return {
        "python": python_info,
        "deps": deps,
        "config": config_section,
        "behaviors": behaviors_section,
        "camera": camera_section,
        "mediapipe_model_files": mediapipe_models,
        "port_8000": port_section,
        "disk_free_mb": disk_free_mb,
        "venv": venv_section,
        "errors": errors,
        "warnings": warnings,
        "can_start": can_start,
    }


def _format_report(report: dict) -> str:
    lines = ["Preflight de Exp-brazo", "=" * 40]
    lines.append(
        f"Python: {report['python']['version']} "
        f"({'OK' if report['python']['ok'] else 'FALLA'})"
    )
    for name, info in report["deps"].items():
        if info.get("ok"):
            lines.append(f"Dependencia {name}: OK ({info.get('version')})")
        else:
            lines.append(f"Dependencia {name}: FALLA ({info.get('error')})")
    config_label = (
        "OK" if report["config"]["ok"] else f"FALLA ({report['config']['error']})"
    )
    lines.append(f"Configuración: {config_label}")
    if report["behaviors"]["ok"]:
        lines.append(f"Comportamientos: OK ({report['behaviors']['count']})")
    else:
        lines.append(f"Comportamientos: FALLA ({'; '.join(report['behaviors']['errors'])})")
    lines.append(f"Cámara: {report['camera']['detail']}")
    lines.append(f"Modelos MediaPipe: {report['mediapipe_model_files']['detail']}")
    lines.append(f"Puerto 8000: {report['port_8000']['detail']}")
    lines.append(f"Disco libre: {report['disk_free_mb']} MB")
    lines.append(
        "Entorno virtual: " + (report["venv"]["path"] or "no detectado")
    )
    for warning in report["warnings"]:
        lines.append(f"Advertencia: {warning}")
    for error in report["errors"]:
        lines.append(f"Error: {error}")
    lines.append("Puede arrancar: " + ("SÍ" if report["can_start"] else "NO"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chequeo previo de Exp-brazo")
    parser.add_argument("--json", action="store_true", help="imprime también el reporte JSON")
    args = parser.parse_args(argv)

    report = run_preflight()
    print(_format_report(report))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, default=str))
    return 0 if report["can_start"] else 1


if __name__ == "__main__":  # pragma: no cover - entrada de consola
    raise SystemExit(main())
