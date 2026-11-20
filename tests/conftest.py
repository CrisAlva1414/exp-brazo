"""Fixtures compartidas de la suite de Exp-brazo."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from exp_brazo.behaviors import BehaviorCatalogStore  # noqa: E402
from exp_brazo.contracts import AppConfig, Behavior, Gesture, PerceptionEvent  # noqa: E402


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture
def app_cfg() -> AppConfig:
    raw = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
    return AppConfig.model_validate(raw)


@pytest.fixture
def robot_cfg(app_cfg: AppConfig):
    return app_cfg.robot


@pytest.fixture
def catalog_store(robot_cfg):
    store = BehaviorCatalogStore(ROOT / "config" / "behaviors.yaml", robot_cfg)
    store.load()
    return store


@pytest.fixture
def catalog(catalog_store: BehaviorCatalogStore) -> list[Behavior]:
    return catalog_store.get_all()


@pytest.fixture
def behavior(catalog: list[Behavior]) -> Behavior:
    return catalog[0]


@pytest.fixture
def event():
    def make(
        gesture: Gesture = Gesture.HOLA,
        confidence: float = 0.9,
        event_id: str | None = None,
    ) -> PerceptionEvent:
        payload = {"gesture": gesture, "confidence": confidence}
        if event_id is not None:
            payload["event_id"] = event_id
        return PerceptionEvent.model_validate(payload)

    return make
