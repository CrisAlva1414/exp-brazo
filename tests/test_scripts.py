"""Pruebas de los scripts de instalación/arranque y de las configuraciones semilla."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

SCRIPTS = ("install.sh", "start.sh")


def _run(script: Path, *args: str, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(script), *args],
        cwd=script.parent,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_exists_and_is_executable(repo_root: Path, name: str) -> None:
    script = repo_root / name
    assert script.is_file(), f"falta {name}"
    assert os.access(script, os.X_OK), f"{name} no tiene bit ejecutable"


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_syntax(repo_root: Path, name: str) -> None:
    result = subprocess.run(
        ["bash", "-n", str(repo_root / name)],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_install_help_mentions_flags(repo_root: Path) -> None:
    result = _run(repo_root / "install.sh", "--help")

    assert result.returncode == 0
    for flag in ("--check", "--offline", "--fetch-wheels", "--help"):
        assert flag in result.stdout


def test_start_help_mentions_flags(repo_root: Path) -> None:
    result = _run(repo_root / "start.sh", "--help")

    assert result.returncode == 0
    for flag in ("--check", "--mock", "--no-camera", "--port", "--host", "--open"):
        assert flag in result.stdout


def test_install_check_is_read_only(repo_root: Path) -> None:
    config_path = repo_root / "config" / "config.yaml"
    behaviors_path = repo_root / "config" / "behaviors.yaml"
    venv_path = repo_root / ".venv"

    config_mtime = config_path.stat().st_mtime_ns
    behaviors_mtime = behaviors_path.stat().st_mtime_ns
    venv_existed = venv_path.exists()

    result = _run(repo_root / "install.sh", "--check", timeout=60)

    assert result.returncode == 0, result.stdout + result.stderr
    assert config_path.stat().st_mtime_ns == config_mtime
    assert behaviors_path.stat().st_mtime_ns == behaviors_mtime
    if not venv_existed:
        assert not venv_path.exists(), "--check no debe crear .venv"


def test_default_config_matches_main_keys(repo_root: Path) -> None:
    main = yaml.safe_load(
        (repo_root / "config" / "config.yaml").read_text(encoding="utf-8")
    )
    defaults = yaml.safe_load(
        (repo_root / "config" / "defaults" / "config.yaml").read_text(encoding="utf-8")
    )

    assert isinstance(main, dict)
    assert isinstance(defaults, dict)
    assert defaults.keys() == main.keys()
    for section in ("camera", "perception", "decision", "robot", "dashboard", "supervisor"):
        assert section in defaults
        assert isinstance(defaults[section], dict)
        assert defaults[section].keys() == main[section].keys()


def test_default_behaviors_match_main_keys(repo_root: Path) -> None:
    main = yaml.safe_load(
        (repo_root / "config" / "behaviors.yaml").read_text(encoding="utf-8")
    )
    defaults = yaml.safe_load(
        (repo_root / "config" / "defaults" / "behaviors.yaml").read_text(encoding="utf-8")
    )

    assert isinstance(main, dict)
    assert isinstance(defaults, dict)
    assert defaults.keys() == main.keys()

    main_behaviors = main["behaviors"]
    default_behaviors = defaults["behaviors"]
    assert [item["id"] for item in default_behaviors] == [
        item["id"] for item in main_behaviors
    ]
    assert [item["trigger_gesture"] for item in default_behaviors] == [
        item["trigger_gesture"] for item in main_behaviors
    ]
    for default_item, main_item in zip(default_behaviors, main_behaviors, strict=True):
        assert default_item.keys() == main_item.keys()
