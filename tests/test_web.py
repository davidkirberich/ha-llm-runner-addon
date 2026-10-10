import io
import json
import tarfile
import threading

import pytest
import requests
import yaml

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


def test_index_is_served_with_relative_api_urls(server, monkeypatch, tmp_path):
    monkeypatch.setattr(web, "STATIC_DIR", str(tmp_path))
    build = tmp_path / "svelte"
    build.mkdir()
    (build / "index.html").write_text('<script src="./assets/index-test.js"></script>', encoding="utf-8")
    response = requests.get(server, timeout=5)

    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/html")
    assert './assets/index-test.js' in response.text
    assert '"/api/' not in response.text


def test_only_ingress_proxy_and_localhost_may_connect(server, monkeypatch):
    monkeypatch.setattr(web, "ALLOWED_CLIENTS", {"172.30.32.2"})

    assert requests.get(server + "api/overview", timeout=5).status_code == 403


def test_svelte_interface_is_default_and_serves_only_build_assets(server, monkeypatch, tmp_path):
    monkeypatch.setattr(web, "STATIC_DIR", str(tmp_path))
    (tmp_path / "index.html").write_text("legacy", encoding="utf-8")
    assert requests.get(server, timeout=5).status_code == 503
    assert requests.get(server + "?ui=svelte", timeout=5).status_code == 503
    assets = tmp_path / "svelte" / "assets"
    assets.mkdir(parents=True)
    (assets.parent / "index.html").write_text("svelte", encoding="utf-8")
    (assets / "index-test.js").write_text("export {};", encoding="utf-8")
    (assets / "index-test.css").write_text("body {}", encoding="utf-8")
    assert requests.get(server, timeout=5).text == "svelte"
    assert requests.get(server + "?ui=svelte", timeout=5).text == "svelte"
    js = requests.get(server + "assets/index-test.js", timeout=5)
    assert js.status_code == 200 and js.headers["Content-Type"].startswith("text/javascript")
    assert requests.get(server + "assets/index-test.css", timeout=5).status_code == 200
    assert requests.get(server + "assets/missing.js", timeout=5).status_code == 404
    assert requests.get(server + "assets/%2e%2e%2findex.html", timeout=5).status_code == 404


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
    assert task["id"] == "menu" and "name" not in task and task["llm"] is True
    assert task["memory_entries"] == 1
    assert task["status"]["state"] == "ok"
    assert "last_prompt" not in task["status"]
    assert any("promt" in w for w in data["config_warnings"])
    assert data["mqtt"]["connected"] is False


def test_task_detail_run_and_clear_memory(server, storage, monkeypatch):
    write_tasks(storage, "tasks:\n  menu:\n    prompt: x\n")
    runner.append_memory("menu", "Pasta", {})
    started = []
    monkeypatch.setattr(runner, "run_task_async", lambda task_id, cfg, client, options, inputs=None: started.append(task_id) or object())

    detail = requests.get(server + "api/tasks/menu", timeout=5).json()
    assert detail["memory"][0]["text"] == "Pasta"
    assert detail["status"] == {"state": "idle"}

    assert send("POST", server + "api/tasks/menu/run").status_code == 202
    assert started == ["menu"]
    assert send("POST", server + "api/tasks/nope/run").status_code == 404

    assert send("DELETE", server + "api/tasks/menu/memory").status_code == 200
    assert runner.load_memory("menu") == []


def test_preview_runs_the_unsaved_task_text(server, storage, monkeypatch):
    write_tasks(storage, "tasks:\n  menu:\n    prompt: old\n  other:\n    prompt: y\n")
    calls = []
    monkeypatch.setattr(runner, "preview_task", lambda task_id, cfg, options: calls.append((task_id, cfg)) or {"ok": True, "result": {"state": "x"}})

    response = send("POST", server + "api/tasks/menu/preview", {"content": "prompt: new {missing\nhours: 0\n"})

    assert response.status_code == 200
    assert response.json()["ok"] is True and response.json()["warnings"]
    assert calls == [("menu", {"prompt": "new {missing", "hours": 0})]
    assert storage["TASKS_CONFIG_PATH"].read_text(encoding="utf-8") == "tasks:\n  menu:\n    prompt: old\n  other:\n    prompt: y\n"


def test_preview_rejects_broken_yaml_and_unknown_tasks(server, storage, monkeypatch):
    write_tasks(storage, "tasks:\n  menu:\n    prompt: x\n")
    monkeypatch.setattr(runner, "preview_task", lambda *args: pytest.fail("must not run"))

    broken = send("POST", server + "api/tasks/menu/preview", {"content": "prompt: [x\n"})
    assert broken.status_code == 400 and "line" in broken.json()["error"]
    assert send("POST", server + "api/tasks/menu/preview", {"content": "hours: soon\nprompt: x\n"}).status_code == 400
    assert send("POST", server + "api/tasks/nope/preview", {"content": "prompt: x\n"}).status_code == 404


STATES = [
    {"entity_id": "sensor.grid_power", "state": "-1200", "attributes": {"friendly_name": "Grid power", "unit_of_measurement": "W"}},
    {"entity_id": "sensor.pv_power", "state": "unavailable", "attributes": {"friendly_name": "PV power", "unit_of_measurement": "W"}},
    {"entity_id": "climate.living_room", "state": "heat", "attributes": {"friendly_name": "Living room", "current_temperature": 21.5}},
]


def check_entities(server, content):
    return send("POST", server + "api/yaml/check", {"content": content, "entities": True}).json()


def test_task_entities_show_current_values(server, monkeypatch):
    monkeypatch.setattr(runner, "fetch_ha_states", lambda: STATES)

    data = check_entities(server, "entities:\n"
                                  "  grid: sensor.grid_power\n  pv: sensor.pv_power\n  room: climate.living_room:current_temperature\n"
                                  "  gone: sensor.removed\n  cam: http://admin:secret@cam.local/snap.jpg\n  cal: calendar.family\n"
                                  "urls:\n  news: https://example.com/news\nprompt: x\n")["entities"]

    rows = {row["alias"]: row for row in data["rows"]}
    assert data["error"] is None and data["list_format"] is False
    assert rows["grid"]["name"] == "Grid power" and rows["grid"]["state"] == "-1200" and rows["grid"]["unit"] == "W"
    assert rows["pv"]["state"] == "unavailable"
    assert rows["room"]["state"] == 21.5 and rows["room"]["unit"] is None
    assert rows["gone"]["missing"] is True
    assert rows["cam"]["kind"] == "camera" and "secret@cam.local" in rows["cam"]["target"]
    assert rows["cal"]["kind"] == "calendar"
    assert rows["news"]["kind"] == "url" and rows["news"]["section"] == "urls"


def test_entity_rows_only_for_a_valid_mapping(server):
    assert check_entities(server, "prompt: [x\n")["entities"] is None
    assert check_entities(server, "- a\n")["entities"] is None
    assert check_entities(server, "")["entities"] is None
    assert check_entities(server, "prompt: x\n")["entities"] == {"rows": [], "error": None, "list_format": False}
    assert "entities" not in send("POST", server + "api/yaml/check", {"content": "prompt: x\n"}).json()


def test_task_detail_shows_the_whole_task_as_written(server, storage):
    write_tasks(storage, "tasks:\n  energy:\n    entities:\n      grid: sensor.grid_power\n    prompt: x\n")

    detail = requests.get(server + "api/tasks/energy", timeout=5).json()

    assert "sensor.grid_power" in detail["config_yaml"]
    assert detail["settings_text"] == "entities:\n  grid: sensor.grid_power\nprompt: x\n"


def test_task_text_is_shown_as_written_in_the_file(server, storage):
    # A space before a line break makes PyYAML fall back to one long quoted string
    write_tasks(storage, "radar:\n  name: \"Radar\"  # shown in HA\n  files:\n    r: http://x/r.gif\n"
                         "  prompt: |\n    Time: {now}. \n    Next line\n\n    Rules\n\n# Other\nother:\n  prompt: y\n")

    detail = requests.get(server + "api/tasks/radar", timeout=5).json()

    assert detail["settings_text"] == ("name: \"Radar\"  # shown in HA\nfiles:\n  r: http://x/r.gif\n"
                                       "prompt: |\n  Time: {now}. \n  Next line\n\n  Rules\n")


def test_task_entities_report_unreachable_home_assistant(server, monkeypatch):
    def offline():
        raise requests.ConnectionError("no route")
    monkeypatch.setattr(runner, "fetch_ha_states", offline)

    data = check_entities(server, "entities:\n  grid: sensor.grid_power\nprompt: x\n")["entities"]

    assert "not reachable" in data["error"]
    assert requests.get(server + "api/entities?q=grid", timeout=5).status_code == 502


def test_entity_search_matches_all_words_in_id_and_name(server, monkeypatch):
    monkeypatch.setattr(runner, "fetch_ha_states", lambda: STATES)

    by_name = requests.get(server + "api/entities?q=GRID%20pow", timeout=5).json()
    by_domain = requests.get(server + "api/entities?q=sensor", timeout=5).json()

    assert [e["entity_id"] for e in by_name["entities"]] == ["sensor.grid_power"]
    assert by_name["entities"][0] == {"entity_id": "sensor.grid_power", "name": "Grid power", "state": "-1200", "unit": "W"}
    assert by_domain["total"] == 2


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


def test_config_validation_warns_about_images_in_tasks_without_prompt(server):
    content = ("tasks:\n  a:\n    files:\n      webcam: http://cam/x.jpg\n    entities:\n      cam: camera.door\n      t: sensor.t\n"
               "  b:\n    prompt: hi\n    files:\n      webcam: http://cam/x.jpg\n")

    warnings = send("POST", server + "api/config/validate", {"content": content}).json()["warnings"]

    assert [w for w in warnings if "no prompt" in w] == [
        "Task 'a': no prompt, so no LLM is called and the images/files webcam, cam are not used. Add a prompt or remove them."]

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


COMMENTED_TASKS = """# My tasks
tasks:
  # Energy report
  energy:
    name: Energy  # shown in HA
    entities:
      grid: sensor.grid_power   # grid
      pv: "sensor.pv_power"
    prompt: |
      Grid {grid}, PV {pv}.

      Keep it short.

  # Plants
  plants:
    prompt: >-
      Water the plants
    files:
      photo: http://cam/photo.jpg
"""


ENERGY_TEXT = """name: Energy  # shown in HA
entities:
  grid: sensor.grid_power   # grid
  pv: "sensor.pv_power"
prompt: |
  Grid {grid}, PV {pv}.

  Keep it short.
"""


def edit_entities(server, content, **change):
    return send("POST", server + "api/yaml/entities", {"content": content, **change})


def test_adding_an_entity_changes_only_the_editor_text(server, storage):
    write_tasks(storage, COMMENTED_TASKS)

    response = edit_entities(server, ENERGY_TEXT, add={"alias": "house", "entity_id": "sensor.house_power"})

    assert response.status_code == 200
    assert response.json()["content"] == ENERGY_TEXT.replace('  pv: "sensor.pv_power"\n', '  pv: "sensor.pv_power"\n  house: sensor.house_power\n')
    assert storage["TASKS_CONFIG_PATH"].read_text(encoding="utf-8") == COMMENTED_TASKS
    assert not (storage["CONFIG_DIR"] / "llm_tasks.yaml.bak").exists()


def test_adding_creates_the_entities_section_before_the_prompt(server):
    content = edit_entities(server, "# Watering\nname: Water\nprompt: >-\n  Water the plants\nhours: 2\n",
                            add={"alias": "rain", "entity_id": "sensor.rain"}).json()["content"]

    assert content == "# Watering\nname: Water\nentities:\n  rain: sensor.rain\nprompt: >-\n  Water the plants\nhours: 2\n"


def test_removing_an_entity_keeps_the_rest(server):
    content = edit_entities(server, ENERGY_TEXT, remove="grid").json()["content"]
    assert content == ENERGY_TEXT.replace("  grid: sensor.grid_power   # grid\n", "")

    content = edit_entities(server, content, remove="pv").json()["content"]
    assert content == ENERGY_TEXT.replace('entities:\n  grid: sensor.grid_power   # grid\n  pv: "sensor.pv_power"\n', "")

    assert edit_entities(server, content, remove="pv").status_code == 404


def test_entities_in_list_format_are_appended_and_removed_by_index(server):
    content = edit_entities(server, "entities:\n  - sensor.a\nprompt: x\n", add={"entity_id": "sensor.b"}).json()["content"]
    assert content == "entities:\n  - sensor.a\n  - sensor.b\nprompt: x\n"

    assert edit_entities(server, content, remove="0").json()["content"] == "entities:\n  - sensor.b\nprompt: x\n"


def test_files_and_urls_are_removed_from_their_own_section(server):
    text = "files:\n  photo: http://cam/a.jpg\n  plan: http://x/p.pdf\nurls:\n  news: https://example.com\nprompt: x  # keep\n"

    content = edit_entities(server, text, remove="photo", section="files").json()["content"]
    assert content == text.replace("  photo: http://cam/a.jpg\n", "")

    content = edit_entities(server, content, remove="news", section="urls").json()["content"]
    assert content == "files:\n  plan: http://x/p.pdf\nprompt: x  # keep\n"

    assert edit_entities(server, content, remove="plan", section="urls").status_code == 404
    assert edit_entities(server, content, remove="plan", section="prompt").status_code == 400


@pytest.mark.parametrize("body, status", [
    ({"add": {"alias": "grid", "entity_id": "sensor.other"}}, 409),
    ({"add": {"alias": "again", "entity_id": "sensor.grid_power"}}, 409),
    ({"add": {"alias": "date", "entity_id": "sensor.other"}}, 400),
    ({"add": {"alias": "123", "entity_id": "sensor.other"}}, 400),
    ({"add": {"alias": "a-b", "entity_id": "sensor.other"}}, 400),
    ({"add": {"alias": "ok", "entity_id": "not an entity"}}, 400),
    ({}, 400),
])
def test_adding_rejects_bad_or_duplicate_aliases(server, body, status):
    assert edit_entities(server, ENERGY_TEXT, **body).status_code == status


@pytest.mark.parametrize("content", ["prompt: [x\n", "- a list\n", ""])
def test_entities_cannot_be_edited_in_broken_text(server, content):
    assert edit_entities(server, content, add={"alias": "x", "entity_id": "sensor.x"}).status_code == 400


def test_alias_may_start_with_a_digit_and_fills_the_prompt(server):
    content = edit_entities(server, ENERGY_TEXT, add={"alias": "1og_temp", "entity_id": "sensor.og_temp"}).json()["content"]

    assert "  1og_temp: sensor.og_temp\n" in content
    assert "{1og_temp} C".format(**{"1og_temp": 21}) == "21 C"


def test_new_task_is_appended_with_a_skeleton(server, storage, monkeypatch):
    write_tasks(storage, COMMENTED_TASKS)
    synced = []
    monkeypatch.setattr(runner, "sync_task_discovery", lambda old, new: synced.append(sorted(new)))

    response = send("POST", server + "api/tasks", {"id": "garden_report"})

    assert response.status_code == 201
    assert storage["TASKS_CONFIG_PATH"].read_text(encoding="utf-8") == COMMENTED_TASKS + (
        "\n  garden_report:\n    prompt: |\n      Describe the current situation in one short sentence.\n")
    assert (storage["CONFIG_DIR"] / "llm_tasks.yaml.bak").read_text(encoding="utf-8") == COMMENTED_TASKS
    assert synced == [["energy", "garden_report", "plants"]]
    assert "name" not in runner.load_tasks()["garden_report"]


def test_new_task_in_a_missing_file(server, storage):
    assert send("POST", server + "api/tasks", {"id": "first"}).status_code == 201
    assert storage["TASKS_CONFIG_PATH"].read_text(encoding="utf-8").startswith("first:\n  prompt: |\n")


@pytest.mark.parametrize("task_id, status", [("energy", 409), ("Energy", 400), ("a-b", 400), ("", 400), ("x" * 65, 400)])
def test_new_task_rejects_bad_or_existing_ids(server, storage, task_id, status):
    write_tasks(storage, COMMENTED_TASKS)

    assert send("POST", server + "api/tasks", {"id": task_id}).status_code == status
    assert storage["TASKS_CONFIG_PATH"].read_text(encoding="utf-8") == COMMENTED_TASKS


def test_task_text_round_trip_leaves_the_file_unchanged(server, storage):
    variants = [COMMENTED_TASKS, COMMENTED_TASKS + "other_section: 1\n"]
    for original in variants:
        storage["TASKS_CONFIG_PATH"].write_bytes(original.encode("utf-8"))
        for task_id in ("energy", "plants"):
            text = requests.get(server + f"api/tasks/{task_id}", timeout=5).json()["settings_text"]
            assert send("PUT", server + f"api/tasks/{task_id}/settings", {"content": text}).status_code == 200
            assert storage["TASKS_CONFIG_PATH"].read_bytes().decode("utf-8") == original


def test_task_text_round_trip_keeps_crlf():
    original = COMMENTED_TASKS.replace("\n", "\r\n")
    for task_id in ("energy", "plants"):
        assert web.replace_task(original, task_id, web.task_text(original, task_id)) == original


def test_task_round_trip_with_indented_blank_line_after_block_scalar():
    # The stray "        " line is prompt content in YAML, but the editor never shows it
    for newline in ("\n", "\r\n"):
        original = ("a:\n  prompt: |\n    hello\n    \n        \nb:\n  prompt: hi\n").replace("\n", newline)
        result = web.replace_task(original, "a", web.task_text(original, "a"))
        tasks = yaml.safe_load(result)
        assert tasks["a"]["prompt"] == "hello\n"
        assert tasks["b"] == {"prompt": "hi"}
        assert result.endswith(newline.join(["", "", "", "b:", "  prompt: hi", ""]))


def test_replace_task_accepts_text_without_final_line_break():
    original = "a:\n  prompt: x\nb:\n  prompt: hi\n"
    result = web.replace_task(original, "a", "prompt: |\n  Was siehst du?")
    assert result == "a:\n  prompt: |\n    Was siehst du?\nb:\n  prompt: hi\n"
    assert yaml.safe_load(result)["a"]["prompt"] == "Was siehst du?\n"

def test_task_text_contains_entities_files_and_comments(server, storage):
    write_tasks(storage, COMMENTED_TASKS)

    assert requests.get(server + "api/tasks/energy", timeout=5).json()["settings_text"] == ENERGY_TEXT
    assert requests.get(server + "api/tasks/plants", timeout=5).json()["settings_text"] == (
        "prompt: >-\n  Water the plants\nfiles:\n  photo: http://cam/photo.jpg\n")


def test_flow_style_task_is_saved_in_block_style(server, storage, monkeypatch):
    write_tasks(storage, "a: {prompt: x, hours: 2}\nb:\n  prompt: y\n")
    monkeypatch.setattr(runner, "sync_task_discovery", lambda old, new: None)

    text = requests.get(server + "api/tasks/a", timeout=5).json()["settings_text"]
    assert text == "prompt: x\nhours: 2\n"
    assert send("PUT", server + "api/tasks/a/settings", {"content": text + "history_limit: 1\n"}).status_code == 200
    assert storage["TASKS_CONFIG_PATH"].read_text(encoding="utf-8") == "a:\n  prompt: x\n  hours: 2\n  history_limit: 1\nb:\n  prompt: y\n"


def test_duplicate_keys_are_a_syntax_error(server, storage):
    write_tasks(storage, COMMENTED_TASKS)

    check = send("POST", server + "api/yaml/check", {"content": "hours: 1\nhours: 2\n"}).json()
    response = send("PUT", server + "api/tasks/energy/settings", {"content": "prompt: x\nhours: 1\nhours: 2\n"})

    assert check["valid"] is False and check["line"] == 2
    assert response.status_code == 400 and response.json()["line"] == 3


def test_saving_the_task_text_with_a_new_entity_and_comments(server, storage, monkeypatch):
    write_tasks(storage, COMMENTED_TASKS)
    monkeypatch.setattr(runner, "sync_task_discovery", lambda old, new: None)

    response = send("PUT", server + "api/tasks/energy/settings",
                    {"content": "# Lead note\nname: Energy  # shown in HA\nentities:\n  grid: sensor.grid_power\n  house: sensor.house\n"
                                "prompt: |\n  Grid {grid}.\nhours: 6  # six\n"})

    assert response.status_code == 200
    assert storage["TASKS_CONFIG_PATH"].read_text(encoding="utf-8") == COMMENTED_TASKS.replace(
        "  energy:\n    name:", "  energy:\n    # Lead note\n    name:").replace(
        '      grid: sensor.grid_power   # grid\n      pv: "sensor.pv_power"\n',
        "      grid: sensor.grid_power\n      house: sensor.house\n").replace(
        "    prompt: |\n      Grid {grid}, PV {pv}.\n\n      Keep it short.\n",
        "    prompt: |\n      Grid {grid}.\n    hours: 6  # six\n")
    assert (storage["CONFIG_DIR"] / "llm_tasks.yaml.bak").read_text(encoding="utf-8") == COMMENTED_TASKS
    assert runner.load_tasks()["energy"]["entities"]["house"] == "sensor.house"
    assert runner.load_tasks()["energy"]["hours"] == 6


@pytest.mark.parametrize("content, status", [
    ("prompt: [unclosed\n", 400),
    ("- a list\n", 400),
    ("", 400),
    ("prompt: x\nhours: abc\n", 400),
])
def test_bad_task_settings_are_rejected(server, storage, content, status):
    write_tasks(storage, COMMENTED_TASKS)

    assert send("PUT", server + "api/tasks/energy/settings", {"content": content}).status_code == status
    assert storage["TASKS_CONFIG_PATH"].read_text(encoding="utf-8") == COMMENTED_TASKS


def test_settings_of_an_unknown_task(server, storage):
    write_tasks(storage, COMMENTED_TASKS)

    assert send("PUT", server + "api/tasks/nope/settings", {"content": "prompt: x\n"}).status_code == 404


def test_yaml_syntax_check(server):
    assert send("POST", server + "api/yaml/check", {"content": "a: 1\nb: |\n  x\n"}).json() == {"valid": True}

    result = send("POST", server + "api/yaml/check", {"content": "a: 1\nb: [x\nc: 2\n"}).json()

    assert result["valid"] is False and result["line"] >= 2 and result["message"]

def test_deleting_a_task_removes_it_with_memory_and_audit_archives(server, storage, monkeypatch):
    write_tasks(storage, COMMENTED_TASKS)
    synced = []
    monkeypatch.setattr(runner, "sync_task_discovery", lambda old, new: synced.append((sorted(old), sorted(new))))
    runner.append_memory("energy", "Low", {})
    runner.append_memory("plants", "Water", {})
    runner.create_audit_archive("energy", "p", "r", {}, "[]", [], {})
    kept = runner.create_audit_archive("plants", "p", "r", {}, "[]", [], {})

    response = send("DELETE", server + "api/tasks/energy")

    assert response.status_code == 200
    assert response.json()["audit_archives_deleted"] == 1
    assert storage["TASKS_CONFIG_PATH"].read_text(encoding="utf-8") == "# My tasks\ntasks:\n" + COMMENTED_TASKS.split("\n\n", 2)[-1]
    assert synced == [(["energy", "plants"], ["plants"])]
    assert runner.load_memory("energy") == [] and runner.load_memory("plants")
    remaining = [a["name"] for a in requests.get(server + "api/audit", timeout=5).json()["archives"]]
    assert remaining == [kept.replace("\\", "/").rsplit("/", 1)[-1]]
    assert send("DELETE", server + "api/tasks/energy").status_code == 404


def test_deleting_a_later_task_keeps_the_comment_of_the_next_one(server, storage):
    write_tasks(storage, COMMENTED_TASKS.replace("  # Plants\n", "  # Water\n  water:\n    prompt: hi\n\n  # Plants\n"))

    assert send("DELETE", server + "api/tasks/water").status_code == 200
    assert storage["TASKS_CONFIG_PATH"].read_text(encoding="utf-8") == COMMENTED_TASKS


def test_running_task_cannot_be_deleted(server, storage):
    write_tasks(storage, COMMENTED_TASKS)
    runner.TASK_STATUS["energy"] = {"state": "running"}

    assert send("DELETE", server + "api/tasks/energy").status_code == 409
    assert storage["TASKS_CONFIG_PATH"].read_text(encoding="utf-8") == COMMENTED_TASKS


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


@pytest.mark.parametrize("content, problem", [
    ("import json\nimport pandas as pd\n\ndef process(df, config):\n    return json.dumps({})\n", None),
    ("try:\n    import not_installed_xyz\nexcept ImportError:\n    pass\n\ndef process(df, config):\n    pass\n", None),
    ("import os\nfrom not_installed_xyz.sub import thing\n\ndef process(df, config):\n    pass\n", "Module 'not_installed_xyz'"),
    ("def helper(df, config):\n    pass\n", "No top-level 'def process"),
    ("def process(df):\n    pass\n", "two arguments"),
    ("def process(df, config):\n    return (\n", "Syntax error"),
])
def test_processor_validation_without_saving(server, storage, content, problem):
    response = send("POST", server + "api/processors/check.py/validate", {"content": content})

    assert response.status_code == 200
    result = response.json()
    assert result["valid"] is (problem is None)
    if problem:
        assert problem in result["errors"][0]["message"]
    assert not (storage["PROCESSORS_DIR"] / "check.py").exists()


def test_processor_names_cannot_escape_the_folder_on_validate(server):
    assert send("POST", server + "api/processors/..%2Frun.py/validate", {"content": "x = 1\n"}).status_code in (400, 404)


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
