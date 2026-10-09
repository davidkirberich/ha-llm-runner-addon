# HA LLM Runner

Runs LLM tasks over your Home Assistant data: current states, sensor history, camera snapshots, files, calendars and web pages. Results are published as sensors via MQTT discovery, so they survive restarts and are recorded like any other entity.

## Installation

1. Install and start the **Mosquitto broker** add-on, and set up the **MQTT** integration in Home Assistant.
2. Add this repository to the add-on store: **Settings > Add-ons > Add-on Store > ... > Repositories** and enter `https://github.com/davidkirberich/ha-llm-runner-addon`.
3. Install **HA LLM Runner**, enter your API key on the **Configuration** tab and start the add-on.
4. Open **LLM Runner** in the sidebar (or **Open Web UI** on the add-on page), add your tasks on the **llm_tasks.yaml** tab (see below) and save.

Upgrading from 1.3.x? Your files are copied to the new location automatically on the first start (see *Files and folders* below).

## Configuration

| Option | Default | Description |
| --- | --- | --- |
| `gemini_api_key` | - | API key from [Google AI Studio](https://aistudio.google.com/apikey). |
| `gemini_model` | `gemini-3.5-flash-lite` | Model for tasks without their own `model:`. |
| `audit_archive` | `true` | Store prompt, answer, values and attachments of each LLM run as a `.tar.gz` archive in the `audit/` folder (see *Files and folders* below). |
| `audit_retention_days` | `30` | Delete audit archives older than this many days. `0` keeps all archives. |
| `timezone` | Home Assistant's time zone | IANA time zone (e.g. `Europe/Berlin`) for timestamps, prompts and time series. |
| `language` | Home Assistant's language | Language of day and month names in `{weekday}`, `{today}`, `{date}`, `{time}`, `{month}` and `{now}` (e.g. `de`, `en-GB`, `fr`). |
| `datetime_format` | `%d.%m.%Y %H:%M:%S` | [strftime](https://strftime.org/) format for `{now}` and the `target_sensor` history. `%A`, `%a`, `%B` and `%b` follow `language`. |
| `mqtt_host` | `core-mosquitto` | MQTT broker host. |
| `mqtt_port` | `1883` | MQTT broker port. |
| `mqtt_user` / `mqtt_password` | - | MQTT credentials, if the broker requires them. |

## How tasks work

Tasks live in `llm_tasks.yaml` under a top-level `tasks:` key. For each task, the add-on creates a sensor (named after `name:`) and a **Run** button. Tasks never run on their own, not even when the add-on starts. A task runs:

- when its button is pressed,
- when **Run** is clicked in the web UI,
- when any message is published to `ha_llm_runner/run/<task_id>`,
- when its task id (or `all` for every task) is published to `ha_llm_runner/run`.

Tasks run one at a time in the background; a run that is requested while another task is running waits for it.

Changes to `llm_tasks.yaml` are picked up on the next run. When you save the file in the web UI, the sensors and buttons of new, renamed and removed tasks are updated right away. If you edit the file in another way, restart the add-on for that.

## Web interface

The add-on adds **LLM Runner** to the Home Assistant sidebar. It shows:

- **Tasks**: status, duration and errors of the last run, the last result and prompt, and the task's memory (with **Clear memory**). Every task can be run from here, also without an MQTT connection.
- **llm_tasks.yaml**: an editor with validation. Saving checks the YAML first, warns about unknown keys and missing processors, and keeps the previous version as `llm_tasks.yaml.bak`.
- **Processors**: create, edit and delete processor scripts. Saving checks the Python syntax.
- **Audit**: browse, view, download and delete audit archives, including the attached images.

The web interface is only reachable through Home Assistant (Ingress) and is available to administrators only. Processors are Python code that runs inside the add-on, so treat access to the add-on like admin access to Home Assistant.

## Files and folders

The add-on keeps its files in its own folder. In the add-on it is `/config`; from outside (Samba, SSH, File editor) it is `/addon_configs/<id>_ha_llm_runner` (`/app_configs/...` in newer Home Assistant versions), where `<id>` depends on the repository. It is included in Home Assistant backups, except `audit/`.

| Path | Content |
| --- | --- |
| `llm_tasks.yaml` | Task definitions. |
| `processors/` | Processor scripts (see `data_processor:`). |
| `memory/<task_id>.json` | The last 30 answers of each task (see `{history}`). |
| `audit/` | Audit archives `audit_<task_id>_<timestamp>.tar.gz`, deleted after `audit_retention_days`. |

Home Assistant's configuration folder is mounted read-only at `/homeassistant`, for example for `files:`.

**Upgrading from 1.3.x.** Up to 1.3.x, the files lived in Home Assistant's configuration folder. On the first start of 1.4.0, the add-on copies them, without overwriting anything that already exists:

- `/config/llm_tasks.yaml` to `llm_tasks.yaml`,
- `/config/scripts/processors/*.py` to `processors/`,
- `/config/<target_sensor>_history.json` or `/config/scripts/<target_sensor>_history.json` (where the original standalone script kept them) to `memory/<task_id>.json`.

The old files are left in place and can be deleted afterwards. Existing tasks keep working unchanged: processor paths and `/config/...` paths in `files:` that point into Home Assistant's configuration folder are still found. Task files from the original standalone script, which list the tasks at the top level without a `tasks:` key, are accepted as well. That script always used German day and month names; if Home Assistant's language is not German, set the `language` option to `de` to keep them.

## Example tasks

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

### 3. Smart doorbell: who is at the door?

When someone rings, the add-on grabs a snapshot from a Hikvision door station via its ISAPI endpoint and lets the LLM describe the visitor in one short sentence. Push it to your phone, or have an Amazon Echo announce it.

#### Task

```yaml
tasks:
  doorbell:
    name: "Front Door Visitor"
    icon: mdi:doorbell-video
    entities:
      # A snapshot URL is attached as an image (user:password@ is supported)
      front_door: "http://doorbell:password@192.168.10.41/ISAPI/Streaming/channels/101/picture"
    prompt: |
      You are a doorbell with voice output. Someone has just rung the bell.
      Describe in at most 20 words who is at the door, for a push notification.
      Recognise delivery services (DHL, Amazon, etc.) and the shipment size (letter, parcel).
      For unknown people, give gender and estimated age (e.g. "man, about 40").
      Ignore our own cars (white van, black BYD).
      If nobody is at the door, answer: "Nobody is at the door."
      No introductory phrases.
```

Result: `sensor.front_door_visitor` holds the answer, e.g. `DHL courier with a medium-sized parcel.`

#### Automation

Replace the doorbell trigger and the notify service with your own entities:

```yaml
automation:
  - alias: "Doorbell: describe visitor"
    triggers:
      - trigger: state
        entity_id: binary_sensor.front_door_doorbell
        to: "on"
    actions:
      - action: mqtt.publish
        data:
          topic: ha_llm_runner/run/doorbell
          payload: RUN
      - wait_for_trigger:
          - trigger: state
            entity_id: sensor.front_door_visitor
        timeout: "00:00:30"
      - action: notify.mobile_app_my_phone
        data:
          title: "Doorbell"
          message: "{{ states('sensor.front_door_visitor') }}"
```

For an Echo announcement, add a second action with your Alexa notify service (e.g. from the Alexa Media Player integration).

#### Notes

- The password is stored in plain text in `llm_tasks.yaml`, so use a dedicated view-only camera user. Special characters must be URL-encoded (`@` becomes `%40`).
- Any other camera works the same way: use a `camera.*` entity or its snapshot URL under `entities:`.
- Want the message in German? Add `Answer in German.` to the prompt.

## Prompt placeholders

| Placeholder | Content |
| --- | --- |
| `{<key>}` | Current state of each entry under `entities:` (use `sensor.x:attribute` for an attribute) |
| `{timeseries}` | History of all `entities:` as JSON (`hours:` window, `resample:` buckets) |
| `{history}` | Previous answers of this task, newest first, separated by `---` (last `history_limit:` entries, default 7) |
| `{metrics}` | All current values as one JSON object |
| `{data}` | Output of the custom processor (see `data_processor:`), otherwise the same as `{metrics}` |

Built-in date and time placeholders use Home Assistant's time zone and language (or the `timezone` / `language` options). Examples for Friday, 9 October 2026, 16:24:

| Placeholder | `de` | `en` | `en-GB` |
| --- | --- | --- | --- |
| `{weekday}` | Freitag | Friday | Friday |
| `{today}` | 9. Oktober | October 9 | 9 October |
| `{date}` | 9. Oktober 2026 | October 9, 2026 | 9 October 2026 |
| `{time}` | 16:24 | 4:24 PM | 16:24 |
| `{month}` | Oktober | October | October |
| `{year}` | 2026 | 2026 | 2026 |
| `{now}` | `datetime_format`, e.g. 09.10.2026 16:24:10 | | |

An entry under `entities:` (or a processor metric) with the same name, e.g. `month`, takes precedence over the built-in placeholder.

If history was loaded (`hours:` greater than 0 and at least one plain entity) or a processor returned data, but the prompt uses neither `{timeseries}` nor `{data}`, the time series is appended to the prompt as a `MEASUREMENTS (JSON)` block. Set `hours: 0` if a task doesn't need the history.

Literal braces must be doubled (`{{` / `}}`). If the prompt references an unknown placeholder, it is sent unformatted with the current values (and the time series) appended instead.

## Task reference

Every key a task in `llm_tasks.yaml` can use. Only `prompt:` is needed for an LLM call; all other keys are optional.

```yaml
tasks:
  <task_id>:            # used in MQTT topics and entity ids, e.g. climate_analysis
    # ... keys below
```

### Sensor (MQTT discovery)

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | string | `LLM <Task Id>` | Name of the sensor. The button is called `Run <name>`. |
| `icon` | string | `mdi:brain` | Icon of the sensor. |
| `state_template` | string | `state`, else `status`, else `summary`, else `OK` | Jinja `value_template` that picks the sensor state from the result JSON, e.g. `"{{ value_json.trend }}"`. All result fields are always available as attributes. |
| `unit_of_measurement` | string | - | Passed to the discovered sensor, e.g. for a numeric `state_template`. |
| `device_class` | string | - | Passed to the discovered sensor. |
| `state_class` | string | - | Passed to the discovered sensor (e.g. `measurement`). |

### Inputs

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `entities` | map `key: source` | - | Data sources, each available as `{key}` in the prompt. The source type is detected by its value: `sensor.x` (any entity) gives the current state, `sensor.x:attribute` gives one attribute, `calendar.x` gives the events of the next 14 days as text, and `camera.x` or an `http(s)://` URL attaches an image snapshot to the request. Snapshot URLs are fetched directly from the camera (`user:pass@` is sent as Digest auth, MJPEG streams are supported); prefer them when a `camera.x` entity doesn't deliver reliable stills, e.g. a Hikvision door station's `/ISAPI/Streaming/channels/101/picture`. Plain entities and attributes also feed the history (see `hours:`). A plain list of entity ids is accepted too (keys become `0`, `1`, ...; no camera/calendar detection). |
| `files` | map `key: url-or-path` | - | Files of any type (image, audio, video, PDF, CSV, ...) attached to the request. Accepts `http(s)://` URLs (`user:pass@` credentials supported) or paths. Relative paths are looked up in Home Assistant's configuration folder first, then in the add-on folder. Max 20 MB each. Whether a type is understood depends on the model. |
| `urls` | map `key: url` | - | Web pages fetched as text (first 15,000 characters) into `{key}`. |

### History and processing

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `hours` | int | `24` | History window loaded from the recorder for the entities. `0` disables history. |
| `resample` | string | `1h` | Bucket size for averaging the history ([pandas offset](https://pandas.pydata.org/docs/user_guide/timeseries.html#offset-aliases), e.g. `15min`, `2h`, `1D`). The result goes into `{timeseries}`, or is appended to the prompt if the prompt doesn't use it. |
| `data_processor` | string | - | Custom Python script that replaces the default aggregation (see below). Searched in the add-on's `processors/` folder first, then in the pre-1.4.0 locations (`/config/scripts/processors` and `/config` of Home Assistant); the `.py` suffix is optional, so `solar_forecast` is enough. If the script fails, the default aggregation is used. |
| `processor` | string | - | Alias of `data_processor`. |

A processor exports one function. It receives the raw history as a `pandas.DataFrame` (one column per entity key, an empty frame if no history was loaded) and the task config. It must return a dict of metrics, which become prompt placeholders, plus any JSON-serializable data, which becomes `{data}` and `{timeseries}`:

```python
# processors/solar_forecast.py
def process(df, config):
    daily = df.resample("1D").mean().round(1)
    return {"pv_avg": float(df["pv"].mean())}, daily.reset_index().to_dict("records")
```

### LLM request

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `prompt` | string | - | Prompt template with placeholders (see above). Without a prompt, no LLM is called and the task only publishes the collected values (and processor data). |
| `provider` | string | `gemini` | LLM provider (see *LLM providers* below). |
| `model` | string | provider option, e.g. `gemini_model` | Model for this task, overriding the add-on option. |
| `temperature` | float | `0.0` | Sampling temperature. |
| `response_schema` | JSON schema | - | Forces a JSON answer of this shape (JSON Schema: `type`, `properties`, `required`, `enum`, `items`, `description`, ...). Every field becomes a sensor attribute. Without a schema, the answer is stored as `text` and `summary`. |

### Output

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `target_sensor` | entity id | - | Additionally writes the result to this entity via the HA REST API: the state is the timestamp, and the attributes are `text` and `history` (the task's memory, the last 30 answers). |
| `friendly_name` | string | value of `target_sensor` | Friendly name of `target_sensor`. |
| `history_limit` | int | `7` | Number of previous answers inserted via `{history}`. Every task with a `prompt` keeps its last 30 answers (or `history_limit`, if larger) in `memory/<task_id>.json`, with or without `target_sensor`. Failed runs are not added. |
| `audit` | bool | `true` if `prompt` is set | Stores the prompt, answer, values and attachments of every run in a `.tar.gz` archive in `audit/`. Ignored when the add-on option `audit_archive` is off. |

## LLM providers

| Provider | `provider:` value | API key option |
| --- | --- | --- |
| Google Gemini | `gemini` (default) | `gemini_api_key` |

Each task can choose its own model with `model:`. Images, PDFs and other attachments as well as `response_schema` are supported. Which file types a model understands depends on the model.

If an LLM call fails (missing API key, quota, network error), the error is logged and the task's sensor keeps its previous state and history.

More providers can be added in code; see the [developer guide](https://github.com/davidkirberich/ha-llm-runner-addon#adding-a-new-provider-eg-openaiprovider-qwenprovider).
