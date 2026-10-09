import json

import pandas as pd
import pytest
import requests

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


def test_write_ha_target_state_posts_text_and_memory(monkeypatch):
    task_cfg = {"target_sensor": "sensor.llm_result", "friendly_name": "LLM Result"}
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
    memory = [{"time": "now", "text": "ok"}]

    runner.write_ha_target_state(task_cfg, {"summary": "ok"}, {}, memory)

    assert captured["url"] == "http://example.invalid/api/states/sensor.llm_result"
    assert captured["payload"]["attributes"]["text"] == "ok"
    assert captured["payload"]["attributes"]["history"] == memory


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
    monkeypatch.setattr(runner, "_mqtt_client", object())
    monkeypatch.setattr(runner, "publish_task_state", lambda client, task_id, data: published.update({task_id: data}))
    written = {}
    monkeypatch.setattr(runner, "write_ha_target_state", lambda cfg, data, options=None, memory=None: written.update(data))
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


def test_preview_runs_the_llm_but_saves_and_publishes_nothing(task_env, monkeypatch):
    published, written = task_env
    audits = []
    monkeypatch.setattr(runner, "create_audit_archive", lambda *args, **kwargs: audits.append(args))
    runner.append_memory("demo", "Yesterday", {})
    task_cfg = {"provider": "recording", "prompt": "Temp {temp}, before: {history}", "entities": {"temp": "sensor.outdoor"},
                "target_sensor": "sensor.llm_result", "audit": True}

    outcome = runner.preview_task("demo", task_cfg, {"recording_api_key": "k"})

    assert outcome["ok"] is True
    assert outcome["prompt"].startswith("Temp 21.5, before: Yesterday")
    assert outcome["result"]["state"] == "ok" and outcome["result"]["summary"] == "All good"
    assert isinstance(outcome["duration"], float)
    assert published == {} and written == {} and audits == []
    assert [entry["text"] for entry in runner.load_memory("demo")] == ["Yesterday"]
    assert "demo" not in runner.TASK_STATUS


def test_data_only_task_does_not_download_images_or_files(task_env, monkeypatch):
    def unexpected(target):
        raise AssertionError(f"downloaded {target}")
    monkeypatch.setattr(runner, "fetch_camera_snapshot", unexpected)
    monkeypatch.setattr(runner, "fetch_binary_file", unexpected)
    task_cfg = {"entities": {"temp": "sensor.outdoor", "cam": "camera.front_door"}, "files": {"webcam": "http://cam/x.jpg"}}

    outcome = runner.preview_task("demo", task_cfg, {})

    assert outcome["ok"] is True and outcome["prompt"] == "" and outcome["attachments"] == []
    assert outcome["result"]["temp"] == "21.5" and RecordingProvider.requests == []

def test_preview_returns_errors_with_traceback(task_env, monkeypatch):
    def broken(spec):
        raise ConnectionError("http://user:secret@cam.local/snap failed")
    monkeypatch.setattr(runner, "fetch_ha_state", broken)

    outcome = runner.preview_task("demo", {"provider": "recording", "prompt": "{t}", "entities": {"t": "sensor.x"}}, {"recording_api_key": "k"})

    assert outcome["ok"] is False
    assert outcome["error"].startswith("ConnectionError: ")
    assert "Traceback" in outcome["traceback"] and "broken" in outcome["traceback"]
    assert "secret" not in outcome["error"] + outcome["traceback"]


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


def test_redact_url_hides_credentials_in_any_url():
    text = "Snapshot for http://admin:p%40ss@cam.local/snap.jpg failed: 401 for url https://u:pw@nas/x"

    assert runner.redact_url(text) == "Snapshot for http://***@cam.local/snap.jpg failed: 401 for url https://***@nas/x"
    assert runner.redact_url("https://example.com/a@b") == "https://example.com/a@b"


def test_failed_downloads_never_log_credentials(monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise requests.ConnectionError("Max retries exceeded with url: http://admin:topsecret@cam.local/snap.jpg")

    monkeypatch.setattr(runner.requests, "get", boom)

    with caplog.at_level("WARNING"):
        result = runner.fetch_external_url("http://admin:topsecret@cam.local/data")

    assert "topsecret" not in caplog.text and "topsecret" not in result
    assert "***@cam.local" in caplog.text


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
    monkeypatch.setattr(runner, "_ha_config", {"time_zone": "Asia/Tokyo"})
    assert runner.get_local_timezone({}).key == "Asia/Tokyo"

    monkeypatch.setattr(runner, "_ha_config", {})
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
            return {"time_zone": "Europe/Vienna", "language": "de"}

    monkeypatch.setattr(runner, "_ha_config", None)
    monkeypatch.setattr(runner.requests, "get", lambda url, **k: calls.append(url) or FakeResponse())

    assert runner.fetch_ha_time_zone() == "Europe/Vienna"
    assert runner.fetch_ha_time_zone() == "Europe/Vienna"
    assert runner.fetch_ha_language() == "de"
    assert calls == [f"{runner.HA_URL}/api/config"]


def test_fetch_ha_config_retries_after_failure(monkeypatch):
    calls = []

    def failing_get(url, **kwargs):
        calls.append(url)
        raise runner.requests.ConnectionError("down")

    monkeypatch.setattr(runner, "_ha_config", None)
    monkeypatch.setattr(runner.requests, "get", failing_get)

    assert runner.fetch_ha_time_zone() == ""
    assert runner.fetch_ha_language() == ""
    assert len(calls) == 2


def test_get_locale_prefers_option_then_home_assistant_then_english(monkeypatch):
    monkeypatch.setattr(runner, "_ha_config", {"language": "fr"})
    assert str(runner.get_locale({"language": "de"})) == "de"
    assert str(runner.get_locale({})) == "fr"

    monkeypatch.setattr(runner, "_ha_config", {"language": "pt-BR"})
    assert str(runner.get_locale({})) == "pt_BR"

    monkeypatch.setattr(runner, "_ha_config", {})
    assert str(runner.get_locale({})) == "en"
    assert str(runner.get_locale({"language": "xx-invalid"})) == "en"


@pytest.mark.parametrize("language, expected", [
    ("de", {"weekday": "Freitag", "today": "9. Oktober", "date": "9. Oktober 2026", "time": "16:24", "month": "Oktober"}),
    ("en", {"weekday": "Friday", "today": "October 9", "date": "October 9, 2026", "time": "4:24 PM", "month": "October"}),
    ("en-GB", {"weekday": "Friday", "today": "9 October", "time": "16:24"}),
    ("fr", {"weekday": "vendredi", "today": "9 octobre", "month": "octobre"}),
])
def test_builtin_placeholders_are_localized(language, expected):
    value = runner.datetime(2026, 10, 9, 16, 24, 10)

    placeholders = runner.builtin_placeholders(value, {"language": language})

    assert placeholders["year"] == "2026"
    for key, text in expected.items():
        assert placeholders[key] == text


def test_format_datetime_localizes_day_and_month_names():
    value = runner.datetime(2026, 10, 9, 16, 24, 10)
    options = {"language": "de", "datetime_format": "%A, %d. %B %Y (%a/%b) 100%%"}

    assert runner.format_datetime(value, options) == "Freitag, 09. Oktober 2026 (Fr./Okt.) 100%"


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
    task_cfg = {"provider": "recording", "data_processor": "missing.py", "entities": {"pv": "sensor.pv"},
                "hours": 24, "prompt": "{timeseries}"}

    runner.execute_task("demo", task_cfg, client=None, options={"recording_api_key": "k"})

    assert json.loads(RecordingProvider.requests[0].prompt)[0]["pv"] == 4.0


def _two_point_history(entities, hours):
    return [[
        {"entity_id": "sensor.outside", "state": "11.0", "last_changed": "2026-01-01T06:00:00+00:00"},
        {"entity_id": "sensor.outside", "state": "13.0", "last_changed": "2026-01-01T08:00:00+00:00"},
    ]]


def test_execute_task_appends_history_json_when_prompt_has_no_series_placeholder(task_env, monkeypatch):
    monkeypatch.setattr(runner, "fetch_ha_data", _two_point_history)
    task_cfg = {
        "provider": "recording",
        "entities": {"outside": "sensor.outside"},
        "hours": 12,
        "prompt": "Outside {outside} degrees. Memory: {{ {history} }}",
    }

    runner.execute_task("demo", task_cfg, client=None, options={"recording_api_key": "k"})

    prompt = RecordingProvider.requests[0].prompt
    head, series = prompt.split("\n\nMEASUREMENTS (JSON):\n")
    assert head == "Outside 21.5 degrees. Memory: { No history available. }"
    rows = json.loads(series)
    assert (rows[0]["outside"], rows[-1]["outside"]) == (11.0, 13.0)


@pytest.mark.parametrize("prompt", ["{timeseries}", "Data: {data}"])
def test_execute_task_does_not_append_history_twice(task_env, monkeypatch, prompt):
    monkeypatch.setattr(runner, "fetch_ha_data", _two_point_history)
    task_cfg = {"provider": "recording", "entities": {"outside": "sensor.outside"}, "hours": 24, "prompt": prompt}

    runner.execute_task("demo", task_cfg, client=None, options={"recording_api_key": "k"})

    assert "MEASUREMENTS" not in RecordingProvider.requests[0].prompt


@pytest.mark.parametrize("history_settings", [{}, {"hours": 0}])
@pytest.mark.parametrize("preview", [False, True])
def test_execute_task_appends_nothing_without_history(task_env, monkeypatch, history_settings, preview):
    requested = []
    monkeypatch.setattr(runner, "fetch_ha_data", lambda entities, hours: requested.append((entities, hours)) or [])
    task_cfg = {"provider": "recording", "entities": {"outside": "sensor.outside"},
                "prompt": "Outside {outside}.", **history_settings}

    if preview:
        assert runner.preview_task("demo", task_cfg, {"recording_api_key": "k"})["ok"] is True
    else:
        runner.execute_task("demo", task_cfg, client=None, options={"recording_api_key": "k"})

    assert requested == []
    assert RecordingProvider.requests[0].prompt == "Outside 21.5."


def test_execute_task_reports_missing_history_to_the_model(task_env):
    task_cfg = {"provider": "recording", "entities": {"outside": "sensor.outside"}, "hours": 24, "prompt": "Outside {outside}."}

    runner.execute_task("demo", task_cfg, client=None, options={"recording_api_key": "k"})

    assert RecordingProvider.requests[0].prompt.endswith("MEASUREMENTS (JSON):\nNo time series data available.")


def test_execute_task_localizes_builtins_and_lets_entities_win(task_env):
    task_cfg = {
        "provider": "recording",
        "entities": {"month": "sensor.month_override"},
        "hours": 0,
        "prompt": "Heute ist {weekday}, der {today}. {month}",
    }
    options = {"recording_api_key": "k", "language": "de"}

    runner.execute_task("demo", task_cfg, client=None, options=options)

    now = runner.local_now(options)
    weekday = runner.format_date(now, "EEEE", locale="de")
    assert RecordingProvider.requests[0].prompt == f"Heute ist {weekday}, der {now.day}. {runner.format_date(now, 'MMMM', locale='de')}. 21.5"

# --- 1.4.0: add-on config folder, migration, task memory, runner ---------------------------------


def test_fetch_binary_file_maps_legacy_config_paths_to_home_assistant(storage):
    (storage["HA_CONFIG_DIR"] / "www").mkdir()
    (storage["HA_CONFIG_DIR"] / "www" / "plan.txt").write_bytes(b"x")

    b64_data, _ = runner.fetch_binary_file("/config/www/plan.txt")

    assert runner.base64.b64decode(b64_data) == b"x"


def test_resolve_data_path_prefers_ha_config_then_addon_folder(storage):
    (storage["CONFIG_DIR"] / "only_addon.txt").write_text("a", encoding="utf-8")

    assert runner.resolve_data_path("only_addon.txt") == str(storage["CONFIG_DIR"] / "only_addon.txt")
    (storage["HA_CONFIG_DIR"] / "only_addon.txt").write_text("b", encoding="utf-8")
    assert runner.resolve_data_path("only_addon.txt") == str(storage["HA_CONFIG_DIR"] / "only_addon.txt")


@pytest.mark.parametrize("reference", ["solar.py", "solar", "scripts/processors/solar.py", "/config/scripts/processors/solar.py"])
def test_resolve_config_path_accepts_old_processor_references(storage, reference):
    legacy = storage["LEGACY_PROCESSORS_DIR"]
    legacy.mkdir(parents=True)
    (legacy / "solar.py").write_text("def process(df, config):\n    return {}, ''\n", encoding="utf-8")

    assert runner.resolve_config_path(reference) == str(legacy / "solar.py")

    storage["PROCESSORS_DIR"].mkdir()
    (storage["PROCESSORS_DIR"] / "solar.py").write_text("", encoding="utf-8")
    assert runner.resolve_config_path(reference) == str(storage["PROCESSORS_DIR"] / "solar.py")


def test_migrate_legacy_storage_copies_tasks_processors_and_memory(storage):
    ha = storage["HA_CONFIG_DIR"]
    (ha / "llm_tasks.yaml").write_text(
        "tasks:\n  weather_advisor:\n    target_sensor: sensor.weather_briefing_llm\n    prompt: hi\n  plain:\n    prompt: hi\n",
        encoding="utf-8")
    storage["LEGACY_PROCESSORS_DIR"].mkdir(parents=True)
    (storage["LEGACY_PROCESSORS_DIR"] / "a.py").write_text("old", encoding="utf-8")
    storage["PROCESSORS_DIR"].mkdir()
    (storage["PROCESSORS_DIR"] / "a.py").write_text("new", encoding="utf-8")
    (ha / "sensor_weather_briefing_llm_history.json").write_text('[{"time": "t", "text": "Pasta"}]', encoding="utf-8")

    assert runner.migrate_legacy_storage() is True

    assert storage["TASKS_CONFIG_PATH"].read_text(encoding="utf-8").startswith("tasks:")
    assert (storage["PROCESSORS_DIR"] / "a.py").read_text(encoding="utf-8") == "new"
    assert runner.load_memory("weather_advisor") == [{"time": "t", "text": "Pasta"}]
    assert (ha / "llm_tasks.yaml").exists()
    # Runs only once: an existing tasks file in the add-on folder always wins
    assert runner.migrate_legacy_storage() is False


def test_prototype_format_without_tasks_key_and_history_in_scripts(storage):
    ha = storage["HA_CONFIG_DIR"]
    (ha / "llm_tasks.yaml").write_text(
        "weather_advisor:\n  target_sensor: sensor.weather_briefing_llm\n  prompt: hi\n"
        "energy_status_24h:\n  hours: 24\n",
        encoding="utf-8")
    (ha / "scripts").mkdir()
    (ha / "scripts" / "sensor_weather_briefing_llm_history.json").write_text(
        '[{"time": "t", "text": "Lachs"}]', encoding="utf-8")

    assert runner.migrate_legacy_storage() is True

    assert list(runner.load_tasks()) == ["weather_advisor", "energy_status_24h"]
    assert runner.load_memory("weather_advisor") == [{"time": "t", "text": "Lachs"}]


def test_tasks_from_config_prefers_tasks_key():
    assert runner.tasks_from_config({"tasks": {"a": {"prompt": "x"}}, "b": {"prompt": "y"}}) == {"a": {"prompt": "x"}}
    assert runner.tasks_from_config({"a": {"prompt": "x"}, "note": "text"}) == {"a": {"prompt": "x"}}
    assert runner.tasks_from_config({"tasks": None}) == {}
    assert runner.tasks_from_config(None) == {}


def test_check_ha_api_reports_missing_token_and_auth_errors(monkeypatch, caplog):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    monkeypatch.setattr(runner.requests, "get", lambda *a, **k: pytest.fail("must not call the API without a token"))
    assert runner.check_ha_api() is False
    assert "No SUPERVISOR_TOKEN" in caplog.text

    class Unauthorized:
        def raise_for_status(self):
            raise runner.requests.HTTPError("401 Client Error: Unauthorized")

    monkeypatch.setenv("SUPERVISOR_TOKEN", "abc")
    monkeypatch.setattr(runner.requests, "get", lambda *a, **k: Unauthorized())
    assert runner.check_ha_api() is False
    assert "401" in caplog.text and "abc" not in caplog.text


def test_memory_keeps_newest_first_and_at_least_memory_size(storage):
    for i in range(runner.MEMORY_SIZE + 5):
        runner.append_memory("demo", f"answer {i}", {}, keep=7)

    memory = runner.load_memory("demo")
    assert len(memory) == runner.MEMORY_SIZE
    assert memory[0]["text"] == f"answer {runner.MEMORY_SIZE + 4}"

    runner.clear_memory("demo")
    assert runner.load_memory("demo") == []


def test_history_placeholder_works_without_target_sensor(task_env):
    published, _ = task_env
    task_cfg = {"provider": "recording", "hours": 0, "history_limit": 2, "prompt": "Before:\n{history}"}
    options = {"recording_api_key": "k"}

    runner.execute_task("menu", task_cfg, client=None, options=options)
    assert "No history available." in RecordingProvider.requests[-1].prompt

    for answer in ("Pasta", "Curry", "Salad"):
        RecordingProvider.answer = json.dumps({"text": answer})
        runner.execute_task("menu", task_cfg, client=None, options=options)
    RecordingProvider.answer = '{"state": "ok", "summary": "All good"}'

    prompt = RecordingProvider.requests[-1].prompt
    assert "Curry\n---\nPasta" in prompt
    assert "Salad" not in prompt
    assert runner.load_memory("menu")[0]["text"] == "Salad"


def test_failed_llm_call_keeps_memory_untouched(task_env, monkeypatch):
    runner.append_memory("menu", "Pasta", {})
    monkeypatch.setattr(RecordingProvider, "generate_text", lambda self, request: (_ for _ in ()).throw(RuntimeError("quota")))

    ok = runner.run_task("menu", {"provider": "recording", "hours": 0, "prompt": "x"}, None, {"recording_api_key": "k"})

    assert ok is False
    assert runner.TASK_STATUS["menu"]["state"] == "error"
    assert "quota" in runner.TASK_STATUS["menu"]["error"]
    assert [e["text"] for e in runner.load_memory("menu")] == ["Pasta"]


def test_run_task_records_status_prompt_and_result(task_env):
    ok = runner.run_task("demo", {"provider": "recording", "hours": 0, "prompt": "Hello"}, None, {"recording_api_key": "k"})

    status = runner.task_status("demo")
    assert ok is True
    assert status["state"] == "ok"
    assert status["last_prompt"].startswith("Hello")
    assert status["last_result"]["summary"] == "All good"
    assert status["duration"] >= 0
    assert runner.task_status("unknown") == {"state": "idle"}


def test_on_connect_publishes_discovery_without_running_tasks(monkeypatch, storage):
    storage["TASKS_CONFIG_PATH"].write_text("tasks:\n  a:\n    prompt: x\n", encoding="utf-8")
    runs, discovered = [], []
    monkeypatch.setattr(runner, "run_all_tasks_async", lambda client, options: runs.append(1))
    monkeypatch.setattr(runner, "run_task_async", lambda *args: runs.append(1))
    monkeypatch.setattr(runner, "publish_task_discovery", lambda client, task_id, cfg: discovered.append(task_id))

    class FakeClient:
        def subscribe(self, topic):
            pass

    client = FakeClient()
    runner.on_connect(client, None, None, 0)
    runner.on_connect(client, None, None, 0)

    assert runs == []
    assert discovered == ["a", "a"]
    assert runner.MQTT_STATUS["connected"] is True


def test_sync_task_discovery_removes_deleted_tasks(monkeypatch):
    published = []

    class FakeClient:
        def publish(self, topic, payload, retain=False):
            published.append((topic, payload))

    monkeypatch.setattr(runner, "publish_task_discovery", lambda client, task_id, cfg: published.append(("add", task_id)))
    runner.sync_task_discovery({"old": {}, "kept": {}}, {"kept": {}}, FakeClient())

    assert ("homeassistant/sensor/llm_old/config", "") in published
    assert ("homeassistant/button/llm_run_old/config", "") in published
    assert ("add", "kept") in published


def test_audit_archives_land_in_audit_dir_and_old_ones_are_pruned(storage):
    import os
    import time as _time

    path = runner.create_audit_archive("demo", "p", "v", {}, "", [], {"audit_retention_days": 1})
    assert os.path.dirname(path) == str(storage["AUDIT_DIR"])

    old = storage["AUDIT_DIR"] / "old.tar.gz"
    old.write_bytes(b"")
    two_days_ago = _time.time() - 2 * 86400
    os.utime(old, (two_days_ago, two_days_ago))

    assert runner.prune_audit_archives(1) == 1
    assert not old.exists()
    assert os.path.exists(path)
    assert runner.prune_audit_archives(0) == 0
