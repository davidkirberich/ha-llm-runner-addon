# HA LLM Runner Add-on

A Home Assistant add-on that runs LLM tasks over sensor history, camera snapshots, files, calendars and web pages, and publishes the results as persistent entities via MQTT.

**User documentation:** [ha-llm-runner/DOCS.md](ha-llm-runner/DOCS.md) (also shown on the add-on's **Documentation** tab in Home Assistant). It covers configuration, all task keys, prompt placeholders and examples.

---

## Installation

[![Add repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fdavidkirberich%2Fha-llm-runner-addon)

Or add `https://github.com/davidkirberich/ha-llm-runner-addon` manually under **Settings > Add-ons > Add-on Store > ... > Repositories**, then install **HA LLM Runner**.

---

## Features

- **Persistent Across Reboots via MQTT Discovery:** Automatically provisions entities in Home Assistant with `retain: true`. No volatile REST states, eliminating data loss during host or core restarts.
- **Strictly Typed Structured Outputs:** Enforces valid JSON directly at the API level via Google Gemini, avoiding fragile regex or text parsing.
- **Full Recorder Integration:** Publishes primary states for immediate tracking while storing structured arrays (e.g., multi-day records) in entity attributes for long-term database storage.
- **Camera and File Attachments:** `camera.*` entities and snapshot/MJPEG URLs in `entities:` are attached as a single still frame. Anything listed under `files:` (images, audio, video, PDFs, ...) is attached as-is from an `http(s)://` URL or a local path (absolute, or relative to Home Assistant's configuration folder), up to 20 MB per file. Credentials in a URL (`http://user:pass@host/...`) are sent via Digest auth.
- **Web Interface (Ingress):** A sidebar panel shows task status and results, runs tasks, and edits `llm_tasks.yaml` and processors with validation. It also browses audit archives.
- **Task Memory:** Every task remembers its last 30 answers. `{history}` feeds them back into the prompt, e.g. so a daily briefing doesn't suggest the same recipe twice.
- **Audit Archives (optional):** Each LLM task stores its prompt, response, values and attachments as a `.tar.gz` archive, kept for `audit_retention_days` (default 30) and excluded from backups. Turn this off globally with the add-on option `audit_archive: false`, or per task with `audit: false`.
- **Time Zone, Language and Date Format:** Timestamps, time series and the `{now}` / `{today}` / `{weekday}` / `{month}` prompt placeholders use the time zone and language configured in Home Assistant. Override them with the add-on options `timezone` (e.g. `Europe/Berlin`) and `language` (e.g. `de`), and change the `{now}` / history format with `datetime_format` (default `%d.%m.%Y %H:%M:%S`).
- **Custom Local Processors:** Allows to integrate custom preprocessing pipelines from the add-on's `processors/` folder before feeding data to the LLM.

---

## Architecture

```text
[ Home Assistant Core ] 
       │  (History REST API: Read-only)
       ▼
[ HA LLM Runner (Container) ] ◄──► [ LLM API (e.g. Gemini) ] (Structured JSON)
       │
       ▼  (MQTT Discovery + Retain)
[ Mosquitto Broker ] ────► [ Home Assistant State Machine & Recorder ]

[ HA sidebar (Ingress) ] ──► [ web.py: status, editors, audit browser ]
```

| Path | Purpose |
| --- | --- |
| [`ha-llm-runner/run.py`](ha-llm-runner/run.py) | Runner: loads tasks, collects inputs, builds prompts, publishes results, task memory and storage migration |
| [`ha-llm-runner/web.py`](ha-llm-runner/web.py) | Ingress web server and JSON API for the sidebar panel |
| [`ha-llm-runner/web/`](ha-llm-runner/web/) | Web interface (single static `index.html`) |
| [`ha-llm-runner/llm_providers/`](ha-llm-runner/llm_providers/) | Provider layer (one class per LLM API) |
| [`ha-llm-runner/config.yaml`](ha-llm-runner/config.yaml) | Add-on manifest, options and schema |
| [`ha-llm-runner/DOCS.md`](ha-llm-runner/DOCS.md) | User documentation (Documentation tab) |
| [`ha-llm-runner/CHANGELOG.md`](ha-llm-runner/CHANGELOG.md) | Release notes |
| [`tests/`](tests/) | pytest suite |

---

## LLM Providers

All model calls go through a small provider layer in [`ha-llm-runner/llm_providers/`](ha-llm-runner/llm_providers/). The runner only builds a provider-neutral `LLMRequest` (prompt, model, temperature, optional JSON schema, base64 attachments such as camera snapshots or PDFs). Each provider turns that into its own API call.

| Provider | `provider:` value | API key option | Notes |
| --- | --- | --- | --- |
| `GeminiProvider` | `gemini` (default) | `gemini_api_key` / `GEMINI_API_KEY` | REST `generateContent`; supports images/PDFs, temperature and `response_schema` |

A task picks its provider with the optional `provider:` key. Tasks without it keep using Gemini, so existing `llm_tasks.yaml` files work unchanged:

```yaml
tasks:
  climate_analysis:
    provider: gemini               # optional, defaults to gemini
    model: gemini-3.5-flash-lite   # optional, defaults to the add-on option gemini_model
    entities:
      living_room: sensor.living_room_temperature
    prompt: "..."
```

If an LLM call fails (missing API key, quota, network error), the task is aborted and logged. The previous sensor state and its history stay untouched.

### Adding a new provider (e.g. `OpenAIProvider`, `QwenProvider`)

1. Create a module in `ha-llm-runner/llm_providers/`, for example `openai.py`, and subclass `LLMProvider`. You only have to implement `generate_text()`, which returns the raw answer text. JSON parsing, markdown-fence stripping and the plain-text fallback are handled by the base class in `generate()`.

   ```python
   import requests

   from .base import LLMProvider, LLMRequest


   class OpenAIProvider(LLMProvider):
       name = "openai"                        # value used in a task's `provider:` field
       default_model = "gpt-5-mini"           # optional; options `openai_api_key` / `openai_model` are read by convention
       endpoint = "https://api.openai.com/v1/chat/completions"

       def generate_text(self, request: LLMRequest) -> str:
           content = [{"type": "text", "text": request.prompt}]
           for data, mime in request.attachments:
               content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})

           payload = {
               "model": request.model,
               "temperature": request.temperature,
               "messages": [{"role": "user", "content": content}],
           }
           if request.schema:
               payload["response_format"] = {
                   "type": "json_schema",
                   "json_schema": {"name": "result", "schema": request.schema},
               }

           res = requests.post(
               self.endpoint,
               headers={"Authorization": f"Bearer {self.api_key}"},
               json=payload,
               timeout=self.timeout,
           )
           res.raise_for_status()
           return res.json()["choices"][0]["message"]["content"]
   ```

   Providers with an OpenAI-compatible endpoint, such as Alibaba Qwen via DashScope, can subclass `OpenAIProvider` and override only `name`, `default_model` and `endpoint`. Check each vendor's docs for which features (images, JSON schema) a given model supports.

2. Register the class in [`llm_providers/__init__.py`](ha-llm-runner/llm_providers/__init__.py):

   ```python
   from .openai import OpenAIProvider

   PROVIDERS = {
       GeminiProvider.name: GeminiProvider,
       OpenAIProvider.name: OpenAIProvider,
   }
   ```

3. Add the provider's options to [`config.yaml`](ha-llm-runner/config.yaml) under both `options` and `schema`, and describe them in [`translations/en.yaml`](ha-llm-runner/translations/en.yaml). Option names follow the convention `<provider>_api_key` and `<provider>_model`, so they are picked up automatically and existing options never need to be renamed:

   ```yaml
   options:
     openai_api_key: ""
     openai_model: ""
   schema:
     openai_api_key: password
     openai_model: str?
   ```

   The API key can also come from an upper-cased environment variable (e.g. `OPENAI_API_KEY`). Then add the provider to the options and providers tables in [`DOCS.md`](ha-llm-runner/DOCS.md), bump `version` in `config.yaml` and add an entry to [`CHANGELOG.md`](ha-llm-runner/CHANGELOG.md).

4. Add tests next to [`tests/test_providers.py`](tests/test_providers.py). Mock `requests.post` and assert on the payload your provider builds and the text it extracts. Tests must never call a real API.

5. Use it in a task with `provider: openai` and a matching `model:`. Without an explicit `model:`, the task falls back to the `<provider>_model` option and then to the provider's `default_model`.

---

## Testing

Run the automated checks before committing changes:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

`requirements-dev.txt` also installs the add-on runtime dependencies from `ha-llm-runner/requirements.txt`. All network calls to Home Assistant and LLM APIs are mocked.

### Testing on your own Home Assistant

[`scripts/deploy-local.ps1`](scripts/deploy-local.ps1) copies the working copy to Home Assistant as a local add-on and installs, updates or rebuilds it there. It needs the **Advanced SSH & Web Terminal** add-on with your public key in `authorized_keys`:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\deploy-local.ps1 -HostName homeassistant.local
```

`-CheckOnly` only tests the connection. The local add-on (slug `local_ha_llm_runner`) has its own config folder. Stop the GitHub-installed version while testing, because both use the same MQTT topics and entities.

To go back to the store version:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\deploy-local.ps1 -Remove -Transfer
```

`-Remove` uninstalls the local add-on. `-Transfer` first updates the store version if needed, then copies the local add-on's `llm_tasks.yaml`, `processors/`, `memory/`, `audit/` and options to it. It stops without changing anything if the store version already has an `llm_tasks.yaml`. Without `-Transfer`, the local add-on's config folder is kept.

---

## License
This project is licensed under the GNU General Public License v3.0.
