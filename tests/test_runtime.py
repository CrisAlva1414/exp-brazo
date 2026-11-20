"""Pruebas de integración del runtime con adaptador mock."""

from __future__ import annotations

import csv
import io
import json
import shutil
from pathlib import Path

import pytest
import yaml

from exp_brazo.contracts import JOINT_NAMES, RobotState
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
    try:
        yield instance
    finally:
        instance.stop()


def test_start_and_stop(runtime: Runtime) -> None:
    status = runtime.start()

    assert status["mode"] == "auto"
    assert runtime.adapter.state == RobotState.IDLE
    assert status["layers"]["supervisor"]["ok"] is True

    runtime.stop()

    assert runtime.adapter.state == RobotState.DISCONNECTED


def test_trigger_executes_and_home_returns(runtime: Runtime) -> None:
    runtime.start()
    before = len(runtime.adapter.history)

    record = runtime.trigger("hola")

    assert record["result"] == "executed"
    assert len(runtime.adapter.history) == before + 1
    assert runtime.adapter.history[-1]["behavior_id"] == "hola"

    home_record = runtime.home()

    assert home_record["result"] == "executed"
    assert runtime.adapter.history[-1]["behavior_id"] == "__home__"


def test_paused_trigger_is_not_executed(runtime: Runtime) -> None:
    runtime.start()
    runtime.set_paused(True)
    before = len(runtime.adapter.history)

    record = runtime.trigger("hola")

    assert record["result"] == "would_have_done"
    assert len(runtime.adapter.history) == before


def test_update_config_versions_persists_and_rollback(
    runtime: Runtime, tmp_path: Path
) -> None:
    old_version = runtime.config.version
    payload = runtime.get_config_dict()
    payload["robot"]["max_speed_pct"] = 40

    status = runtime.update_config(payload)

    assert status["config_version"] == old_version + 1
    assert runtime.config.robot.max_speed_pct == 40

    on_disk = yaml.safe_load(runtime.config_path.read_text(encoding="utf-8"))
    assert on_disk["version"] == old_version + 1
    assert on_disk["robot"]["max_speed_pct"] == 40

    invalid = runtime.get_config_dict()
    invalid["robot"]["max_speed_pct"] = 999
    with pytest.raises(ValueError):
        runtime.update_config(invalid)

    assert runtime.config.robot.max_speed_pct == 40
    still_on_disk = yaml.safe_load(runtime.config_path.read_text(encoding="utf-8"))
    assert still_on_disk["version"] == old_version + 1

    restored_status = runtime.rollback_config()

    assert restored_status["config_version"] == old_version + 2
    assert runtime.config.robot.max_speed_pct == 30


def test_update_behaviors_rejects_out_of_limit_delta(runtime: Runtime) -> None:
    payload = runtime.behaviors_dict()
    hola = next(item for item in payload["behaviors"] if item["id"] == "hola")
    hola["steps"][0]["deltas"]["J1"] = 1000.0

    with pytest.raises(ValueError):
        runtime.update_behaviors(payload)

    stored = runtime.behavior_store.get("hola")
    assert stored is not None
    assert stored.steps[0].deltas["J1"] == 8.0


def test_validate_behaviors_returns_errors_without_saving(runtime: Runtime) -> None:
    payload = runtime.behaviors_dict()
    hola = next(item for item in payload["behaviors"] if item["id"] == "hola")
    hola["steps"][0]["deltas"]["J1"] = 1000.0

    errors = runtime.validate_behaviors(payload)

    assert errors
    assert runtime.behavior_store.get("hola").steps[0].deltas["J1"] == 8.0


def test_preview_behavior_builds_timeline(runtime: Runtime) -> None:
    preview = runtime.preview_behavior("hola")

    assert preview["behavior_id"] == "hola"
    assert preview["validated"] is False
    assert preview["warnings"] == ["SIN VALIDAR"]
    assert len(preview["steps"]) == 5
    assert preview["steps"][0]["t_start_ms"] == 0
    assert preview["steps"][1]["t_start_ms"] == preview["steps"][0]["duration_ms"] + preview[
        "steps"
    ][0]["pause_ms"]
    assert preview["final_joints"] == list(runtime.config.robot.home_joints)
    assert preview["total_ms"] > 0


@pytest.mark.parametrize("behavior_id", ["hola", "si", "no"])
def test_preview_minimum_gestures_stays_within_joint_limits(
    runtime: Runtime, behavior_id: str
) -> None:
    preview = runtime.preview_behavior(behavior_id)

    assert preview["behavior_id"] == behavior_id
    assert preview["validated"] is False
    assert preview["warnings"] == ["SIN VALIDAR"]
    assert preview["steps"]
    assert preview["total_ms"] > 0

    for step in preview["steps"]:
        for joint in JOINT_NAMES:
            low, high = runtime.config.robot.joint_limits[joint]
            assert low <= step["target_joints"][joint] <= high


def test_test_behavior_mock_executes_only_that_behavior(runtime: Runtime) -> None:
    result = runtime.test_behavior_mock("si")

    assert result["ok"] is True
    assert result["result"]["behavior_id"] == "si"
    assert result["result"]["steps_executed"] == 5
    assert result["preview"]["behavior_id"] == "si"


def test_events_and_exports(runtime: Runtime) -> None:
    runtime.start()
    runtime.trigger("hola")
    runtime.home()

    events = runtime.events()
    assert events
    assert runtime.events(gesture="HOLA")
    assert runtime.events(result="executed")

    parsed = json.loads(runtime.export_events("json"))
    assert isinstance(parsed, list)
    assert parsed

    csv_text = runtime.export_events("csv")
    first_line = csv_text.splitlines()[0]
    assert "behavior_id" in first_line
    assert "hola" in csv_text


def test_export_csv_escapes_formula_cells(runtime: Runtime) -> None:
    runtime.controller.logs.append(
        {
            "ts": 0.0,
            "event_id": "formula",
            "gesture": "HOLA",
            "confidence": 0.9,
            "decision": "ok",
            "behavior_id": "hola",
            "result": "executed",
            "error": "=cmd|' /C calc'!A0",
            "mode": "auto",
            "duration_ms": 1.0,
        }
    )

    csv_text = runtime.export_events("csv")

    assert "'=cmd" in csv_text
    for row in csv.reader(io.StringIO(csv_text)):
        for cell in row:
            assert not cell.startswith("=")


def test_replay_path_is_confined(repo_root: Path, monkeypatch) -> None:
    monkeypatch.chdir(repo_root)

    with pytest.raises(ValueError):
        Runtime._validate_replay_path("/etc/passwd")
    with pytest.raises(ValueError):
        Runtime._validate_replay_path("recordings/../../etc/passwd")

    recordings = repo_root / "recordings"
    txt = recordings / "tmp_test.txt"
    mp4 = recordings / "tmp_test.mp4"
    try:
        txt.write_text("no es video", encoding="utf-8")
        with pytest.raises(ValueError):
            Runtime._validate_replay_path(txt)

        mp4.write_bytes(b"")
        resolved = Runtime._validate_replay_path(mp4)
        assert resolved == mp4.resolve()
        assert resolved.is_relative_to(recordings.resolve())
    finally:
        txt.unlink(missing_ok=True)
        mp4.unlink(missing_ok=True)


def test_config_save_sets_0600(runtime: Runtime) -> None:
    payload = runtime.get_config_dict()
    payload["robot"]["max_speed_pct"] = 40

    runtime.update_config(payload)

    assert runtime.config_path.stat().st_mode & 0o777 == 0o600
    version_files = runtime.config_store.version_files()
    assert version_files
    assert version_files[-1].stat().st_mode & 0o777 == 0o600


def test_ambient_trigger_bypasses_disabled(runtime: Runtime) -> None:
    assert runtime.behavior_store.get("ambiental").enabled is False
    runtime.adapter.connect()
    before = len(runtime.adapter.history)

    runtime._trigger_ambient()

    assert len(runtime.adapter.history) == before + 1
    assert runtime.adapter.history[-1]["behavior_id"] == "ambiental"


def test_troubleshooting_responds_and_report_has_header(runtime: Runtime) -> None:
    report = runtime.troubleshooting()

    assert isinstance(report["results"], list)
    ids = {item["id"] for item in report["results"]}
    assert {
        "camera",
        "light_framing",
        "controller_ping",
        "controller_ports",
        "robot_state",
        "mediapipe_models",
        "performance",
        "config",
    } <= ids
    assert "REPORTE" in report["report"]


def test_replay_status_starts_idle(runtime: Runtime) -> None:
    status = runtime.replay_status()

    assert status["running"] is False
    assert status["frames"] == 0
    assert status["events"] == []
    assert status["started_at"] is None


def test_dobot_adapter_is_built_without_connecting(tmp_path: Path, repo_root: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    behaviors_path = config_dir / "behaviors.yaml"
    shutil.copy(repo_root / "config" / "config.yaml", config_path)
    shutil.copy(repo_root / "config" / "behaviors.yaml", behaviors_path)

    instance = Runtime(
        config_path=config_path,
        behaviors_path=behaviors_path,
        force_adapter="dobot",
        camera_enabled=False,
        execute_inline=True,
    )
    try:
        assert instance.adapter.name == "dobot"
        assert instance.adapter.state == RobotState.DISCONNECTED
    finally:
        instance.stop()


class _RunningRecorder:
    def status(self) -> dict:
        return {"running": True}

    def stop(self) -> None:
        raise AssertionError("no se debe detener una grabación activa al rechazarla")


class _FakeReplayEvent:
    def model_dump(self, mode: str = "json") -> dict:
        return {"event_id": "fake"}


def test_replay_start_rejects_second_active_replay(
    runtime: Runtime, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    source = recordings / "ok.mp4"
    source.write_bytes(b"")

    runtime.replay = {
        "running": True,
        "frames": 0,
        "events": [],
        "error": None,
        "started_at": None,
    }

    with pytest.raises(ValueError, match="reproducción en curso"):
        runtime.replay_start(source)

    assert runtime.replay["running"] is True


def test_record_start_rejects_second_active_recording(runtime: Runtime) -> None:
    runtime.recorder = _RunningRecorder()

    with pytest.raises(ValueError, match="grabación en curso"):
        runtime.record_start(1)


def test_record_stop_without_recorder_returns_controlled_error(runtime: Runtime) -> None:
    runtime.recorder = None

    result = runtime.record_stop()

    assert result["ok"] is False
    assert "no hay grabación en curso" in result["error"]


def test_replay_events_are_capped(runtime: Runtime) -> None:
    runtime.replay = {
        "running": True,
        "frames": 0,
        "events": [{"i": index} for index in range(5000)],
        "error": None,
        "started_at": None,
    }

    runtime._on_replay_event(_FakeReplayEvent())

    assert len(runtime.replay["events"]) == 5000
    assert runtime.replay["events"][-1] == {"event_id": "fake"}
