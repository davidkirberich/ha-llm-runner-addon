import base64
import json
import threading
from types import SimpleNamespace

import pytest
from jsonschema.exceptions import ValidationError

import run
import web


@pytest.fixture
def config(storage):
    folder = storage["HA_CONFIG_DIR"] / "www" / "reolink_ftp"
    folder.mkdir(parents=True)
    (folder / "camera one.jpg").write_bytes(b"first image")
    (folder / "camera two.jpeg").write_bytes(b"second image")
    return {
        "prompt": "Analyse {ort}, {datei}",
        "audit": False,
        "inputs": {
            "files": {"image": {"root": "www/reolink_ftp", "extensions": [".jpg", ".jpeg"]}},
            "variables": ["ort", "datei"],
        },
        "validate_response": True,
        "response_schema": {
            "type": "object",
            "properties": {"tier_erkannt": {"type": "boolean"}},
            "required": ["tier_erkannt"],
        },
    }


def payload(filename="camera one.jpg"):
    return {
        "files": {"image": f"/config/www/reolink_ftp/{filename}"},
        "variables": {"ort": "Garage", "datei": filename},
    }


def test_event_paths_resolve_to_ha_not_addon(config, storage):
    result = run.prepare_task_inputs(config, payload())
    assert result.files["image"] == str(storage["HA_CONFIG_DIR"] / "www/reolink_ftp/camera one.jpg")
    assert result.variables == payload()["variables"]
    assert "_call_files" not in config


@pytest.mark.parametrize("data", [
    {}, {"files": {}}, {"model": "other"}, {"variables": []},
    {"files": {"other": "/config/private.jpg"}},
    {"variables": {"history": "bad"}},
    payload("../private.jpg"),
    payload("missing.jpg"),
    {"files": {"image": "https://example.org/image.jpg"}, "variables": payload()["variables"]},
    {"files": payload()["files"], "variables": {"ort": False, "datei": "test"}},
])
def test_invalid_inputs_fail_before_provider(config, monkeypatch, data):
    monkeypatch.setattr(run, "get_provider", lambda *args: pytest.fail("Provider must not be called"))
    with pytest.raises(ValueError):
        run.execute_task("test", config, None, {}, inputs=data)


def test_empty_large_and_unsupported_files(config, storage, monkeypatch):
    file = storage["HA_CONFIG_DIR"] / "www/reolink_ftp/camera one.jpg"
    file.write_bytes(b"")
    with pytest.raises(ValueError, match="non-empty"):
        run.prepare_task_inputs(config, payload())
    file.write_bytes(b"abc")
    monkeypatch.setattr(run, "MAX_FILE_BYTES", 2)
    with pytest.raises(ValueError, match="at most"):
        run.prepare_task_inputs(config, payload())
    other = file.with_suffix(".txt")
    other.write_text("hello")
    with pytest.raises(ValueError, match="extension"):
        run.prepare_task_inputs(config, payload(other.name))


def test_symlink_cannot_escape_allowed_root(config, storage):
    outside = storage["HA_CONFIG_DIR"] / "private.jpg"
    outside.write_bytes(b"private")
    link = storage["HA_CONFIG_DIR"] / "www/reolink_ftp/link.jpg"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks require permissions on this platform")
    with pytest.raises(ValueError, match="outside"):
        run.prepare_task_inputs(config, payload(link.name))


@pytest.mark.parametrize("policy", [
    {"files": {"image": {"root": "../private", "extensions": [".jpg"]}}},
    {"files": {"image": {"root": "/absolute", "extensions": [".jpg"]}}},
    {"files": {"image": {"root": "www", "extensions": []}}},
    {"variables": ["history"]}, {"variables": ["ort", "ort"]},
    {"variables": ["input_files"]}, {"other": True},
])
def test_invalid_input_policy_is_rejected(config, policy):
    config["inputs"] = policy
    with pytest.raises(ValueError):
        run.validate_inputs_config(config)


@pytest.mark.parametrize("answer", [{"tier_erkannt": True}, {"tier_erkannt": False}])
def test_independent_files_and_deterministic_origins(config, monkeypatch, answer):
    calls, published = [], []
    provider = SimpleNamespace(
        resolve_model=lambda *args: "test",
        generate=lambda request: calls.append(request) or {**answer, "ort": "invented", "datei": "invented"},
    )
    monkeypatch.setattr(run, "get_provider", lambda *args: provider)
    monkeypatch.setattr(run, "publish_task_state", lambda client, task, result: published.append(result))
    for filename in ("camera one.jpg", "camera two.jpeg"):
        run.execute_task("test", config, object(), {}, inputs=payload(filename))
    assert [base64.b64decode(request.attachments[0][0]) for request in calls] == [b"first image", b"second image"]
    assert [result["datei"] for result in published] == ["camera one.jpg", "camera two.jpeg"]
    assert all(result["ort"] == "Garage" and result["tier_erkannt"] is answer["tier_erkannt"] for result in published)


@pytest.mark.parametrize("answer", [{}, {"tier_erkannt": "false"}, {"text": "invalid JSON"}])
def test_schema_errors_do_not_publish_or_write_memory(config, monkeypatch, answer):
    provider = SimpleNamespace(resolve_model=lambda *args: "test", generate=lambda request: answer)
    monkeypatch.setattr(run, "get_provider", lambda *args: provider)
    monkeypatch.setattr(run, "publish_task_state", lambda *args: pytest.fail("Invalid result published"))
    monkeypatch.setattr(run, "append_memory", lambda *args, **kwargs: pytest.fail("Invalid result saved"))
    with pytest.raises(ValidationError):
        run.execute_task("test", config, object(), {}, inputs=payload())


def test_mqtt_invocation_and_legacy_run(config, monkeypatch):
    calls = []
    monkeypatch.setattr(run, "load_options", lambda: {})
    monkeypatch.setattr(run, "load_tasks", lambda: {"test": config})
    monkeypatch.setattr(run, "run_task_async", lambda *args: calls.append(args))
    for data in (json.dumps(payload()), "RUN"):
        run.on_message(None, None, SimpleNamespace(topic="ha_llm_runner/run/test", payload=data.encode(), retain=False))
    assert calls[0][-1] == payload()
    assert calls[1][-1] is None
    run.on_message(None, None, SimpleNamespace(topic="ha_llm_runner/run/test", payload=b"{bad", retain=False))
    assert run.TASK_STATUS["test"]["state"] == "error"
    run.on_message(None, None, SimpleNamespace(topic="ha_llm_runner/run/test", payload=json.dumps(payload()).encode(), retain=True))
    assert len(calls) == 2
    assert "Retained" in run.TASK_STATUS["test"]["error"]


def test_async_context_is_copied(config, monkeypatch):
    recorded = []
    class Thread:
        def __init__(self, **kwargs):
            recorded.append(kwargs)
        def start(self):
            pass
    monkeypatch.setattr(threading, "Thread", Thread)
    data = payload()
    run.run_task_async("test", config, None, {}, data)
    data["variables"]["ort"] = "changed"
    config["prompt"] = "changed"
    assert recorded[0]["args"][1]["prompt"] == "Analyse {ort}, {datei}"
    assert recorded[0]["args"][-1]["variables"]["ort"] == "Garage"


def test_preview_and_manual_run_without_required_image(config, monkeypatch):
    monkeypatch.setattr(run, "get_provider", lambda *args: pytest.fail("Provider must not be called"))
    assert run.preview_task("test", config, {})["ok"] is False
    assert run.run_task("test", config, None, {}) is False
    assert run.TASK_STATUS["test"]["state"] == "error"
    with pytest.raises(ValueError, match="Missing required"):
        run.run_task_async("test", config, None, {})


def test_rest_uses_same_invocation_validation(config, monkeypatch):
    app = web.WebApp(run)
    monkeypatch.setattr(app, "task_or_404", lambda task: config)
    with pytest.raises(web.ApiError, match="Missing required"):
        app.run_task("test", body={})
    recorded = []
    monkeypatch.setattr(run, "run_task_async", lambda *args: recorded.append(args))
    monkeypatch.setattr(run, "load_options", lambda: {})
    assert app.run_task("test", body=payload())[0] == 202
    assert recorded[0][-1] == payload()
