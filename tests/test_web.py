import io
import json
import tarfile
import threading

import pytest
import requests

import run as runner
import web


@pytest.fixture
def server(storage):
    srv = web.create_server(runner, host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()
    srv.server_close()


def send(method, url, body=None):
    return requests.request(method, url, data=json.dumps(body or {}), headers={"Content-Type": "application/json"}, timeout=5)


def write_tasks(storage, text):
    storage["TASKS_CONFIG_PATH"].write_text(text, encoding="utf-8")


def test_index_is_served_with_relative_api_urls(server):
    response = requests.get(server, timeout=5)

    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/html")
    assert 'api("GET", "api/overview")' in response.text
    assert '"/api/' not in response.text


def test_only_ingress_proxy_and_localhost_may_connect(server, monkeypatch):
    monkeypatch.setattr(web, "ALLOWED_CLIENTS", {"172.30.32.2"})

    assert requests.get(server + "api/overview", timeout=5).status_code == 403


def test_changes_require_json_content_type(server, storage):
    write_tasks(storage, "tasks:\n  a:\n    prompt: x\n")

    response = requests.post(server + "api/tasks/a/run", data="x", headers={"Content-Type": "text/plain"}, timeout=5)

    assert response.status_code == 415


def test_overview_lists_tasks_status_and_config_problems(server, storage):
    write_tasks(storage, "tasks:\n  menu:\n    name: Menu\n    prompt: x\n    promt: typo\n")
    runner.append_memory("menu", "Pasta", {})
    runner.TASK_STATUS["menu"] = {"state": "ok", "last_prompt": "secret", "finished_at": "2025-01-01T08:00:00"}

    data = requests.get(server + "api/overview", timeout=5).json()

    task = data["tasks"][0]
    assert task["id"] == "menu" and task["name"] == "Menu" and task["llm"] is True
    assert task["memory_entries"] == 1
    assert task["status"]["state"] == "ok"
    assert "last_prompt" not in task["status"]
    assert any("promt" in w for w in data["config_warnings"])
    assert data["mqtt"]["connected"] is False


def test_task_detail_run_and_clear_memory(server, storage, monkeypatch):
    write_tasks(storage, "tasks:\n  menu:\n    prompt: x\n")
    runner.append_memory("menu", "Pasta", {})
    started = []
    monkeypatch.setattr(runner, "run_task_async", lambda task_id, cfg, client, options: started.append(task_id))

    detail = requests.get(server + "api/tasks/menu", timeout=5).json()
    assert detail["memory"][0]["text"] == "Pasta"
    assert detail["status"] == {"state": "idle"}

    assert send("POST", server + "api/tasks/menu/run").status_code == 202
    assert started == ["menu"]
    assert send("POST", server + "api/tasks/nope/run").status_code == 404

    assert send("DELETE", server + "api/tasks/menu/memory").status_code == 200
    assert runner.load_memory("menu") == []


def test_config_validation_reports_yaml_line(server):
    result = send("POST", server + "api/config/validate", {"content": "tasks:\n  a:\n    prompt: [unclosed\n"}).json()

    assert result["valid"] is False
    assert result["errors"][0]["line"] >= 3


@pytest.mark.parametrize("content, message", [
    ("- a\n- b\n", "mapping"),
    ("tasks:\n  - a\n", "mapping"),
    ("tasks:\n  a: hello\n", "Task 'a'"),
    ("tasks:\n  a:\n    hours: lots\n", "whole number"),
])
def test_config_validation_rejects_wrong_structure(server, content, message):
    result = send("POST", server + "api/config/validate", {"content": content}).json()

    assert result["valid"] is False
    assert message in result["errors"][0]["message"]


def test_config_validation_warns_about_missing_processor_and_broken_braces(server):
    content = "tasks:\n  a:\n    data_processor: missing.py\n    prompt: 'JSON like {\"a\": 1'\n"

    result = send("POST", server + "api/config/validate", {"content": content}).json()

    assert result["valid"] is True
    assert any("missing.py" in w for w in result["warnings"])
    assert any("braces" in w for w in result["warnings"])


def test_config_validation_accepts_prototype_format_without_tasks_key(server):
    content = "weather_advisor:\n  prompt: hi\n  history_limit: 7\nnote: just text\n"

    result = send("POST", server + "api/config/validate", {"content": content}).json()

    assert result["valid"] is True
    assert result["task_ids"] == ["weather_advisor"]
    assert any("'note'" in w for w in result["warnings"])


def test_saving_config_keeps_backup_and_syncs_entities(server, storage, monkeypatch):
    write_tasks(storage, "tasks:\n  old:\n    prompt: x\n")
    synced = []
    monkeypatch.setattr(runner, "sync_task_discovery", lambda old, new: synced.append((sorted(old), sorted(new))))

    response = send("PUT", server + "api/config", {"content": "tasks:\n  new:\n    prompt: y\n"})

    assert response.status_code == 200
    assert storage["TASKS_CONFIG_PATH"].read_text(encoding="utf-8") == "tasks:\n  new:\n    prompt: y\n"
    assert (storage["CONFIG_DIR"] / "llm_tasks.yaml.bak").read_text(encoding="utf-8") == "tasks:\n  old:\n    prompt: x\n"
    assert synced == [(["old"], ["new"])]


def test_invalid_config_is_not_saved(server, storage):
    write_tasks(storage, "tasks: {}\n")

    response = send("PUT", server + "api/config", {"content": "tasks: [\n"})

    assert response.status_code == 400
    assert response.json()["errors"]
    assert storage["TASKS_CONFIG_PATH"].read_text(encoding="utf-8") == "tasks: {}\n"


def test_processor_crud_with_syntax_check(server, storage):
    write_tasks(storage, "tasks:\n  solar:\n    data_processor: solar.py\n")

    bad = send("PUT", server + "api/processors/solar.py", {"content": "def process(df, config):\n    return (\n"})
    assert bad.status_code == 400
    assert bad.json()["line"] >= 2

    ok = send("PUT", server + "api/processors/solar.py", {"content": "def helper():\n    pass\n"})
    assert ok.status_code == 200
    assert "process" in ok.json()["warnings"][0]

    listing = requests.get(server + "api/processors", timeout=5).json()
    assert listing["processors"][0]["name"] == "solar.py"
    assert listing["processors"][0]["used_by"] == ["solar"]
    assert requests.get(server + "api/processors/solar.py", timeout=5).json()["content"].startswith("def helper")

    assert send("DELETE", server + "api/processors/solar.py").status_code == 200
    assert not (storage["PROCESSORS_DIR"] / "solar.py").exists()


@pytest.mark.parametrize("name", ["..%2Frun.py", "x.txt", "%2E%2E%2F%2E%2E%2Fevil.py", "a b.py"])
def test_processor_names_cannot_escape_the_folder(server, name):
    response = send("PUT", server + f"api/processors/{name}", {"content": "x = 1\n"})

    assert response.status_code in (400, 404)


def test_audit_list_detail_files_and_delete(server, storage):
    image = runner.base64.b64encode(b"\x89PNG fake").decode()
    path = runner.create_audit_archive("menu", "the prompt", "the answer", {"t": 1}, '[{"a": 1}]', [(image, "image/png")], {})
    name = path.replace("\\", "/").rsplit("/", 1)[-1]

    listing = requests.get(server + "api/audit?task=menu", timeout=5).json()["archives"]
    assert [a["name"] for a in listing] == [name]
    assert requests.get(server + "api/audit?task=other", timeout=5).json()["archives"] == []

    members = requests.get(server + f"api/audit/{name}", timeout=5).json()["members"]
    assert [m["name"] for m in members][:2] == ["prompt.txt", "response.md"]
    assert members[0]["content"] == "the prompt"
    picture = next(m for m in members if m["mime"] == "image/png")

    file_response = requests.get(server + f"api/audit/{name}/files/{picture['name']}", timeout=5)
    assert file_response.content == b"\x89PNG fake"
    assert file_response.headers["Content-Type"] == "image/png"

    download = requests.get(server + f"api/audit/{name}/download", timeout=5)
    assert "attachment" in download.headers["Content-Disposition"]
    with tarfile.open(fileobj=io.BytesIO(download.content), mode="r:gz") as tar:
        assert "prompt.txt" in tar.getnames()

    assert send("DELETE", server + f"api/audit/{name}").status_code == 200
    assert requests.get(server + "api/audit", timeout=5).json()["archives"] == []


@pytest.mark.parametrize("name", ["..%2Fllm_tasks.yaml", "audit_x_20250101_000000.tar.gz.bak", "%2E%2E"])
def test_audit_names_cannot_escape_the_folder(server, name):
    assert requests.get(server + f"api/audit/{name}", timeout=5).status_code in (400, 404)
