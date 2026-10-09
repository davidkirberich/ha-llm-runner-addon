# Project Guidelines

Home Assistant add-on that runs LLM tasks defined in `/config/llm_tasks.yaml` over HA sensor history, cameras, files, calendars and URLs, and publishes the results via MQTT discovery. User-facing documentation (options, task reference, placeholders, examples) lives in [ha-llm-runner/DOCS.md](ha-llm-runner/DOCS.md), which Home Assistant shows on the add-on's Documentation tab. The root [README.md](README.md) is for GitHub: features, architecture, provider developer guide and tests. Read DOCS.md before changing task behaviour.

## Architecture

- [ha-llm-runner/run.py](ha-llm-runner/run.py): the runner. It loads tasks, collects inputs, aggregates history (`process_default`) or runs a custom `data_processor`, builds the prompt, calls the provider, then publishes over MQTT, writes `target_sensor` and creates an audit archive.
- [ha-llm-runner/llm_providers/](ha-llm-runner/llm_providers/): the provider layer. `run.py` only talks to `LLMProvider` / `LLMRequest` from `base.py` via `get_provider()`. Provider-specific code (URLs, payloads, auth) belongs in a provider class, never in `run.py`.
- [ha-llm-runner/config.yaml](ha-llm-runner/config.yaml) and [translations/en.yaml](ha-llm-runner/translations/en.yaml): add-on options, schema and UI labels.
- Container layout: `/app/run.py` + `/app/llm_providers`. The add-on only maps `config:rw`. `/media` and `/share` are not available.

## Build and Test

```powershell
pip install -r requirements-dev.txt
python -m pytest -q
```

Tests must not hit the network or a real Home Assistant. Stub the `run` / provider functions with `monkeypatch`, as in [tests/test_run.py](tests/test_run.py). [tests/conftest.py](tests/conftest.py) adds the add-on folder to `sys.path` and pins the time zone to UTC.

## Conventions

- **Backward compatibility is mandatory.** Existing user task YAMLs must keep working. Never rename or remove task keys, add-on options or prompt placeholders. Add new optional ones with defaults that preserve current behaviour.
- Add-on options for providers follow `<provider>_api_key` / `<provider>_model`. Use `api_key_option_names()` / `model_option_name()`; don't invent new patterns.
- New option: update `config.yaml` (`options` + `schema`), `translations/en.yaml` and the options table in `DOCS.md` together.
- New task key or placeholder: document it in the `DOCS.md` "Task reference" / "Prompt placeholders" tables.
- Behaviour change: bump `version` in `config.yaml` and add an entry to [ha-llm-runner/CHANGELOG.md](ha-llm-runner/CHANGELOG.md).
- `DOCS.md` is rendered inside Home Assistant, where relative links don't work. Use full GitHub URLs there.
- New Python module or package: add a `COPY` line to the [Dockerfile](ha-llm-runner/Dockerfile). New runtime dependency: add it to `ha-llm-runner/requirements.txt`. Images are Alpine-based, so prefer pure-Python packages.
- Dates and times: use `local_now(options)`, `format_datetime()` and `get_local_timezone(options)`. Never hard-code `UTC` or a specific zone such as `Europe/Berlin`.
- All code, comments, logs and prompts are in English.
- Failed LLM calls must raise, so the last good result and its history are not overwritten. Failing optional inputs log a warning and continue.
