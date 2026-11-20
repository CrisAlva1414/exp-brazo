"""Contratos de versiones de las dependencias runtime.

El test solo parsea ``requirements.txt``: no requiere que los paquetes estén
instalados, para que la suite pueda correr en entornos mínimos.
"""

from __future__ import annotations

from pathlib import Path

REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"

EXPECTED_VERSIONS = {
    "opencv-contrib-python": "4.12.0.88",
    "fastapi": "0.139.0",
    "starlette": "1.3.1",
    "numpy": "2.2.6",
    "mediapipe": "0.10.35",
}


def _parse_requirements(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name, separator, version = line.partition("==")
        assert separator, f"requisito sin versión fija: {line}"
        package = name.split("[", 1)[0].strip().lower()
        entries[package] = version.strip()
    return entries


def test_runtime_dependencies_are_pinned() -> None:
    requirements = _parse_requirements(REQUIREMENTS)

    for package, version in EXPECTED_VERSIONS.items():
        assert requirements.get(package) == version, (
            f"{package} debe estar en {version}; encontrado: {requirements.get(package)}"
        )


def test_opencv_python_and_python_multipart_are_absent() -> None:
    requirements = _parse_requirements(REQUIREMENTS)

    assert "python-multipart" not in requirements
    assert "opencv-python" not in requirements
