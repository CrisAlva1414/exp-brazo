"""Pruebas del catálogo y la validación de comportamientos."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from exp_brazo.behaviors import (
    BehaviorCatalogStore,
    BehaviorValidationError,
    validate_behavior,
    validate_behavior_set,
)
from exp_brazo.contracts import Behavior, BehaviorStep, Gesture


def _make_behavior(
    *,
    behavior_id: str = "test",
    deltas: dict[str, float] | None = None,
    speed_pct: int = 10,
    acceleration_pct: int = 10,
    priority: int = 0,
) -> Behavior:
    return Behavior(
        id=behavior_id,
        trigger_gesture=Gesture.HOLA,
        name="Test",
        speed_pct=speed_pct,
        acceleration_pct=acceleration_pct,
        priority=priority,
        steps=[
            BehaviorStep(
                deltas=deltas or {"J1": 1.0},
                duration_ms=100,
                pause_ms=0,
            )
        ],
    )


def test_real_catalog_loads_seed_behaviors(catalog):
    ids = {behavior.id for behavior in catalog}
    assert len(catalog) >= 4
    assert {"hola", "si", "no", "seis_siete"}.issubset(ids)


def test_behavior_step_fills_missing_joints():
    step = BehaviorStep(deltas={"J2": 3.0}, duration_ms=100, pause_ms=0)

    assert list(step.deltas) == ["J1", "J2", "J3", "J4", "J5", "J6"]
    assert step.deltas["J2"] == 3.0
    assert step.deltas["J1"] == 0.0


def test_behavior_step_rejects_unknown_joint():
    with pytest.raises(ValidationError, match="articulación desconocida"):
        BehaviorStep(deltas={"J7": 1.0}, duration_ms=100, pause_ms=0)


def test_joint_limit_violation(robot_cfg):
    behavior = _make_behavior(deltas={"J1": 1000.0})

    errors = validate_behavior(behavior, robot_cfg)

    assert any("excede el límite" in error for error in errors)


def test_speed_above_robot_max(robot_cfg):
    behavior = _make_behavior(speed_pct=robot_cfg.max_speed_pct + 1)

    errors = validate_behavior(behavior, robot_cfg)

    assert any("speed_pct" in error for error in errors)


def test_acceleration_above_robot_max(robot_cfg):
    behavior = _make_behavior(acceleration_pct=robot_cfg.max_accel_pct + 1)

    errors = validate_behavior(behavior, robot_cfg)

    assert any("acceleration_pct" in error for error in errors)


def test_duplicate_ids_are_reported(robot_cfg):
    behavior = _make_behavior(behavior_id="duplicado")

    errors = validate_behavior_set([behavior, behavior], robot_cfg)

    assert any("duplicado" in error.lower() for error in errors)


def test_invalid_yaml_raises_validation_error(tmp_path, robot_cfg):
    path = tmp_path / "behaviors.yaml"
    path.write_text("behaviors: [\n", encoding="utf-8")
    store = BehaviorCatalogStore(path, robot_cfg)

    with pytest.raises(BehaviorValidationError):
        store.load()


def test_missing_catalog_raises_validation_error(tmp_path, robot_cfg):
    store = BehaviorCatalogStore(tmp_path / "missing.yaml", robot_cfg)

    with pytest.raises(BehaviorValidationError):
        store.load()
