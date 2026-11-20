"""Pruebas del preflight ejecutable."""

from __future__ import annotations

import shutil
from pathlib import Path

from exp_brazo.preflight import run_preflight


def _copy_configs(tmp_path: Path, repo_root: Path) -> tuple[Path, Path]:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    behaviors_path = config_dir / "behaviors.yaml"
    shutil.copy(repo_root / "config" / "config.yaml", config_path)
    shutil.copy(repo_root / "config" / "behaviors.yaml", behaviors_path)
    return config_path, behaviors_path


def test_run_preflight_reports_environment(tmp_path: Path, repo_root: Path) -> None:
    config_path, behaviors_path = _copy_configs(tmp_path, repo_root)

    report = run_preflight(
        config_path=config_path,
        behaviors_path=behaviors_path,
        probe_camera=False,
    )

    assert isinstance(report["can_start"], bool)
    assert set(report) >= {
        "python",
        "deps",
        "config",
        "behaviors",
        "camera",
        "mediapipe_model_files",
        "port_8000",
        "disk_free_mb",
        "venv",
        "errors",
        "warnings",
        "can_start",
    }
    assert set(report["mediapipe_model_files"]) == {"ok", "missing", "checked", "detail"}
    model_names = {item["name"] for item in report["mediapipe_model_files"]["checked"]}
    assert model_names == {
        "pose_landmarker_lite.task",
        "pose_landmarker_full.task",
        "hand_landmarker.task",
    }
    for dependency in ("fastapi", "pydantic", "yaml"):
        assert report["deps"][dependency]["ok"] is True
    assert report["config"]["ok"] is True
    assert report["behaviors"]["ok"] is True
    assert report["behaviors"]["count"] >= 4
    assert report["camera"]["ok"] is None
    assert report["camera"]["detail"] == "sondeo de cámara desactivado"
    assert isinstance(report["disk_free_mb"], int)
    assert isinstance(report["errors"], list)
    assert isinstance(report["warnings"], list)


def test_run_preflight_does_not_crash_without_cv2(tmp_path: Path, repo_root: Path) -> None:
    config_path, behaviors_path = _copy_configs(tmp_path, repo_root)

    # Con sonda de cámara, si cv2/mediapipe no están, debe reportar warning
    # y NO lanzar. El arranque degradado sigue siendo válido.
    report = run_preflight(
        config_path=config_path,
        behaviors_path=behaviors_path,
        probe_camera=True,
    )

    assert "cv2" in report["deps"]
    assert "mediapipe" in report["deps"]
    assert isinstance(report["camera"]["ok"], bool | None)


def test_run_preflight_missing_config_blocks_start(tmp_path: Path) -> None:
    report = run_preflight(
        config_path=tmp_path / "missing.yaml",
        behaviors_path=tmp_path / "missing-behaviors.yaml",
        probe_camera=False,
    )

    assert report["config"]["ok"] is False
    assert report["behaviors"]["ok"] is False
    assert report["can_start"] is False
    assert report["errors"]
