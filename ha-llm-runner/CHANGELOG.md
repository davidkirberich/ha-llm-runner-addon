# Changelog

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
