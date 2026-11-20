"""Cierre final: host LAN efectivo y cotas de configuración."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from exp_brazo.contracts import BehaviorStep, DashboardConfig, RobotConfig
from exp_brazo.dashboard.server import create_app


class _Dashboard:
    host = "127.0.0.1"
    port = 8071


class _Camera:
    index = 0


class _Config:
    dashboard = _Dashboard()
    camera = _Camera()


class _FakeRuntime:
    config = _Config()

    def status(self) -> dict:
        return {"ok": True}


def test_effective_lan_host_is_allowed() -> None:
    app = create_app(
        _FakeRuntime(),
        token="test-token",
        allow_remote=True,
        effective_host="192.168.1.10",
        port=8071,
    )
    assert "192.168.1.10" in app.state.allowed_hosts
    assert "http://192.168.1.10:8071" in app.state.allowed_origins

    client = TestClient(app, base_url="http://192.168.1.10:8071")
    assert client.get("/").status_code == 401
    assert client.get("/?token=test-token").status_code == 200
    assert (
        client.get(
            "/api/status", headers={"X-ExpBrazo-Token": "test-token"}
        ).status_code
        == 200
    )
    cameras = client.get(
        "/api/cameras", headers={"X-ExpBrazo-Token": "test-token"}
    )
    assert cameras.status_code == 200
    payload = cameras.json()
    assert isinstance(payload["devices"], list)
    assert payload["active_index"] == 0


def test_nan_delta_is_rejected() -> None:
    with pytest.raises(ValidationError):
        BehaviorStep(deltas={"J1": float("nan")}, duration_ms=10, pause_ms=0)


def test_dedupe_ttl_bounds(app_cfg) -> None:
    raw = app_cfg.robot.model_dump()
    for bad in (-1, float("inf")):
        raw["dedupe_ttl_s"] = bad
        with pytest.raises(ValidationError):
            RobotConfig.model_validate(raw)
    raw["dedupe_ttl_s"] = 86400
    assert RobotConfig.model_validate(raw).dedupe_ttl_s == 86400


def test_dashboard_host_and_port_bounds() -> None:
    for bad_host in ("*", "a b", "http://x"):
        with pytest.raises(ValidationError):
            DashboardConfig(host=bad_host)
    for bad_port in (0, 70000):
        with pytest.raises(ValidationError):
            DashboardConfig(port=bad_port)
    assert DashboardConfig(host="192.168.1.10", port=8071).port == 8071
