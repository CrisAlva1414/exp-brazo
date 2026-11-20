"""Regresiones del gate final: seguridad, límites y cámara degradada.

Estas pruebas no tocan ``config/`` real: trabajan sobre copias en ``tmp_path``
y no requieren cámara, robot ni MediaPipe reales.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from exp_brazo.dashboard.server import create_app
from exp_brazo.robot.mock import MockAdapter
from exp_brazo.runtime import Runtime

TOKEN = "gate-token"
AUTH = {"X-ExpBrazo-Token": TOKEN}


def _copy_config(tmp_path: Path, repo_root: Path, **camera_overrides) -> tuple[Path, Path]:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    behaviors_path = config_dir / "behaviors.yaml"
    config = yaml.safe_load((repo_root / "config" / "config.yaml").read_text(encoding="utf-8"))
    config["camera"].update(camera_overrides)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    shutil.copy(repo_root / "config" / "behaviors.yaml", behaviors_path)
    return config_path, behaviors_path


def _runtime(tmp_path: Path, repo_root: Path, **kwargs) -> Runtime:
    config_path, behaviors_path = _copy_config(tmp_path, repo_root)
    return Runtime(
        config_path=config_path,
        behaviors_path=behaviors_path,
        force_adapter="mock",
        camera_enabled=False,
        execute_inline=True,
        adapter_factory=lambda robot_config: MockAdapter(robot_config, time_scale=0),
        **kwargs,
    )


@pytest.fixture
def runtime(tmp_path: Path, repo_root: Path):
    instance = _runtime(tmp_path, repo_root)
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


@pytest.mark.parametrize(
    "path",
    [
        "/api/status",
        "/api/events",
        "/api/events/export",
        "/api/config",
        "/api/behaviors",
        "/api/behaviors/hola/preview",
        "/api/troubleshooting",
        "/api/replay/status",
        "/api/snapshot.jpg",
        "/video.mjpeg",
    ],
)
def test_non_public_get_endpoints_require_token(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 401


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/control/trigger/hola"),
        ("POST", "/api/control/home"),
        ("POST", "/api/control/pause"),
        ("POST", "/api/control/mode"),
        ("POST", "/api/config/rollback"),
        ("PUT", "/api/config"),
        ("PUT", "/api/behaviors"),
        ("POST", "/api/behaviors/validate"),
        ("POST", "/api/behaviors/hola/test-mock"),
        ("POST", "/api/troubleshooting/run"),
        ("POST", "/api/replay/start"),
        ("POST", "/api/replay/upload?filename=x.mp4"),
        ("POST", "/api/record/start"),
        ("POST", "/api/record/stop"),
    ],
)
def test_non_public_write_endpoints_require_token(
    client: TestClient, method: str, path: str
) -> None:
    assert client.request(method, path).status_code == 401


def test_public_endpoints_work_without_token(client: TestClient) -> None:
    assert client.get("/").status_code == 200
    assert client.get("/api/health").json() == {"ok": True}
    assert client.get("/favicon.ico").status_code == 204
    assert client.get("/static/app.js").status_code == 200


def test_config_api_only_shrinks_envelope_and_validates_home(client: TestClient) -> None:
    current = client.get("/api/config", headers=AUTH).json()

    expanded = current["config"]
    expanded["robot"]["joint_limits"]["J1"] = [-999, 999]
    response = client.put("/api/config", json={"config": expanded}, headers=AUTH)
    assert response.status_code == 400
    assert "envelope" in response.json()["error"]

    bad_home = client.get("/api/config", headers=AUTH).json()["config"]
    bad_home["robot"]["home_joints"][0] = 999
    response = client.put("/api/config", json={"config": bad_home}, headers=AUTH)
    assert response.status_code == 400
    assert "home_joints" in response.json()["error"]

    shrunk = client.get("/api/config", headers=AUTH).json()["config"]
    shrunk["robot"]["joint_limits"]["J1"] = [-90, 90]
    response = client.put("/api/config", json={"config": shrunk}, headers=AUTH)
    assert response.status_code == 200
    version = response.json()["status"]["config_version"]
    assert client.get("/api/config", headers=AUTH).json()["version"] == version


def test_upload_filename_traversal_stays_inside_recordings(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    response = client.post(
        "/api/replay/upload",
        params={"filename": "../../evil.mp4"},
        content=b"x",
        headers={**AUTH, "Content-Type": "application/octet-stream"},
    )

    assert response.status_code != 500
    uploads = tmp_path / "recordings" / "uploads"
    assert list(uploads.glob("*_evil.mp4"))
    assert not (tmp_path / "evil.mp4").exists()
    assert not (tmp_path.parent / "evil.mp4").exists()


def test_camera_down_keeps_dashboard_and_diagnostics_operational(
    tmp_path: Path, repo_root: Path
) -> None:
    class DownPipeline:
        def __init__(self) -> None:
            self.running = False

        def start(self) -> None:
            self.running = True

        def stop(self) -> None:
            self.running = False

        def restart(self) -> None:
            self.running = True

        def update_config(self, *_args) -> None:
            return None

        def status(self) -> dict:
            return {
                "camera_ok": False,
                "running": self.running,
                "fps": 0.0,
                "process_fps": 0.0,
                "latency_ms": 0.0,
                "frames": 0,
                "last_error": "cámara no disponible",
                "heartbeat_age_s": 0.0,
                "gestures": {},
            }

    config_path, behaviors_path = _copy_config(tmp_path, repo_root, index=99)
    instance = Runtime(
        config_path=config_path,
        behaviors_path=behaviors_path,
        force_adapter="mock",
        camera_enabled=True,
        execute_inline=True,
        adapter_factory=lambda robot_config: MockAdapter(robot_config, time_scale=0),
        pipeline_factory=lambda *_args: DownPipeline(),
    )
    instance.start()
    try:
        app = create_app(
            instance,
            token=TOKEN,
            allowed_hosts=["testserver"],
            allowed_origins=["http://testserver"],
        )
        with TestClient(app) as test_client:
            assert test_client.get("/api/health").status_code == 200
            status = test_client.get("/api/status", headers=AUTH).json()
            assert status["perception"]["camera_ok"] is False
            assert status["layers"]["camera"]["ok"] is False

            started = time.perf_counter()
            response = test_client.get("/api/troubleshooting", headers=AUTH)
            elapsed = time.perf_counter() - started
            assert response.status_code == 200
            assert elapsed < 6.0
            ids = {item["id"] for item in response.json()["results"]}
            assert "camera" in ids
    finally:
        instance.stop()
