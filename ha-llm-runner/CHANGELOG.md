# Changelog

## 1.4.1 (2026-10-09)

### Changed

- Tasks no longer run when the add-on starts. They only run when triggered (button, web interface or MQTT), so restarts and updates cause no LLM calls and no extra memory entries. The sensors keep their last value across restarts.

## 1.4.0 (2026-10-09)

Existing `llm_tasks.yaml` files keep working unchanged. Your files are moved to the add-on's own folder automatically (see *Changed*).

### Added

- Web interface in the Home Assistant sidebar (**LLM Runner**): task status and last results, run buttons, an `llm_tasks.yaml` editor with validation, a processor editor and an audit archive browser.
- Every task with a prompt has a memory of its last 30 answers, also without `target_sensor:`. `{history}` works for every task, and the memory can be cleared in the web interface.
- `audit_retention_days` option (default 30).
- `language` option. Day and month names in prompts follow Home Assistant's language by default.
- `{date}`, `{time}`, `{month}` and `{year}` prompt placeholders.

### Changed

- The add-on keeps its files in its own folder (`/addon_configs/<id>_ha_llm_runner`), which is included in backups: `llm_tasks.yaml`, `processors/`, `memory/` and `audit/`. On the first start, `llm_tasks.yaml`, `scripts/processors/*.py` and `<target_sensor>_history.json` (also from `scripts/`) are copied from Home Assistant's configuration folder; the originals stay in place. Old processor paths and `/config/...` file paths keep working.
- `llm_tasks.yaml` may list the tasks at the top level without a `tasks:` key, as the original standalone script did.
- Home Assistant's configuration folder is mounted read-only.
- Audit archives are kept across add-on updates, deleted after `audit_retention_days` and excluded from backups.
- Saving `llm_tasks.yaml` in the web interface updates the sensors and buttons of new, renamed and removed tasks without a restart.
- Tasks run in the background, one at a time, so MQTT stays responsive during long LLM calls. Tasks no longer run again when the MQTT connection is re-established.
- The add-on starts even if the MQTT broker is not reachable yet, and keeps reconnecting.
- `{weekday}`, `{today}` and `{now}` are localised, e.g. `Freitag` and `9. Oktober` instead of `Friday` and `9 October`. `%A`, `%a`, `%B` and `%b` in `datetime_format` are localised as well.
- Entities, URLs and processor metrics named like a built-in placeholder (e.g. `today`) now take precedence over it.
- Loaded history is appended to the prompt as JSON when the prompt uses neither `{timeseries}` nor `{data}`, as in the original prototype. Set `hours: 0` to skip it.
- The log shows at startup whether the Home Assistant API is reachable.

### Fixed

- The add-on couldn't read any Home Assistant data (`401 Unauthorized`) and ignored Home Assistant's time zone: the base image's s6-overlay started the add-on without `SUPERVISOR_TOKEN` and `TZ`.

## 1.3.1

First release since 1.0.0. Existing `llm_tasks.yaml` files keep working unchanged.

### Added

- Provider layer for LLM APIs. Tasks can select one with `provider:` (default `gemini`) and `model:`.
- `files:` attaches any file type (images, audio, video, PDF, ...) from a URL or a path under `/config`, up to 20 MB.
- `calendar.*` entities, `entity:attribute` values and `urls:` as prompt inputs.
- `{timeseries}`, `{data}`, `{metrics}`, `{history}`, `{now}`, `{today}` and `{weekday}` prompt placeholders.
- `target_sensor:` keeps a history of the last 30 answers.
- Options `audit_archive`, `timezone` and `datetime_format`.
- Documentation with a task reference and examples.

### Changed

- Timestamps and time series use Home Assistant's time zone instead of UTC.
- Failed LLM calls no longer overwrite the last result.

### Fixed

- `data_processor:` scripts with a relative path were not found, so the default aggregation was used instead. They now run exactly once per task.

## 1.0.0

- Initial release: LLM tasks with MQTT discovery, sensor and Run button per task, MQTT triggers.
