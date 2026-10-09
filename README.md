# HA LLM Runner Add-on

A modular Home Assistant Add-on for orchestrating LLM data pipelines. It connects historical sensor data and local media to LLM APIs (like Google Gemini) and publishes the structured results as persistent entities via MQTT.

---

## Features

- **Persistent Across Reboots via MQTT Discovery:** Automatically provisions entities in Home Assistant with `retain: true`. No volatile REST states, eliminating data loss during host or core restarts.
- **Strictly Typed Structured Outputs:** Enforces valid JSON directly at the API level via Google Gemini, avoiding fragile regex or text parsing.
- **Full Recorder Integration:** Publishes primary states for immediate tracking while storing structured arrays (e.g., multi-day records) in entity attributes for long-term database storage.
- **Camera and File Attachments:** `camera.*` entities and snapshot/MJPEG URLs in `entities:` are attached as a single still frame. Anything listed under `files:` (images, audio, video, PDFs, ...) is attached as-is from an `http(s)://` URL or a local path (absolute or relative to `/config`), up to 20 MB per file. Credentials in a URL (`http://user:pass@host/...`) are sent via Digest auth.
- **Audit Archives (optional):** Each LLM task stores its prompt, response, values and attachments as a `.tar.gz` archive. Turn this off globally with the add-on option `audit_archive: false`, or per task with `audit: false`.
- **Time Zone and Date Format:** Timestamps, time series and the `{now}` / `{today}` / `{weekday}` prompt placeholders use the time zone configured in Home Assistant. Override it globally with the add-on option `timezone` (e.g. `Europe/Berlin`) and change the `{now}` / history format with `datetime_format` (default `%d.%m.%Y %H:%M:%S`).
- **Custom Local Processors:** Allows to integrate custom preprocessing pipelines from `/config/scripts/processors/` before feeding data to the LLM.

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
```

---

## Example Tasks

Tasks live in `/config/llm_tasks.yaml`. Each task gets a sensor (named after `name:`) and a **Run** button in Home Assistant via MQTT discovery. All tasks run once when the add-on starts. After that, a task runs whenever its button is pressed or a message is published to `ha_llm_runner/run/<task_id>`.

More real-world use cases, such as a smart doorbell that describes visitors from a camera snapshot, are collected in [EXAMPLES.md](EXAMPLES.md).

### 1. Simple text generation (no history)

Reads the current state of two entities, puts them into the prompt and stores the answer as the sensor state.

```yaml
tasks:
  morning_briefing:
    name: "Morning Briefing"
    entities:
      outdoor: sensor.outdoor_temperature   # current state -> {outdoor}
      weather: weather.home                 # current state -> {weather}, e.g. "rainy"
    hours: 0                                # don't load any history
    prompt: >-
      It is {weekday}, {now}. Outside it is {outdoor} °C and the weather is {weather}.
      Write a friendly morning briefing in at most two sentences (under 200 characters).
```

Result: `sensor.morning_briefing` holds the text (Home Assistant states are limited to 255 characters). The full answer is also stored in its `text` / `summary` attributes.

### 2. Time series analysis over the last 48 hours

The add-on loads the history of every entity from the Home Assistant recorder, averages it into 2-hour buckets and passes it to the LLM as JSON through `{timeseries}`. A `response_schema` makes the LLM return structured fields, which become sensor attributes.

```yaml
tasks:
  climate_analysis:
    name: "Living Room Climate"
    entities:
      living_room: sensor.living_room_temperature
      humidity: sensor.living_room_humidity
      outdoor: sensor.outdoor_temperature
    hours: 48          # history window loaded automatically (default: 24)
    resample: 2h       # average per 2 hours -> 24 rows per entity (default: 1h)
    temperature: 0.2
    prompt: |
      Analyse my living room climate over the last 48 hours.
      Current values: living room {living_room} °C, humidity {humidity} %, outdoor {outdoor} °C.

      Measurements (2-hour averages, JSON):
      {timeseries}

      Describe the indoor temperature trend and flag a mould risk if humidity stayed above 60 % for long periods.
      Set state to "warning" if anything needs attention, otherwise "ok".
    response_schema:
      type: object
      properties:
        state:
          type: string
          enum: [ok, warning]
        trend:
          type: string
          enum: [rising, falling, stable]
        summary:
          type: string
          description: At most 200 characters.
      required: [state, trend, summary]
```

Result: `sensor.living_room_climate` shows `ok` or `warning`, with `trend`, `summary` and `updated_at` as attributes. The prompt receives data like this:

```json
[{"timestamp":"2026-10-07T18:00:00+02:00","living_room":21.4,"humidity":58.2,"outdoor":12.1}, ...]
```

To run it on a schedule, publish to its topic from an automation:

```yaml
automation:
  - alias: "Climate analysis every morning"
    triggers:
      - trigger: time
        at: "07:00:00"
    actions:
      - action: mqtt.publish
        data:
          topic: ha_llm_runner/run/climate_analysis
          payload: RUN
```

### Prompt placeholders

| Placeholder | Content |
| --- | --- |
| `{<key>}` | Current state of each entry under `entities:` (use `sensor.x:attribute` for an attribute) |
| `{timeseries}` | History of all `entities:` as JSON (`hours:` window, `resample:` buckets) |
| `{now}`, `{today}`, `{weekday}` | Current date and time in Home Assistant's time zone |
| `{history}` | Previous answers of this task (requires `target_sensor:`, last `history_limit:` entries, default 7) |
| `{metrics}` | All current values as one JSON object |
| `{data}` | Output of the custom processor (see `data_processor:`), otherwise the same as `{metrics}` |

Literal braces must be doubled (`{{` / `}}`). If the prompt references an unknown placeholder, it is sent unformatted with the current values and the time series appended instead.


### Task reference

Every key a task in `llm_tasks.yaml` can use. Only `prompt:` is needed for an LLM call; all other keys are optional.

```yaml
tasks:
  <task_id>:            # used in MQTT topics and entity ids, e.g. climate_analysis
    # ... keys below
```

#### Sensor (MQTT discovery)

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | string | `LLM <Task Id>` | Name of the sensor. The button is called `Run <name>`. |
| `icon` | string | `mdi:brain` | Icon of the sensor. |
| `state_template` | string | `state`, else `status`, else `summary`, else `OK` | Jinja `value_template` that picks the sensor state from the result JSON, e.g. `"{{ value_json.trend }}"`. All result fields are always available as attributes. |
| `unit_of_measurement` | string | - | Passed to the discovered sensor, e.g. for a numeric `state_template`. |
| `device_class` | string | - | Passed to the discovered sensor. |
| `state_class` | string | - | Passed to the discovered sensor (e.g. `measurement`). |

#### Inputs

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `entities` | map `key: source` | - | Data sources, each available as `{key}` in the prompt. The source type is detected by its value: `sensor.x` (any entity) gives the current state, `sensor.x:attribute` gives one attribute, `calendar.x` gives the events of the next 14 days as text, and `camera.x` or an `http(s)://` URL attaches an image snapshot to the request. Plain entities and attributes also feed the history (see `hours:`). A plain list of entity ids is accepted too (keys become `0`, `1`, ...; no camera/calendar detection). |
| `files` | map `key: url-or-path` | - | Files of any type (image, audio, video, PDF, CSV, ...) attached to the request. Accepts `http(s)://` URLs (`user:pass@` credentials supported) or paths, relative ones resolved against `/config`. Max 20 MB each. Whether a type is understood depends on the model. |
| `urls` | map `key: url` | - | Web pages fetched as text (first 15,000 characters) into `{key}`. |

#### History and processing

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `hours` | int | `24` | History window loaded from the recorder for the entities. `0` disables history. |
| `resample` | string | `1h` | Bucket size for averaging the history ([pandas offset](https://pandas.pydata.org/docs/user_guide/timeseries.html#offset-aliases), e.g. `15min`, `2h`, `1D`). The result goes into `{timeseries}`. |
| `data_processor` | string | - | Custom Python script that replaces the default aggregation (see below). Searched in `/config`, `/config/scripts/processors` and next to `llm_tasks.yaml`; the `.py` suffix is optional. If the script fails, the default aggregation is used. |
| `processor` | string | - | Alias of `data_processor`. |

A processor exports one function. It receives the raw history as a `pandas.DataFrame` (one column per entity key, an empty frame if no history was loaded) and the task config. It must return a dict of metrics, which become prompt placeholders, plus any JSON-serializable data, which becomes `{data}` and `{timeseries}`:

```python
# /config/scripts/processors/solar_forecast.py
def process(df, config):
    daily = df.resample("1D").mean().round(1)
    return {"pv_avg": float(df["pv"].mean())}, daily.reset_index().to_dict("records")
```

#### LLM request

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `prompt` | string | - | Prompt template with placeholders (see above). Without a prompt, no LLM is called and the task only publishes the collected values (and processor data). |
| `provider` | string | `gemini` | LLM provider (see [LLM Providers](#llm-providers)). |
| `model` | string | provider option, e.g. `gemini_model` | Model for this task, overriding the add-on option. |
| `temperature` | float | `0.0` | Sampling temperature. |
| `response_schema` | JSON schema | - | Forces a JSON answer of this shape (JSON Schema: `type`, `properties`, `required`, `enum`, `items`, `description`, ...). Every field becomes a sensor attribute. Without a schema, the answer is stored as `text` and `summary`. |

#### Output

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `target_sensor` | entity id | - | Additionally writes the result to this entity via the HA REST API: the state is the timestamp, and the attributes are `text` and `history` (the last 30 answers). The history is kept in `/config/<target_sensor>_history.json` and enables `{history}`. |
| `friendly_name` | string | value of `target_sensor` | Friendly name of `target_sensor`. |
| `history_limit` | int | `7` | Number of previous answers inserted via `{history}`. |
| `audit` | bool | `true` if `prompt` is set | Stores the prompt, answer, values and attachments of every run in a ZIP archive. Ignored when the add-on option `audit_archive` is off. |

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

   The API key can also come from an upper-cased environment variable (e.g. `OPENAI_API_KEY`). Bump `version` in `config.yaml` after changing options.

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

---

## License
This project is licensed under the GNU General Public License v3.0.