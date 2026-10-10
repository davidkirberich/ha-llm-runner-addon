# Changelog

## Unreleased

### Added

- Complete Svelte 5 / TypeScript web interface: Tasks with New/Remove, run actions, results, prompts and memory; full configuration editing; Processor New/Validate/Save/Delete; and Audit filtering, viewing, attachments, downloads and deletion.
- Reproducible frontend build and checks with Vite; static assets are compiled in a separate Docker build stage without adding Node to the runtime.
- Svelte task Config tab: whole-task YAML editor, live entity table and search with Add/Remove, syntax validation, Save/Discard, Ctrl+S and unsaved Preview via Ctrl+Enter. Drafts survive polling; stale entity edits cannot overwrite newer text, and edits made during saving remain unsaved.

### Changed

- Svelte is now the default and only web interface; the former hand-written DOM UI is removed. The Python API remains unchanged. Development checkouts must build the frontend first; the Docker image builds it automatically.
- Audit uses dependent Task and Call dropdowns instead of archive buttons. Calls are sorted newest first and limited to the latest 50 per task; changing tasks clears the selected call and its contents.
- New task and processor names are entered directly in the interface rather than native browser prompt dialogs.
- Processor selection uses a left sidebar on desktop and stacks above the editor on small screens. File editor line/column indicators appear in the top toolbar.
- Task action success messages use a green background and disappear after five seconds or when switching main tabs; they can still be dismissed manually.
- File validation results appear beside Validate, with errors and warnings above the editor. Processor validation again describes the compilation, process(df, config) and import checks; configuration validation shows the task count.

## 1.4.2 (2026-10-10)

### Added

- Web interface: each task has a **Details** button next to **Run**. It opens the task below the list with the tabs **Details** (last result and prompt), **Memory** and **Config**.
- **Config** tab: lists the entities, files and URLs a task reads with name and current value from Home Assistant, so typos and `unavailable` sensors stand out. A click on an entity ID opens the entity's Home Assistant dialog. Below it, a search over all Home Assistant entities adds an entity with an editable alias to the task's `entities:`; **Remove** takes an entity, file or URL out again.
- **Config** tab: **Task YAML** shows the whole task as written in `llm_tasks.yaml` (including entities, files, URLs and comments) and can be edited directly, e.g. to copy it into a chat assistant and paste the refined version back. The entity table follows the editor text live, **Add**/**Remove** only change the editor text, and **Save** writes the task. A ✓ / ✗ shows live whether the YAML syntax is valid; saving keeps the rest of `llm_tasks.yaml` unchanged, refuses results with errors and saves the previous version as `llm_tasks.yaml.bak`.
- **Config** tab: **Preview** (or Ctrl+Enter in the Task YAML) runs the unsaved task text once and shows the result, the duration and the prompt as sent, or the error with traceback, below the editor. It is a real LLM call, but writes no memory, no audit archive, no `target_sensor` and publishes nothing via MQTT.
- Tasks tab: **New** creates a task with only an example `prompt` (no `name`) and opens its **Config** tab. A red **Remove** in the task details deletes a task after a confirmation, together with its Home Assistant sensor, its memory and its audit archives.
- Processors tab: **Validate** checks a processor without running it (compiles, has `process(df, config)`, all imported modules are installed). Saving runs the same checks and shows problems as warnings.

### Changed

- Entity history is now opt-in: an omitted `hours` defaults to `0` instead of `24`, in both normal runs and Preview. Set `hours` to a positive number to include recorder history; current entity values and task memory (`{history}`) are unchanged.
- Clicking a task row no longer opens the task; use **Details**.
- The task list and the task details show the task ID from `llm_tasks.yaml` instead of `name` (`name` is still the name of the sensor in Home Assistant).
- Processors tab: the list shows only the file name and how many tasks use it; the task names are shown above the editor.
- Task details and preview: free-text answers are shown as readable text; the full result (with the duplicate `text`/`summary` attributes and `updated_at`) is folded away under "Raw result".

### Fixed

- Passwords in camera, file and URL targets (`user:pass@`) no longer appear in the add-on log or in error texts passed to the LLM; they are shown as `***@`. The **Config** tab shows the targets exactly as written in `llm_tasks.yaml` (the web interface is for Home Assistant admins only).
- Saving or previewing a task in the **Config** tab no longer fails with "could not be put back into llm_tasks.yaml unchanged" when its last multi-line value (e.g. `prompt: |`) is followed by an indented, otherwise empty line. Such a line belonged to the value in YAML; it is now removed when the task is saved. The same error no longer appears when the task text ends without a final line break.
- A task without `prompt` (data-only, no LLM call) no longer downloads its camera images and `files`, they were never used. The configuration check warns about them, and **Preview** shows "No prompt" with a hint instead of a green "OK".

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
