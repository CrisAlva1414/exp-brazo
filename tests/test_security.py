"""Pruebas del hardening del dashboard: token, Origin, hosts y cabeceras."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from exp_brazo.dashboard.security import SESSION_COOKIE
from exp_brazo.dashboard.server import create_app
from exp_brazo.robot.mock import MockAdapter
from exp_brazo.runtime import Runtime

TOKEN = "test-token"
AUTH = {"X-ExpBrazo-Token": TOKEN}


@pytest.fixture
def runtime(tmp_path: Path, repo_root: Path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    behaviors_path = config_dir / "behaviors.yaml"
    shutil.copy(repo_root / "config" / "config.yaml", config_path)
    shutil.copy(repo_root / "config" / "behaviors.yaml", behaviors_path)

    instance = Runtime(
        config_path=config_path,
        behaviors_path=behaviors_path,
        force_adapter="mock",
        camera_enabled=False,
        execute_inline=True,
        adapter_factory=lambda robot_config: MockAdapter(robot_config, time_scale=0),
    )
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


@pytest.fixture
def client(runtime: Runtime):
    app = create_app(
        runtime,
        token=TOKEN,
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
    )
    with TestClient(app) as test_client:
        yield test_client


def test_health_is_public(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_status_requires_token(client: TestClient) -> None:
    denied = client.get("/api/status")
    assert denied.status_code == 401
    assert denied.json() == {"error": "token inválido o ausente"}

    allowed = client.get("/api/status", headers=AUTH)
    assert allowed.status_code == 200
    assert allowed.json()["mode"] == "auto"

    query = client.get("/api/status", params={"token": TOKEN})
    assert query.status_code == 200


def test_trigger_requires_token(client: TestClient, runtime: Runtime) -> None:
    before = len(runtime.adapter.history)

    denied = client.post("/api/control/trigger/hola")
    assert denied.status_code == 401
    assert len(runtime.adapter.history) == before

    allowed = client.post("/api/control/trigger/hola", headers=AUTH)
    assert allowed.status_code == 200
    assert allowed.json()["result"] == "executed"
    assert len(runtime.adapter.history) == before + 1


def test_index_injects_token_and_security_headers(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert TOKEN in response.text
    assert "__EXP_BRAZO_TOKEN__" not in response.text
    assert 'name="exp-brazo-token"' in response.text
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]


def test_foreign_origin_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/control/trigger/hola",
        headers={**AUTH, "Origin": "http://evil.example"},
    )
    assert response.status_code == 403
    assert response.json() == {"error": "origen no permitido"}


def test_allowed_origins_use_effective_port(runtime: Runtime) -> None:
    """El Origin debe validarse contra el puerto real de uvicorn (8032)."""

    app = create_app(runtime, token=TOKEN, port=8032)
    with TestClient(app, base_url="http://127.0.0.1:8032") as test_client:
        allowed = test_client.post(
            "/api/control/trigger/hola",
            headers={**AUTH, "Origin": "http://127.0.0.1:8032"},
        )
        assert allowed.status_code == 200

        denied = test_client.post(
            "/api/control/trigger/hola",
            headers={**AUTH, "Origin": "http://evil.example"},
        )
        assert denied.status_code == 403
        assert denied.json() == {"error": "origen no permitido"}


def test_websocket_requires_token(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass


def test_websocket_accepts_valid_token(client: TestClient) -> None:
    with client.websocket_connect("/ws?token=" + TOKEN) as websocket:
        first = websocket.receive_json()
        second = websocket.receive_json()

    assert first["type"] == "status"
    assert first["data"]["mode"] == "auto"
    assert second["type"] == "events"


def _remote_app(runtime: Runtime):
    return create_app(
        runtime,
        token=TOKEN,
        allow_remote=True,
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
    )


def test_remote_index_requires_bootstrap_token(runtime: Runtime) -> None:
    app = _remote_app(runtime)
    with TestClient(app) as remote:
        denied = remote.get("/")
        assert denied.status_code == 401

        bootstrap = remote.get("/", params={"token": TOKEN})
        assert bootstrap.status_code == 200
        assert TOKEN in bootstrap.text
        assert 'name="exp-brazo-token"' in bootstrap.text
        assert bootstrap.headers["Cache-Control"] == "no-store"
        cookie = bootstrap.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie
        assert SESSION_COOKIE in cookie


def test_remote_session_cookie_authorizes_root_and_api(runtime: Runtime) -> None:
    app = _remote_app(runtime)
    with TestClient(app) as remote:
        bootstrap = remote.get("/", params={"token": TOKEN})
        assert bootstrap.status_code == 200

        root_with_cookie = remote.get("/")
        assert root_with_cookie.status_code == 200
        assert TOKEN in root_with_cookie.text

        status_with_cookie = remote.get("/api/status")
        assert status_with_cookie.status_code == 200

        remote.cookies.clear()
        status_without_cookie = remote.get("/api/status")
        assert status_without_cookie.status_code == 401
        assert remote.get("/").status_code == 401


def test_loopback_root_remains_public_without_token(runtime: Runtime) -> None:
    app = create_app(
        runtime,
        token=TOKEN,
        allow_remote=False,
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
    )
    with TestClient(app) as loopback:
        assert loopback.get("/").status_code == 200


def test_remote_health_and_static_remain_public(runtime: Runtime) -> None:
    app = _remote_app(runtime)
    with TestClient(app) as remote:
        assert remote.get("/api/health").status_code == 200
        assert remote.get("/static/app.js").status_code == 200


def test_websocket_accepts_session_cookie(runtime: Runtime) -> None:
    app = _remote_app(runtime)
    with TestClient(app) as remote:
        with remote.websocket_connect(
            "/ws", headers={"cookie": f"{SESSION_COOKIE}={TOKEN}"}
        ) as websocket:
            first = websocket.receive_json()
        assert first["type"] == "status"


def test_large_json_body_is_rejected_before_processing(client: TestClient) -> None:
    response = client.post(
        "/api/control/pause",
        content=b"x" * 1_000_001,
        headers={**AUTH, "Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {"error": "cuerpo JSON demasiado grande"}
