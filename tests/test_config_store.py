"""Pruebas de carga, guardado versionado y rollback de configuración."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from exp_brazo.config_store import ConfigError, ConfigStore
from exp_brazo.contracts import Gesture


def _make_store(tmp_path: Path, repo_root: Path) -> tuple[ConfigStore, Path, Path]:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    shutil.copy(repo_root / "config" / "config.yaml", config_path)
    versions_dir = tmp_path / "config_versions"
    versions_dir.mkdir()
    return ConfigStore(config_path, versions_dir), config_path, versions_dir


def test_load_real_config(tmp_path, repo_root):
    store, config_path, versions_dir = _make_store(tmp_path, repo_root)

    config = store.get()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert config.version == raw["version"]
    assert config.robot.adapter == "mock"
    assert len(config.perception.gestures) == 4
    assert config.perception.gestures[Gesture.HOLA].hold_ms == 300
    assert (versions_dir / f"v{raw['version']:04d}.yaml").exists()


def test_save_increments_version_and_writes_file(tmp_path, repo_root):
    store, config_path, versions_dir = _make_store(tmp_path, repo_root)
    initial = store.get().version

    payload = store.get().model_dump(mode="python")
    payload["robot"]["max_speed_pct"] = 25

    saved = store.save(payload, reason="test")

    assert saved.version == initial + 1
    assert saved.robot.max_speed_pct == 25
    assert (versions_dir / f"v{initial + 1:04d}.yaml").exists()

    on_disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert on_disk["version"] == initial + 1
    assert on_disk["robot"]["max_speed_pct"] == 25
    assert store.get().robot.max_speed_pct == 25


def test_rollback_restores_previous_values(tmp_path, repo_root):
    store, config_path, versions_dir = _make_store(tmp_path, repo_root)
    initial = store.get().version

    payload = store.get().model_dump(mode="python")
    payload["robot"]["max_speed_pct"] = 25
    store.save(payload)

    restored = store.rollback()

    assert restored.version == initial + 2
    assert restored.robot.max_speed_pct == 30
    assert store.get().robot.max_speed_pct == 30
    assert (versions_dir / f"v{initial + 2:04d}.yaml").exists()

    on_disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert on_disk["version"] == initial + 2
    assert on_disk["robot"]["max_speed_pct"] == 30


def test_save_invalid_config_keeps_memory_unchanged(tmp_path, repo_root):
    store, _, versions_dir = _make_store(tmp_path, repo_root)
    initial = store.get().version

    payload = store.get().model_dump(mode="python")
    payload["robot"]["max_speed_pct"] = 999

    with pytest.raises(ValidationError):
        store.save(payload)

    assert store.get().version == initial
    assert store.get().robot.max_speed_pct == 30
    assert not (versions_dir / f"v{initial + 1:04d}.yaml").exists()


def test_missing_config_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="No existe"):
        ConfigStore(tmp_path / "config" / "missing.yaml", tmp_path / "versions")
