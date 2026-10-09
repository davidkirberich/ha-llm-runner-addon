# Project Guidelines

Home Assistant add-on that runs LLM tasks defined in `llm_tasks.yaml` (in the add-on's own config folder) over HA sensor history, cameras, files, calendars and URLs, and publishes the results via MQTT discovery. User-facing documentation (options, task reference, placeholders, examples) lives in [ha-llm-runner/DOCS.md](ha-llm-runner/DOCS.md), which Home Assistant shows on the add-on's Documentation tab. The root [README.md](README.md) is for GitHub: features, architecture, provider developer guide and tests. Read DOCS.md before changing task behaviour.

## Architecture

- [ha-llm-runner/run.py](ha-llm-runner/run.py): the runner. It loads tasks, collects inputs, aggregates history (`process_default`) or runs a custom `data_processor`, builds the prompt, calls the provider, then publishes over MQTT, writes `target_sensor` and creates an audit archive.
- [ha-llm-runner/llm_providers/](ha-llm-runner/llm_providers/): the provider layer. `run.py` only talks to `LLMProvider` / `LLMRequest` from `base.py` via `get_provider()`. Provider-specific code (URLs, payloads, auth) belongs in a provider class, never in `run.py`.
- [ha-llm-runner/web.py](ha-llm-runner/web.py) + [web/index.html](ha-llm-runner/web/index.html): Ingress web UI (stdlib `ThreadingHTTPServer`, vanilla JS, no build step). `web.py` gets the runner module passed in and calls its functions; keep task logic in `run.py`. Requests are only accepted from the Ingress proxy (`172.30.32.2`) and localhost; state-changing requests must be JSON. UI URLs must stay relative because of the Ingress path prefix.
- [ha-llm-runner/config.yaml](ha-llm-runner/config.yaml) and [translations/en.yaml](ha-llm-runner/translations/en.yaml): add-on options, schema and UI labels.
- Container layout: `/app/run.py`, `/app/web.py`, `/app/web/` + `/app/llm_providers`. Maps `addon_config:rw` (the add-on's own folder at `/config`: `llm_tasks.yaml`, `processors/`, `memory/`, `audit/`) and `homeassistant_config:ro` (HA config at `/homeassistant`). Always use the path constants in `run.py` (`CONFIG_DIR`, `TASKS_CONFIG_PATH`, `MEMORY_DIR`, ...). Pre-1.4.0 `/config/...` paths meant the HA config and must keep resolving (`legacy_path_candidates`). `/media` and `/share` are not available.

## Build and Test

```powershell
pip install -r requirements-dev.txt
python -m pytest -q
```

Tests must not hit the network or a real Home Assistant. Stub the `run` / provider functions with `monkeypatch`, as in [tests/test_run.py](tests/test_run.py). [tests/conftest.py](tests/conftest.py) adds the add-on folder to `sys.path` and empties the cached Home Assistant config, so the time zone is UTC and the language English. Its autouse `storage` fixture points all path constants to `tmp_path` and resets the runner state, so tests never touch `/config`. Web API tests in [tests/test_web.py](tests/test_web.py) start a real server on a free port.

## Conventions

- **Backward compatibility is mandatory.** Existing user task YAMLs must keep working. Never rename or remove task keys, add-on options or prompt placeholders. Add new optional ones with defaults that preserve current behaviour.
- Add-on options for providers follow `<provider>_api_key` / `<provider>_model`. Use `api_key_option_names()` / `model_option_name()`; don't invent new patterns.
- New option: update `config.yaml` (`options` + `schema`), `translations/en.yaml` and the options table in `DOCS.md` together.
- New task key or placeholder: document it in the `DOCS.md` "Task reference" / "Prompt placeholders" tables.
- Behaviour change: bump `version` in `config.yaml` and add an entry to [ha-llm-runner/CHANGELOG.md](ha-llm-runner/CHANGELOG.md).
- `DOCS.md` is rendered inside Home Assistant, where relative links don't work. Use full GitHub URLs there.
- New Python module or package: add a `COPY` line to the [Dockerfile](ha-llm-runner/Dockerfile). New runtime dependency: add it to `ha-llm-runner/requirements.txt`. Images are Alpine-based, so prefer pure-Python packages.
- Dates and times: use `local_now(options)`, `format_datetime()` and `get_local_timezone(options)`. Never hard-code `UTC` or a specific zone such as `Europe/Berlin`.
- Day and month names: use `get_locale(options)` with Babel (`format_date` etc.) or `builtin_placeholders()`. Never hard-code English names; new date placeholders belong in `builtin_placeholders()`.
- All code, comments, logs and prompts are in English.
- Failed LLM calls must raise, so the last good result and its history are not overwritten. Failing optional inputs log a warning and continue.
