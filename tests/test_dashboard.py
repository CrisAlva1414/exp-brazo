"""Pruebas HTTP del dashboard local con el adaptador mock."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from exp_brazo.dashboard import server as dashboard_server
from exp_brazo.dashboard.server import create_app
from exp_brazo.robot.mock import MockAdapter
from exp_brazo.runtime import Runtime


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
        token="test-token",
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
    )
    with TestClient(app) as test_client:
        test_client.headers.update({"X-ExpBrazo-Token": "test-token"})
        yield test_client


def test_static_files_are_served(client: TestClient) -> None:
    root = client.get("/")
    assert root.status_code == 200
    assert "Exp-brazo" in root.text

    app_js = client.get("/static/app.js")
    assert app_js.status_code == 200
    assert "addEventListener" in app_js.text

    style_css = client.get("/static/style.css")
    assert style_css.status_code == 200
    assert "--bg" in style_css.text


def test_core_read_endpoints(client: TestClient) -> None:
    assert client.get("/api/health").json() == {"ok": True}

    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.json()["mode"] == "auto"

    config = client.get("/api/config").json()
    assert "config" in config
    assert config["config"]["robot"]["adapter"] == "mock"
    assert isinstance(config["versions"], list)

    behaviors = client.get("/api/behaviors").json()
    assert len(behaviors["behaviors"]) >= 4

    troubleshooting = client.get("/api/troubleshooting")
    assert troubleshooting.status_code == 200
    results = troubleshooting.json()["results"]
    assert results
    assert all("id" in item for item in results)


def test_manual_trigger_executes_inline(client: TestClient, runtime: Runtime) -> None:
    before = len(runtime.adapter.history)

    record = client.post("/api/control/trigger/hola").json()

    assert record["result"] == "executed"
    assert len(runtime.adapter.history) == before + 1
    assert runtime.adapter.history[-1]["behavior_id"] == "hola"


def test_pause_turns_trigger_into_would_have_done(client: TestClient, runtime: Runtime) -> None:
    paused = client.post("/api/control/pause", json={"paused": True})
    assert paused.status_code == 200
    assert paused.json()["paused"] is True
    before = len(runtime.adapter.history)

    record = client.post("/api/control/trigger/hola").json()

    assert record["result"] == "would_have_done"
    assert len(runtime.adapter.history) == before

    resumed = client.post("/api/control/pause", json={"paused": False})
    assert resumed.status_code == 200
    assert resumed.json()["paused"] is False


def test_events_and_exports(client: TestClient) -> None:
    client.post("/api/control/trigger/hola")

    events = client.get("/api/events")
    assert events.status_code == 200
    assert events.json()["events"]

    filtered = client.get("/api/events", params={"gesture": "HOLA", "result": "executed"})
    assert filtered.status_code == 200

    exported_json = client.get("/api/events/export", params={"format": "json"})
    assert exported_json.status_code == 200
    assert exported_json.headers["content-type"].startswith("application/json")
    assert "eventos.json" in exported_json.headers["content-disposition"]
    assert exported_json.json()

    exported_csv = client.get("/api/events/export", params={"format": "csv"})
    assert exported_csv.status_code == 200
    assert exported_csv.headers["content-type"].startswith("text/csv")
    assert "eventos.csv" in exported_csv.headers["content-disposition"]
    assert "behavior_id" in exported_csv.text

    invalid = client.get("/api/events/export", params={"format": "xml"})
    assert invalid.status_code == 400
    assert "error" in invalid.json()


def test_behavior_preview_and_mock_test(client: TestClient) -> None:
    preview = client.get("/api/behaviors/hola/preview")
    assert preview.status_code == 200
    payload = preview.json()
    assert payload["steps"]
    assert payload["warnings"] == ["SIN VALIDAR"]

    tested = client.post("/api/behaviors/hola/test-mock")
    assert tested.status_code == 200
    assert tested.json()["ok"] is True


def test_behavior_editor_validates_and_persists_from_dashboard(client: TestClient) -> None:
    payload = client.get("/api/behaviors").json()
    hola = next(item for item in payload["behaviors"] if item["id"] == "hola")
    hola["description"] = "SIN VALIDAR. Editado desde el dashboard."

    validated = client.post("/api/behaviors/validate", json=payload)
    assert validated.status_code == 200
    assert validated.json()["errors"] == []

    updated = client.put("/api/behaviors", json=payload)
    assert updated.status_code == 200

    stored = client.get("/api/behaviors").json()
    stored_hola = next(item for item in stored["behaviors"] if item["id"] == "hola")
    assert stored_hola["description"] == hola["description"]

    invalid_payload = client.get("/api/behaviors").json()
    invalid_hola = next(item for item in invalid_payload["behaviors"] if item["id"] == "hola")
    invalid_hola["speed_pct"] = 31  # por encima de robot.max_speed_pct (30)

    invalid_validation = client.post("/api/behaviors/validate", json=invalid_payload)
    assert invalid_validation.status_code == 200
    assert invalid_validation.json()["errors"]

    rejected = client.put("/api/behaviors", json=invalid_payload)
    assert rejected.status_code == 400
    assert "error" in rejected.json()

    unchanged = client.get("/api/behaviors").json()
    unchanged_hola = next(item for item in unchanged["behaviors"] if item["id"] == "hola")
    assert unchanged_hola["speed_pct"] == hola["speed_pct"]


def test_websocket_streams_status_and_events(client: TestClient) -> None:
    with client.websocket_connect("/ws?token=test-token") as websocket:
        first = websocket.receive_json()
        second = websocket.receive_json()

    assert first["type"] == "status"
    assert first["data"]["mode"] == "auto"
    assert second["type"] == "events"
    assert isinstance(second["data"], list)


def test_invalid_mode_is_rejected(client: TestClient) -> None:
    response = client.post("/api/control/mode", json={"mode": "inventado"})
    assert response.status_code == 400
    assert "error" in response.json()


def test_config_update_validation_and_rollback(client: TestClient) -> None:
    current = client.get("/api/config").json()
    old_version = current["version"]
    payload = current["config"]
    payload["robot"]["max_speed_pct"] = 40

    updated = client.put("/api/config", json={"config": payload})
    assert updated.status_code == 200
    assert updated.json()["status"]["config_version"] == old_version + 1

    invalid_payload = client.get("/api/config").json()["config"]
    invalid_payload["robot"]["max_speed_pct"] = 999
    invalid = client.put("/api/config", json={"config": invalid_payload})
    assert invalid.status_code == 400
    assert "error" in invalid.json()

    rolled_back = client.post("/api/config/rollback")
    assert rolled_back.status_code == 200
    assert rolled_back.json()["status"]["config_version"] == old_version + 2


def test_replay_status_and_upload_are_controlled(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status = client.get("/api/replay/status")
    assert status.status_code == 200
    assert status.json()["running"] is False

    monkeypatch.chdir(tmp_path)
    uploaded = client.post(
        "/api/replay/upload",
        params={"filename": "x.mp4"},
        content=b"x",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert uploaded.status_code in (200, 400)
    assert uploaded.status_code != 500
    upload_dir = tmp_path / "recordings" / "uploads"
    assert list(upload_dir.glob("*_x.mp4"))


def test_replay_upload_rejects_oversized_file_without_leftovers(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dashboard_server, "_MAX_UPLOAD_BYTES", 10)

    response = client.post(
        "/api/replay/upload",
        params={"filename": "grande.mp4"},
        content=b"x" * 20,
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code in (400, 413)
    upload_dir = tmp_path / "recordings" / "uploads"
    if upload_dir.exists():
        assert not list(upload_dir.glob(".part-*"))
        assert not list(upload_dir.glob("*_grande.mp4"))


def test_replay_upload_rejects_disallowed_extension(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    response = client.post(
        "/api/replay/upload",
        params={"filename": "payload.exe"},
        content=b"x",
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 400
    upload_dir = tmp_path / "recordings" / "uploads"
    if upload_dir.exists():
        assert not list(upload_dir.iterdir())


def test_replay_upload_streaming_limit_cleans_temporary_file(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dashboard_server, "_MAX_UPLOAD_BYTES", 10)

    def chunks():
        yield b"x" * 8
        yield b"x" * 8

    response = client.post(
        "/api/replay/upload",
        params={"filename": "stream.mp4"},
        content=chunks(),
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code in (400, 413)
    upload_dir = tmp_path / "recordings" / "uploads"
    if upload_dir.exists():
        assert not list(upload_dir.glob(".part-*"))
        assert not list(upload_dir.glob("*_stream.mp4"))


def test_put_behaviors_cannot_forge_validated(client: TestClient) -> None:
    payload = client.get("/api/behaviors").json()
    for item in payload["behaviors"]:
        item["validated"] = True

    updated = client.put("/api/behaviors", json=payload)
    assert updated.status_code == 200

    stored = client.get("/api/behaviors").json()
    assert all(item["validated"] is False for item in stored["behaviors"])


def test_replay_upload_rejects_when_total_quota_exceeded(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dashboard_server, "_MAX_UPLOAD_TOTAL_BYTES", 10)

    response = client.post(
        "/api/replay/upload",
        params={"filename": "cuota.mp4"},
        content=b"x" * 20,
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 413
    upload_dir = tmp_path / "recordings" / "uploads"
    if upload_dir.exists():
        assert not list(upload_dir.glob(".part-*"))
        assert not list(upload_dir.glob("*_cuota.mp4"))
