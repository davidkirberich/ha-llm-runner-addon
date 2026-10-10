import json
import threading
from types import SimpleNamespace

import pytest

import run
import web


class Broker:
    def __init__(self):
        self.messages = []
        self.subscriptions = []

    def publish(self, topic, payload, **options):
        self.messages.append((topic, json.loads(payload) if payload else None, options))
        return SimpleNamespace(rc=0)

    def subscribe(self, topic, qos=0):
        self.subscriptions.append(topic)

    def events(self):
        return [payload for topic, payload, _ in self.messages if topic == run.EVENT_TOPIC]


def trigger(broker, task="test", payload=None, retain=False):
    run.on_message(broker, None, SimpleNamespace(
        topic=f"ha_llm_runner/run/{task}",
        payload=json.dumps(payload or {}).encode(), retain=retain,
    ))


@pytest.fixture
def broker(monkeypatch):
    broker = Broker()
    monkeypatch.setattr(run, "_mqtt_client", broker)
    monkeypatch.setattr(run, "load_options", lambda: {})
    monkeypatch.setattr(run, "load_tasks", lambda: {"test": {"hours": 0}})
    return broker


def test_correlated_success_metadata_never_enters_result(broker):
    data = {"run_id": "unique", "request_id": "Kartoffel"}
    thread = run.run_task_async("test", {"hours": 0}, broker, {}, data)
    thread.join(3)
    assert not thread.is_alive()
    events = broker.events()
    assert [event["state"] for event in events] == ["triggered", "running", "ok"]
    assert all(event["run_id"] == "unique" and event["request_id"] == "Kartoffel" for event in events)
    assert all(event["session_id"] == run.SESSION_ID and event["protocol"] == 1 for event in events)
    assert not {"run_id", "request_id"} & events[-1]["result"].keys()
    assert events[-1]["duration"] >= 0
    assert events[-1]["started_at"] and events[-1]["finished_at"]
    assert not run.task_busy("test")
    for topic, _, options in broker.messages:
        if topic == run.EVENT_TOPIC:
            assert options == {"retain": False, "qos": 1}
        elif topic.startswith("ha_llm_runner/status/"):
            assert options == {"retain": True, "qos": 1}


@pytest.mark.parametrize("data", [
    {"run_id": "bad-input", "variables": {"unknown": "no"}},
    {"run_id": "bad-input", "request_id": True},
    {"run_id": "bad-input", "files": []},
])
def test_rejections_return_usable_correlation_without_execution(broker, monkeypatch, data):
    monkeypatch.setattr(run, "execute_task", lambda *a, **k: pytest.fail("Rejected input executed"))
    trigger(broker, payload=data)
    event = broker.events()[-1]
    assert event["state"] == "error"
    assert event["run_id"] == "bad-input"
    assert event["error"]
    assert not run.task_busy("test")


def test_unknown_task_has_correlated_error(broker):
    trigger(broker, task="missing", payload={"run_id": "unknown", "request_id": "caller"})
    event = broker.events()[-1]
    assert (event["task_id"], event["run_id"], event["request_id"]) == ("missing", "unknown", "caller")
    assert event["state"] == "error"
    assert "not found" in event["error"]


def test_processor_error_does_not_call_provider_or_replace_result(broker, monkeypatch):
    monkeypatch.setattr(run, "get_provider", lambda *a: pytest.fail("Provider called"))
    monkeypatch.setattr(run, "publish_task_state", lambda *a: pytest.fail("Result overwritten"))
    assert not run.run_task("test", {"processor": "missing.py", "prompt": "hello", "hours": 0}, broker, {})
    assert [event["state"] for event in broker.events()] == ["triggered", "running", "error"]
    assert "missing.py" in broker.events()[-1]["error"]


def test_provider_error_redacts_secrets(broker, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("https://u:password@host/?key=secret&x=1 token credential")
    monkeypatch.setattr(run, "execute_task", fail)
    assert not run.run_task("test", {}, broker, {"gemini_api_key": "secret", "mqtt_password": "credential"})
    error = broker.events()[-1]["error"]
    assert "password" not in error and "secret" not in error and "credential" not in error
    assert "***" in error


def test_duplicate_run_ids_do_not_execute_twice(broker, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def execute(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return {"result": {"text": "done"}}
    monkeypatch.setattr(run, "execute_task", execute)
    first = run.run_task_async("test", {}, broker, {}, {"run_id": "same"})
    assert entered.wait(3)
    assert run.run_task_async("test", {}, broker, {}, {"run_id": "same"}) is None
    release.set()
    first.join(3)
    assert run.run_task_async("test", {}, broker, {}, {"run_id": "same"}) is None
    assert calls == [1]
    assert len(broker.events()) == 3


def test_two_same_task_invocations_keep_own_inputs_and_correlations(broker, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def execute(task, cfg, client, options, inputs):
        if inputs["run_id"] == "one":
            entered.set()
            assert release.wait(3)
        return {"result": {"text": inputs["run_id"]}}
    monkeypatch.setattr(run, "execute_task", execute)
    first = run.run_task_async("test", {}, broker, {}, {"run_id": "one", "request_id": "same-label"})
    assert entered.wait(3)
    second = run.run_task_async("test", {}, broker, {}, {"run_id": "two", "request_id": "same-label"})
    assert run.task_busy("test")
    release.set()
    first.join(3)
    second.join(3)
    for run_id in ("one", "two"):
        events = [event for event in broker.events() if event["run_id"] == run_id]
        assert [event["state"] for event in events] == ["triggered", "running", "ok"]
        assert events[-1]["result"]["text"] == run_id


def test_reconnect_republishes_current_status_not_idle(broker):
    invocation = run.Invocation("test", queued_at="now")
    run.publish_lifecycle(broker, invocation, "running")
    broker.messages.clear()
    run.on_connect(broker, None, None, 0)
    assert broker.events() == []
    statuses = [payload for topic, payload, _ in broker.messages if topic == "ha_llm_runner/status/test"]
    assert statuses[-1]["state"] == "running"
    assert statuses[-1]["run_id"] == invocation.run_id
    assert broker.messages[-1][1]["state"] == "online"


def test_catalog_and_discovery_use_only_task_ids(broker):
    run.sync_task_discovery({}, {"test": {"name": "Friendly", "prompt": "secret"}}, broker)
    catalog = [payload for topic, payload, _ in broker.messages if topic == run.CATALOG_TOPIC][-1]
    assert catalog["tasks"] == [{"task_id": "test"}]
    configs = {topic: payload for topic, payload, _ in broker.messages if topic.endswith("/config")}
    assert configs["homeassistant/sensor/llm_test/config"]["name"] == "test"
    assert configs["homeassistant/button/llm_run_test/config"]["name"] == "Run test"
    run.sync_task_discovery({"test": {}}, {}, broker)
    assert "test" not in run._task_lifecycle
    assert [payload for topic, payload, _ in broker.messages if topic == run.CATALOG_TOPIC][-1]["tasks"] == []
    assert ("ha_llm_runner/status/test", None, {"retain": True}) in broker.messages


@pytest.mark.parametrize("payload", [b"RUN", b'{"run_id":"old"}'])
def test_retained_commands_never_execute(broker, monkeypatch, payload):
    monkeypatch.setattr(run, "execute_task", lambda *a, **k: pytest.fail("Retained command executed"))
    run.on_message(broker, None, SimpleNamespace(topic="ha_llm_runner/run/test", payload=payload, retain=True))
    assert broker.events() == []


def test_preview_emits_no_lifecycle(broker):
    assert run.preview_task("test", {"hours": 0}, {})["ok"]
    assert broker.messages == []


def test_invalid_utf8_does_not_break_mqtt_callback(broker):
    run.on_message(broker, None, SimpleNamespace(topic="ha_llm_runner/run/test", payload=b"\xff", retain=False))
    assert broker.events() == []


def test_main_sets_offline_last_will_and_announces_shutdown(monkeypatch):
    class Client(Broker):
        def will_set(self, topic, payload, **options):
            self.will = (topic, json.loads(payload), options)
        def connect_async(self, host, port, keepalive):
            self.connection = (host, port, keepalive)
        def loop_forever(self, **kwargs):
            self.on_connect(self, None, None, 0)
        def disconnect(self):
            self.disconnected = True
    client = Client()
    monkeypatch.setattr(run.mqtt, "Client", lambda *args, **kwargs: client)
    monkeypatch.setattr(run, "prepare_storage", lambda: None)
    monkeypatch.setattr(run, "check_ha_api", lambda: True)
    monkeypatch.setattr(run, "load_options", lambda: {})
    monkeypatch.setattr(run, "load_tasks", lambda: {"test": {}})
    monkeypatch.setattr(web, "start_web_server", lambda *a, **k: None)
    run.main()
    topic, will, options = client.will
    assert topic == run.AVAILABILITY_TOPIC
    assert will == {"protocol": 1, "session_id": run.SESSION_ID, "state": "offline"}
    assert options == {"qos": 1, "retain": True}
    assert client.connection == ("core-mosquitto", 1883, 60)
    assert client.disconnected
    assert [payload["state"] for topic, payload, _ in client.messages
            if topic == run.AVAILABILITY_TOPIC] == ["online", "offline"]


def test_completed_dedup_cache_is_bounded(broker):
    for index in range(260):
        invocation = run.Invocation("test", str(index))
        assert run.reserve_invocation(invocation)
        run.finish_invocation(invocation)
    assert len(run._completed_invocations) == 256
    assert run.reserve_invocation(run.Invocation("test", "0"))


def test_duplicate_is_ignored_before_input_revalidation(broker, monkeypatch):
    invocation = run.Invocation("test", "done")
    assert run.reserve_invocation(invocation)
    run.finish_invocation(invocation)
    monkeypatch.setattr(run, "prepare_task_inputs", lambda *a: pytest.fail("Duplicate revalidated"))
    assert run.run_task_async("test", {}, broker, {}, {"run_id": "done"}) is None
    assert broker.messages == []


def test_rest_reports_duplicate_without_claiming_new_queue_entry(broker, monkeypatch):
    invocation = run.Invocation("test", "done")
    assert run.reserve_invocation(invocation)
    run.finish_invocation(invocation)
    app = web.WebApp(run)
    monkeypatch.setattr(app, "task_or_404", lambda task: {})
    assert app.run_task("test", body={"run_id": "done"}) == (200, {"queued": [], "duplicate": True})


def test_thread_start_failure_returns_error_and_releases_invocation(broker, monkeypatch):
    def fail(thread):
        raise RuntimeError("cannot start thread")
    monkeypatch.setattr(threading.Thread, "start", fail)
    with pytest.raises(RuntimeError):
        run.run_task_async("test", {}, broker, {}, {"run_id": "start-failed"})
    assert [event["state"] for event in broker.events()] == ["triggered", "error"]
    assert run.TASK_STATUS["test"]["state"] == "error"
    assert not run.task_busy("test")


def test_run_all_emits_triggered_immediately_even_while_queue_blocked(broker, monkeypatch):
    monkeypatch.setattr(run, "load_tasks", lambda: {"one": {"hours": 0}, "two": {"hours": 0}})
    with run._run_lock:
        thread = run.run_all_tasks_async(broker, {})
        assert [event["state"] for event in broker.events()] == ["triggered", "triggered"]
        assert run.task_busy("one") and run.task_busy("two")
    thread.join(3)
    assert not thread.is_alive()
    assert [event["state"] for event in broker.events()].count("ok") == 2
