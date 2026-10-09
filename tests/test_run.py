import json

import pandas as pd
import pytest

import llm_providers
import run as runner
from llm_providers import LLMProvider


def test_get_ha_headers_uses_bearer_token(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "token-123")
    monkeypatch.delenv("HA_TOKEN", raising=False)

    headers = runner.get_ha_headers()

    assert headers["Content-Type"] == "application/json"
    assert headers["Authorization"] == "Bearer token-123"


def test_fetch_ha_state_uses_entity_attribute_when_present(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "state": "unknown",
                "attributes": {"text_summary": "Everything looks good"},
            }

    def fake_get(url, headers=None, timeout=10):
        return FakeResponse()

    monkeypatch.setattr(runner.requests, "get", fake_get)

    value = runner.fetch_ha_state("sensor.temperature:text_summary")

    assert value == "Everything looks good"


def test_process_default_returns_current_and_timeseries():
    df = pd.DataFrame(
        {
            "sensor_a": [10.0, 12.0, 11.5],
            "sensor_b": [100.0, 104.0, 102.0],
        },
        index=pd.date_range("2024-01-01 00:00:00", periods=3, freq="h"),
    )

    current, timeseries_json = runner.process_default(df, resample_rule="1h")

    assert abs(current["sensor_a"] - 11.5) < 0.001
    assert abs(current["sensor_b"] - 102.0) < 0.001
    payload = json.loads(timeseries_json)
    assert isinstance(payload, list)
    assert len(payload) >= 1
    assert "sensor_a" in payload[0]


def test_resolve_config_path_finds_relative_processor_file(tmp_path, monkeypatch):
    processors_dir = tmp_path / "scripts" / "processors"
    processors_dir.mkdir(parents=True)
    target = processors_dir / "solar_lade_forecast.py"
    target.write_text("def process(df, config):\n    return {}, 'ok'\n", encoding="utf-8")

    monkeypatch.setattr(runner, "PROCESSORS_DIR", str(processors_dir))
    monkeypatch.setattr(runner, "TASKS_CONFIG_PATH", str(tmp_path / "llm_tasks.yaml"))

    assert runner.resolve_config_path("solar_lade_forecast.py") == str(target)


def test_write_ha_target_state_persists_history_and_posts_attributes(monkeypatch, tmp_path):
    task_cfg = {"target_sensor": "sensor.llm_result", "friendly_name": "LLM Result"}
    history_path = tmp_path / "llm_tasks.yaml"
    monkeypatch.setattr(runner, "TASKS_CONFIG_PATH", str(history_path))
    monkeypatch.setattr(runner, "HA_URL", "http://example.invalid")

    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

    def fake_post(url, headers=None, json=None, timeout=10):
        captured["url"] = url
        captured["payload"] = json
        return FakeResponse()

    monkeypatch.setattr(runner.requests, "post", fake_post)

    runner.write_ha_target_state(task_cfg, {"summary": "ok"})

    history_file = tmp_path / "sensor_llm_result_history.json"
    assert history_file.exists()
    history = json.loads(history_file.read_text(encoding="utf-8"))
    assert history[0]["text"] == "ok"
    assert captured["url"] == "http://example.invalid/api/states/sensor.llm_result"
    assert captured["payload"]["attributes"]["text"] == "ok"


class RecordingProvider(LLMProvider):
    name = "recording"
    default_model = "recording-default"
    requests = []
    answer = '{"state": "ok", "summary": "All good"}'

    def generate_text(self, request):
        RecordingProvider.requests.append(request)
        return self.answer


@pytest.fixture
def task_env(monkeypatch, tmp_path):
    RecordingProvider.requests = []
    monkeypatch.setitem(llm_providers.PROVIDERS, "recording", RecordingProvider)
    monkeypatch.setattr(runner, "TASKS_CONFIG_PATH", str(tmp_path / "llm_tasks.yaml"))
    monkeypatch.setattr(runner, "fetch_ha_state", lambda spec: "21.5")
    monkeypatch.setattr(runner, "fetch_ha_data", lambda entities, hours: [])
    monkeypatch.setattr(runner, "fetch_camera_snapshot", lambda target: ("aW1n", "image/jpeg"))
    monkeypatch.setattr(runner, "create_audit_archive", lambda *args, **kwargs: None)

    published = {}
    monkeypatch.setattr(runner, "publish_task_state", lambda client, task_id, data: published.update({task_id: data}))
    written = {}
    monkeypatch.setattr(runner, "write_ha_target_state", lambda cfg, data, options=None: written.update(data))
    return published, written


def test_execute_task_routes_prompt_images_and_temperature_through_provider(task_env):
    published, written = task_env
    task_cfg = {
        "provider": "recording",
        "model": "test-model",
        "temperature": 0.7,
        "prompt": "Temperature is {temp}. Look at the camera.",
        "response_schema": {"type": "object"},
        "entities": {"temp": "sensor.outdoor", "cam": "camera.front_door"},
        "target_sensor": "sensor.llm_result",
    }

    runner.execute_task("demo", task_cfg, client=None, options={"recording_api_key": "k"})

    request = RecordingProvider.requests[0]
    assert request.model == "test-model"
    assert request.temperature == 0.7
    assert request.schema == {"type": "object"}
    assert request.attachments == [("aW1n", "image/jpeg")]
    assert request.prompt.startswith("Temperature is 21.5. Look at the camera.")
    assert "Attached image source(s): cam" in request.prompt
    assert published["demo"]["state"] == "ok"
    assert written["summary"] == "All good"


def test_execute_task_uses_global_model_and_gemini_by_default(task_env, monkeypatch):
    calls = []

    def fake_generate(self, request):
        calls.append((type(self).__name__, request.model))
        return {"summary": "done"}

    monkeypatch.setattr(llm_providers.GeminiProvider, "generate", fake_generate)

    runner.execute_task("demo", {"prompt": "Hi"}, client=None,
                        options={"gemini_api_key": "k", "gemini_model": "gemini-global"})

    assert calls == [("GeminiProvider", "gemini-global")]


def test_execute_task_does_not_publish_when_llm_call_fails(task_env, monkeypatch):
    published, written = task_env

    def boom(self, request):
        raise RuntimeError("quota exceeded")

    monkeypatch.setattr(RecordingProvider, "generate_text", boom)

    with pytest.raises(RuntimeError, match="quota exceeded"):
        runner.execute_task("demo", {"provider": "recording", "prompt": "Hi"}, client=None,
                            options={"recording_api_key": "k"})

    assert published == {}
    assert written == {}

def test_split_url_credentials_moves_userinfo_into_digest_auth():
    url, auth = runner.split_url_credentials("http://admin:p%40ss@192.168.1.5:8080/snap.jpg")

    assert url == "http://192.168.1.5:8080/snap.jpg"
    assert (auth.username, auth.password) == ("admin", "p@ss")
    assert runner.split_url_credentials("https://example.com/a.mp3") == ("https://example.com/a.mp3", None)


def test_fetch_binary_file_reads_local_file_relative_to_config(monkeypatch, tmp_path):
    (tmp_path / "notes.csv").write_bytes(b"a,b\n1,2\n")
    monkeypatch.setattr(runner, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(runner.requests, "get", lambda *a, **k: pytest.fail("local files must not use HTTP"))

    b64_data, mime_type = runner.fetch_binary_file("notes.csv")

    assert runner.base64.b64decode(b64_data) == b"a,b\n1,2\n"
    assert mime_type == "text/csv"


def test_fetch_binary_file_rejects_missing_local_file_instead_of_camera_proxy(monkeypatch):
    monkeypatch.setattr(runner.requests, "get", lambda *a, **k: pytest.fail("must not call camera_proxy"))

    with pytest.raises(FileNotFoundError):
        runner.fetch_binary_file("camera.front_door")


class FakeResponse:
    def __init__(self, content, content_type):
        self.content = content
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield self.content


@pytest.mark.parametrize("url, content_type, expected", [
    ("http://nas/song.mp3?dl=1", "application/octet-stream", "audio/mpeg"),
    ("http://nas/clip.mov", "", "video/quicktime"),
    ("http://nas/download", "application/pdf; charset=binary", "application/pdf"),
    ("http://nas/download", "application/octet-stream", "application/octet-stream"),
])
def test_fetch_binary_file_guesses_mime_type_when_server_is_generic(monkeypatch, url, content_type, expected):
    seen = {}

    def fake_get(url, **kwargs):
        seen["auth"] = kwargs["auth"]
        return FakeResponse(b"x", content_type)

    monkeypatch.setattr(runner.requests, "get", fake_get)

    _, mime_type = runner.fetch_binary_file(url)

    assert mime_type == expected
    assert seen["auth"] is None


def test_fetch_binary_file_enforces_size_limit(monkeypatch):
    monkeypatch.setattr(runner, "MAX_FILE_BYTES", 3)
    monkeypatch.setattr(runner.requests, "get", lambda url, **k: FakeResponse(b"12345", "audio/mpeg"))

    with pytest.raises(ValueError, match="exceeds"):
        runner.fetch_binary_file("http://nas/song.mp3")


def test_execute_task_announces_files_separately_from_images(task_env, monkeypatch):
    monkeypatch.setattr(runner, "fetch_binary_file", lambda target: ("bXAz", "audio/mpeg"))
    task_cfg = {
        "provider": "recording",
        "prompt": "Describe everything.",
        "entities": {"cam": "camera.front_door"},
        "files": {"doorbell": "http://nas/ring.mp3"},
    }

    runner.execute_task("demo", task_cfg, client=None, options={"recording_api_key": "k"})

    request = RecordingProvider.requests[0]
    assert request.attachments == [("aW1n", "image/jpeg"), ("bXAz", "audio/mpeg")]
    assert "Attached image source(s): cam." in request.prompt
    assert "Attached file(s): doorbell (audio/mpeg)." in request.prompt


@pytest.mark.parametrize("options, task_extra, expected", [
    ({}, {}, True),
    ({"audit_archive": True}, {}, True),
    ({"audit_archive": False}, {}, False),
    ({"audit_archive": True}, {"audit": False}, False),
    ({"audit_archive": False}, {"audit": True}, False),
])
def test_audit_archive_follows_global_option_and_task_opt_out(task_env, monkeypatch, options, task_extra, expected):
    archived = []
    monkeypatch.setattr(runner, "create_audit_archive", lambda task_id, *args: archived.append(task_id))
    task_cfg = {"provider": "recording", "prompt": "Hi", **task_extra}

    runner.execute_task("demo", task_cfg, client=None, options={"recording_api_key": "k", **options})

    assert archived == (["demo"] if expected else [])


def test_local_timezone_prefers_option_then_tz_env_then_home_assistant(monkeypatch):
    assert runner.get_local_timezone({"timezone": "America/New_York"}).key == "America/New_York"

    monkeypatch.setenv("TZ", "Europe/Berlin")
    assert runner.get_local_timezone({}).key == "Europe/Berlin"

    monkeypatch.delenv("TZ")
    monkeypatch.setattr(runner, "_ha_time_zone", "Asia/Tokyo")
    assert runner.get_local_timezone({}).key == "Asia/Tokyo"

    monkeypatch.setattr(runner, "_ha_time_zone", "")
    assert runner.get_local_timezone({}) == runner.timezone.utc


def test_local_timezone_skips_invalid_names(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")

    assert runner.get_local_timezone({"timezone": "Mars/Olympus"}).key == "Europe/Berlin"


def test_fetch_ha_time_zone_reads_and_caches_api_config(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"time_zone": "Europe/Vienna"}

    monkeypatch.setattr(runner, "_ha_time_zone", None)
    monkeypatch.setattr(runner.requests, "get", lambda url, **k: calls.append(url) or FakeResponse())

    assert runner.fetch_ha_time_zone() == "Europe/Vienna"
    assert runner.fetch_ha_time_zone() == "Europe/Vienna"
    assert calls == [f"{runner.HA_URL}/api/config"]


def test_format_datetime_uses_option_or_legacy_default():
    value = runner.datetime(2026, 10, 9, 16, 24, 10)

    assert runner.format_datetime(value, {}) == "09.10.2026 16:24:10"
    assert runner.format_datetime(value, {"datetime_format": "%Y-%m-%d %H:%M"}) == "2026-10-09 16:24"


def test_build_raw_dataframe_converts_to_configured_timezone():
    raw = [[{"entity_id": "sensor.t", "state": "20", "last_changed": "2026-01-01T12:00:00+00:00"}]]

    df = runner.build_raw_dataframe(raw, {"t": "sensor.t"}, runner.ZoneInfo("Europe/Berlin"))

    assert str(df.index.tz) == "Europe/Berlin"
    assert df.index[0].hour == 13


def test_execute_task_uses_configured_timezone_and_format(task_env, monkeypatch):
    published, _ = task_env
    task_cfg = {"provider": "recording", "prompt": "It is {now} on {today}."}
    options = {"recording_api_key": "k", "timezone": "Asia/Tokyo", "datetime_format": "%Y|%H"}

    runner.execute_task("demo", task_cfg, client=None, options=options)

    tokyo_now = runner.datetime.now(runner.ZoneInfo("Asia/Tokyo"))
    prompt = RecordingProvider.requests[0].prompt
    assert prompt.startswith(f"It is {tokyo_now:%Y}|")
    assert ". " not in prompt.split(" on ")[1][:3]
    assert published["demo"]["updated_at"].endswith("+09:00")


def test_execute_task_sends_history_via_timeseries_placeholder(task_env, monkeypatch):
    requested = {}

    def fake_history(entities, hours):
        requested.update(entities=entities, hours=hours)
        return [[
            {"entity_id": "sensor.living_room", "state": "20.0", "last_changed": "2026-01-01T10:00:00+00:00"},
            {"entity_id": "sensor.living_room", "state": "22.0", "last_changed": "2026-01-01T12:00:00+00:00"},
        ]]

    monkeypatch.setattr(runner, "fetch_ha_data", fake_history)
    task_cfg = {
        "provider": "recording",
        "entities": {"living_room": "sensor.living_room"},
        "hours": 48,
        "resample": "2h",
        "prompt": "Now: {now}. Current: {living_room}.\nHistory:\n{timeseries}",
    }

    runner.execute_task("demo", task_cfg, client=None, options={"recording_api_key": "k"})

    assert requested == {"entities": ["sensor.living_room"], "hours": 48}
    history = json.loads(RecordingProvider.requests[0].prompt.split("History:\n", 1)[1])
    assert [row["living_room"] for row in history] == [20.0, 22.0]
    assert history[0]["timestamp"] == "2026-01-01T10:00:00+00:00"


def test_timeseries_placeholder_reports_when_history_is_disabled(task_env):
    task_cfg = {"provider": "recording", "entities": {"t": "sensor.t"}, "hours": 0, "prompt": "{timeseries}"}

    runner.execute_task("demo", task_cfg, client=None, options={"recording_api_key": "k"})

    assert RecordingProvider.requests[0].prompt == "No history requested."


def test_process_default_keeps_local_time_offset_in_json():
    idx = pd.date_range("2026-10-07 10:00", periods=2, freq="h", tz="UTC").tz_convert("Europe/Berlin")
    df = pd.DataFrame({"t": [1.0, 2.0]}, index=idx)

    _, timeseries_json = runner.process_default(df, "1h")

    assert json.loads(timeseries_json)[0]["timestamp"] == "2026-10-07T12:00:00+02:00"


@pytest.mark.parametrize("key", ["data_processor", "processor"])
def test_execute_task_runs_processor_once_from_processors_dir(task_env, monkeypatch, tmp_path, key):
    processors_dir = tmp_path / "scripts" / "processors"
    processors_dir.mkdir(parents=True)
    (processors_dir / "solar.py").write_text(
        "CALLS = []\n"
        "def process(df, config):\n"
        "    CALLS.append(1)\n"
        "    return {'forecast_kwh': 12.5}, [{'hour': 12, 'kwh': 3.1}]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "PROCESSORS_DIR", str(processors_dir))
    calls = []
    real_run = runner.run_processor
    monkeypatch.setattr(runner, "run_processor", lambda *a: calls.append(a[0]) or real_run(*a))
    task_cfg = {
        "provider": "recording",
        key: "processors/solar.py",
        "entities": {"pv": "sensor.pv"},
        "prompt": "Forecast {forecast_kwh} kWh. Data: {data}. Series: {timeseries}",
    }

    runner.execute_task("demo", task_cfg, client=None, options={"recording_api_key": "k"})

    assert calls == ["processors/solar.py"]
    prompt = RecordingProvider.requests[0].prompt
    assert prompt == 'Forecast 12.5 kWh. Data: [{"hour": 12, "kwh": 3.1}]. Series: [{"hour": 12, "kwh": 3.1}]'


def test_execute_task_falls_back_to_default_aggregation_when_processor_missing(task_env, monkeypatch):
    monkeypatch.setattr(runner, "fetch_ha_data", lambda entities, hours: [[
        {"entity_id": "sensor.pv", "state": "4.0", "last_changed": "2026-01-01T10:00:00+00:00"},
    ]])
    task_cfg = {"provider": "recording", "data_processor": "missing.py", "entities": {"pv": "sensor.pv"}, "prompt": "{timeseries}"}

    runner.execute_task("demo", task_cfg, client=None, options={"recording_api_key": "k"})

    assert json.loads(RecordingProvider.requests[0].prompt)[0]["pv"] == 4.0
