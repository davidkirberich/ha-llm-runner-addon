import os
import sys
import re
import json
import base64
import io
import tarfile
import logging
import importlib.util
import mimetypes
from urllib.parse import urlparse, unquote
from datetime import datetime, timezone, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
import requests
from requests.auth import HTTPDigestAuth
import pandas as pd
import paho.mqtt.client as mqtt

from llm_providers import LLMRequest, get_provider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("ha_llm_runner")

OPTIONS_PATH = "/data/options.json"
CONFIG_DIR = "/config"
TASKS_CONFIG_PATH = "/config/llm_tasks.yaml"
PROCESSORS_DIR = "/config/scripts/processors"

HA_URL = os.environ.get("SUPERVISOR_URL", "http://supervisor/core")
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

DEFAULT_DATETIME_FORMAT = "%d.%m.%Y %H:%M:%S"
_ha_time_zone: str | None = None


def get_ha_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HA_TOKEN") or ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_ha_time_zone() -> str:
    global _ha_time_zone
    if _ha_time_zone is None:
        try:
            r = requests.get(f"{HA_URL}/api/config", headers=get_ha_headers(), timeout=10)
            r.raise_for_status()
            _ha_time_zone = r.json().get("time_zone") or ""
        except Exception as e:
            logger.warning(f"Could not read time zone from Home Assistant: {e}")
            return ""
    return _ha_time_zone


def get_local_timezone(options: dict | None = None) -> tzinfo:
    """Option `timezone` > TZ set by the Supervisor (HA's configured zone) > HA /api/config > UTC."""
    options = load_options() if options is None else options
    sources = (
        ("add-on option 'timezone'", lambda: options.get("timezone")),
        ("TZ environment variable", lambda: os.environ.get("TZ")),
        ("Home Assistant config", fetch_ha_time_zone),
    )
    for source, read in sources:
        name = str(read() or "").strip()
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning(f"Ignoring unknown time zone '{name}' from {source}.")
    return timezone.utc


def local_now(options: dict | None = None) -> datetime:
    return datetime.now(get_local_timezone(options))


def format_datetime(value: datetime, options: dict | None = None) -> str:
    options = load_options() if options is None else options
    return value.strftime(options.get("datetime_format") or DEFAULT_DATETIME_FORMAT)

def fetch_external_url(url: str, max_chars: int = 15000) -> str:
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        text = r.text
        if len(text) > max_chars:
            logger.warning(f"Payload from {url} was truncated (> {max_chars} chars)")
            return text[:max_chars] + "\n... [TRUNCATED]"
        return text
    except Exception as e:
        logger.warning(f"Fetch of external URL {url} failed: {e}")
        return f"ERROR: {e}"


def fetch_ha_state(entity_spec: str) -> str:
    entity_id, _, attr = entity_spec.partition(":")
    if not entity_id:
        return "not available"
    url = f"{HA_URL}/api/states/{entity_id}"
    try:
        r = requests.get(url, headers=get_ha_headers(), timeout=10)
        if r.status_code != 200:
            return f"Error ({r.status_code})"
        data = r.json()
        attributes = data.get("attributes", {})
        if attr:
            val = attributes.get(attr, data.get("state"))
            return str(val) if val is not None else "not available"
        for candidate in ["text_summary", "text", "summary", "description"]:
            if candidate in attributes and attributes[candidate]:
                return str(attributes[candidate])
        state = data.get("state")
        return str(state) if state not in ["unavailable", "unknown", None] else "not available"
    except Exception as e:
        logger.warning(f"Live state fetch for {entity_id} failed: {e}")
        return "not available"


MAX_FILE_BYTES = 20 * 1024 * 1024
GENERIC_MIME_TYPES = {"", "application/octet-stream", "binary/octet-stream", "application/binary"}
# Built-in table only, so results don't depend on host files or the Windows registry
MIME_TYPES = mimetypes.MimeTypes()


def is_http_url(target: str) -> bool:
    return target.startswith("http://") or target.startswith("https://")


def split_url_credentials(target: str) -> tuple[str, HTTPDigestAuth | None]:
    """Moves `user:pass@` out of the URL into Digest auth (common for IP cameras and NAS shares)."""
    parsed = urlparse(target)
    if not (parsed.username and parsed.password):
        return target, None
    netloc = parsed.hostname or parsed.netloc
    if parsed.port:
        netloc += f":{parsed.port}"
    auth = HTTPDigestAuth(unquote(parsed.username), unquote(parsed.password))
    return parsed._replace(netloc=netloc).geturl(), auth


def guess_mime_type(name: str, reported: str = "") -> str:
    reported = reported.split(";")[0].strip().lower()
    if reported not in GENERIC_MIME_TYPES:
        return reported
    guessed, _ = MIME_TYPES.guess_type(urlparse(name).path if is_http_url(name) else name)
    return guessed or "application/octet-stream"


def fetch_camera_snapshot(target: str) -> tuple[str, str]:
    """Single still frame from a `camera.*` entity or a snapshot/MJPEG URL."""
    if is_http_url(target):
        url, auth = split_url_credentials(target)
        headers = {"User-Agent": "Mozilla/5.0"}
    else:
        url, auth = f"{HA_URL}/api/camera_proxy/{target}", None
        headers = get_ha_headers()

    with requests.get(url, headers=headers, auth=auth, stream=True, timeout=15) as r:
        r.raise_for_status()
        content_type = r.headers.get("Content-Type", "")

        if "image/" in content_type:
            raw_bytes = r.content
            mime_type = content_type.split(";")[0].strip()
        elif "multipart" in content_type:
            boundary_match = re.search(r"boundary=([^;]+)", content_type)
            boundary = (boundary_match.group(1).strip() if boundary_match else "--frame").encode()
            if not boundary.startswith(b"--"):
                boundary = b"--" + boundary

            buffer = bytearray()
            for chunk in r.iter_content(chunk_size=4096):
                buffer.extend(chunk)
                if buffer.count(boundary) >= 2:
                    break

            parts = buffer.split(boundary)
            if len(parts) < 2:
                raise RuntimeError("Could not extract a complete frame from the camera stream.")

            frame_data = parts[1]
            header_end = frame_data.find(b"\r\n\r\n")
            if header_end == -1:
                header_end = frame_data.find(b"\n\n")
                raw_bytes = frame_data[header_end + 2:]
            else:
                raw_bytes = frame_data[header_end + 4:]
            mime_type = "image/jpeg"
        else:
            raw_bytes = r.content
            mime_type = "image/jpeg"

    if not raw_bytes or len(raw_bytes) < 1000:
        raise ValueError(f"Snapshot is empty or invalid ({len(raw_bytes)} bytes).")

    return base64.b64encode(raw_bytes).decode("utf-8"), mime_type


def fetch_binary_file(target: str) -> tuple[str, str]:
    """Any file (image, audio, video, PDF, ...) from a URL or a local path; absolute or relative to /config."""
    if is_http_url(target):
        url, auth = split_url_credentials(target)
        with requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, auth=auth, stream=True, timeout=20) as r:
            r.raise_for_status()
            buffer = bytearray()
            for chunk in r.iter_content(chunk_size=65536):
                buffer.extend(chunk)
                if len(buffer) > MAX_FILE_BYTES:
                    raise ValueError(f"File exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB.")
            raw_bytes = bytes(buffer)
            mime_type = guess_mime_type(target, r.headers.get("Content-Type", ""))
    else:
        path = target if os.path.isabs(target) else os.path.join(CONFIG_DIR, target)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"File not found: {path}")
        if os.path.getsize(path) > MAX_FILE_BYTES:
            raise ValueError(f"File exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB.")
        with open(path, "rb") as f:
            raw_bytes = f.read()
        mime_type = guess_mime_type(path)

    if not raw_bytes:
        raise ValueError("File is empty.")

    return base64.b64encode(raw_bytes).decode("utf-8"), mime_type


def fetch_calendar_events(entity_id: str, days: int = 14) -> str:
    start_time = datetime.now(timezone.utc)
    end_time = start_time + timedelta(days=days)
    start_str = start_time.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_str = end_time.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    url = f"{HA_URL}/api/calendars/{entity_id}?start={start_str}&end={end_str}"
    try:
        r = requests.get(url, headers=get_ha_headers(), timeout=10)
        if r.status_code != 200:
            return f"Calendar query failed: {r.status_code}"
        events = r.json()
        if not events:
            return "No upcoming events."
        summary = []
        for event in events:
            start_obj = event.get("start", {})
            date_str = start_obj.get("date") or start_obj.get("dateTime", "")[:10]
            title = event.get("summary", "Unknown event")
            summary.append(f"{date_str}: {title}")
        return " | ".join(summary)
    except Exception as e:
        logger.warning(f"Calendar fetch for {entity_id} failed: {e}")
        return "Calendar data unavailable."


def fetch_ha_data(entities: list[str], hours: int):
    if not entities:
        return []
    now_utc = datetime.now(timezone.utc)
    start = (now_utc - timedelta(hours=hours)).isoformat()
    end = now_utc.isoformat()
    url = f"{HA_URL}/api/history/period/{start}"
    params = {
        "end_time": end,
        "filter_entity_id": ",".join(entities),
        "minimal_response": "1"
    }
    try:
        r = requests.get(url, headers=get_ha_headers(), params=params, timeout=40)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"History fetch failed for entities {entities}: {e}")
        return []


def build_raw_dataframe(raw_data, entity_map, tz: tzinfo | None = None):
    series = {}
    for entity_history in raw_data:
        if not entity_history:
            continue
        entity_id = entity_history[0].get("entity_id")
        if not entity_id:
            continue
        key = next((k for k, v in entity_map.items() if v == entity_id), None)
        if not key:
            continue

        timestamps = [entry.get("last_changed") for entry in entity_history]
        values = [entry.get("state") for entry in entity_history]
        if not timestamps:
            continue

        idx = pd.to_datetime(timestamps, format="ISO8601", utc=True)
        s = pd.Series(values, index=idx, name=key)
        s = s[~s.index.duplicated(keep="last")]
        series[key] = s

    if not series:
        return pd.DataFrame()

    resampled = {k: s.resample("1min").ffill().bfill() for k, s in series.items()}
    df = pd.DataFrame(resampled).sort_index().tz_convert(tz or get_local_timezone())
    df = df.replace(["unavailable", "unknown", "None", ""], pd.NA)
    return df


def process_default(df, resample_rule="1h"):
    if df.empty:
        return {}, "No sensor data available."

    df_num = df.apply(pd.to_numeric, errors="coerce").dropna(axis=1, how="all")
    if df_num.empty:
        return {}, "No numeric time series data available."

    df_agg = df_num.resample(resample_rule).mean().round(2)
    current = df_agg.iloc[-1].to_dict() if not df_agg.empty else {}
    current = {k: float(v) for k, v in current.items() if pd.notna(v)}
    df_agg.index.name = "timestamp"
    out = df_agg.fillna(0).reset_index()
    # to_json would convert to UTC; keep local time with its offset so the LLM reads wall-clock times
    out["timestamp"] = out["timestamp"].map(lambda ts: ts.isoformat())
    timeseries_json = out.to_json(orient="records")
    return current, timeseries_json


def create_audit_archive(task_name: str, prompt: str, verdict: str, metrics: dict, timeseries_data: str, images: list) -> str:
    audit_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "archive")
    os.makedirs(audit_dir, exist_ok=True)

    timestamp = local_now().strftime("%Y%m%d_%H%M%S")
    tar_filename = os.path.join(audit_dir, f"audit_{task_name}_{timestamp}.tar.gz")

    with tarfile.open(tar_filename, "w:gz") as tar:
        prompt_bytes = prompt.encode("utf-8")
        info = tarfile.TarInfo(name="prompt.txt")
        info.size = len(prompt_bytes)
        tar.addfile(info, io.BytesIO(prompt_bytes))

        verdict_bytes = verdict.encode("utf-8")
        info = tarfile.TarInfo(name="response.md")
        info.size = len(verdict_bytes)
        tar.addfile(info, io.BytesIO(verdict_bytes))

        metrics_bytes = json.dumps(metrics, indent=2, ensure_ascii=False).encode("utf-8")
        info = tarfile.TarInfo(name="kpis.json")
        info.size = len(metrics_bytes)
        tar.addfile(info, io.BytesIO(metrics_bytes))

        if timeseries_data:
            try:
                json.loads(timeseries_data)
                data_name = "data.json"
            except (TypeError, ValueError):
                data_name = "data.txt"
            data_bytes = str(timeseries_data).encode("utf-8")
            info = tarfile.TarInfo(name=data_name)
            info.size = len(data_bytes)
            tar.addfile(info, io.BytesIO(data_bytes))

        for idx, (b64_data, mime_type) in enumerate(images):
            ext = MIME_TYPES.guess_extension(mime_type) or ".bin"
            img_bytes = base64.b64decode(b64_data)
            prefix = "image" if mime_type.startswith("image/") else "file"
            info = tarfile.TarInfo(name=f"{prefix}_{idx}{ext}")
            info.size = len(img_bytes)
            tar.addfile(info, io.BytesIO(img_bytes))

    return tar_filename


def load_options() -> dict:
    if os.path.exists(OPTIONS_PATH):
        try:
            with open(OPTIONS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to read {OPTIONS_PATH}: {e}")
    return {}


def load_tasks_config() -> dict:
    if not os.path.exists(TASKS_CONFIG_PATH):
        logger.warning(f"Tasks config file not found at {TASKS_CONFIG_PATH}")
        return {}
    try:
        with open(TASKS_CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"Error loading {TASKS_CONFIG_PATH}: {e}")
        return {}


def publish_task_discovery(client: mqtt.Client, task_id: str, task_config: dict):
    """Creates the sensor and button entities in HA via MQTT discovery."""
    clean_id = task_id.lower().replace("-", "_")
    base_topic = f"homeassistant/sensor/llm_{clean_id}"
    sensor_config_topic = f"{base_topic}/config"
    state_topic = f"{base_topic}/state"

    generic_fallback_template = (
        "{{ value_json.state if value_json.state is defined "
        "else (value_json.status if value_json.status is defined "
        "else (value_json.summary if value_json.summary is defined else 'OK')) }}"
    )
    primary_state_template = task_config.get("state_template", generic_fallback_template)

    device_info = {
        "identifiers": ["ha_llm_runner"],
        "name": "HA LLM Runner Engine",
        "model": "Modular LLM Processor",
        "manufacturer": "Custom Automation"
    }

    # 1. Sensor Discovery
    sensor_payload = {
        "name": task_config.get("name", f"LLM {task_id.replace('_', ' ').title()}"),
        "unique_id": f"llm_runner_{clean_id}",
        "state_topic": state_topic,
        "value_template": primary_state_template,
        "json_attributes_topic": state_topic,
        "icon": task_config.get("icon", "mdi:brain"),
        "device": device_info
    }
    for key in ["unit_of_measurement", "device_class", "state_class"]:
        if key in task_config:
            sensor_payload[key] = task_config[key]

    client.publish(sensor_config_topic, json.dumps(sensor_payload), retain=True)

    # 2. Button Discovery (creates button.run_<name> in Home Assistant)
    button_config_topic = f"homeassistant/button/llm_run_{clean_id}/config"
    button_payload = {
        "name": f"Run {task_config.get('name', task_id)}",
        "unique_id": f"llm_button_{clean_id}",
        "command_topic": f"ha_llm_runner/run/{task_id}",
        "payload_press": "RUN",
        "icon": "mdi:play-circle-outline",
        "device": device_info
    }
    client.publish(button_config_topic, json.dumps(button_payload), retain=True)


def publish_task_state(client: mqtt.Client, task_id: str, result_data: dict):
    clean_id = task_id.lower().replace("-", "_")
    state_topic = f"homeassistant/sensor/llm_{clean_id}/state"
    client.publish(state_topic, json.dumps(result_data), retain=True)
    logger.info(f"Published retained state to {state_topic}")


def resolve_config_path(path_value: str) -> str:
    if not path_value:
        return path_value
    if os.path.isabs(path_value):
        return path_value

    candidates = [
        path_value,
        os.path.join("/config", path_value),
        os.path.join(PROCESSORS_DIR, path_value),
        os.path.join(PROCESSORS_DIR, os.path.basename(path_value)),
        os.path.join(os.path.dirname(TASKS_CONFIG_PATH), path_value),
        os.path.join(os.path.dirname(TASKS_CONFIG_PATH), os.path.basename(path_value))
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return path_value


def run_processor(processor_name: str, df: pd.DataFrame, task_config: dict):
    os.makedirs(PROCESSORS_DIR, exist_ok=True)

    candidate = resolve_config_path(processor_name)
    if not os.path.exists(candidate):
        if not processor_name.endswith(".py"):
            candidate = f"{candidate}.py"
    if not os.path.exists(candidate):
        raise FileNotFoundError(f"Processor script not found: {candidate}")

    spec = importlib.util.spec_from_file_location(os.path.basename(candidate).replace(".py", ""), candidate)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if not hasattr(module, "process"):
        raise AttributeError(f"Processor '{processor_name}' must export 'process(df, config)'")

    return module.process(df, task_config)


def write_ha_target_state(task_config: dict, result_payload: dict, options: dict | None = None):
    target_sensor = task_config.get("target_sensor")
    if not target_sensor:
        return

    result_text = None
    if isinstance(result_payload, dict):
        for key in ("text", "summary", "status", "value", "result", "verdict"):
            if key in result_payload and result_payload[key] not in (None, ""):
                result_text = str(result_payload[key])
                break
    if result_text is None:
        result_text = json.dumps(result_payload, ensure_ascii=False)

    history_file = os.path.join(os.path.dirname(TASKS_CONFIG_PATH), f"{target_sensor.replace('.', '_')}_history.json")
    event_history = []
    if os.path.exists(history_file):
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                event_history = json.load(f)
        except Exception:
            event_history = []

    now = local_now(options)
    now_str = format_datetime(now, options)
    event_history.insert(0, {"time": now_str, "text": result_text})
    event_history = event_history[:30]

    try:
        with open(history_file, "w", encoding="utf-8") as f:
            json.dump(event_history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Could not save history file {history_file}: {e}")

    post_url = f"{HA_URL}/api/states/{target_sensor}"
    payload = {
        "state": now.isoformat(),
        "attributes": {
            "text": result_text,
            "history": event_history,
            "friendly_name": task_config.get("friendly_name", target_sensor)
        }
    }

    try:
        requests.post(post_url, headers=get_ha_headers(), json=payload, timeout=10).raise_for_status()
    except Exception as e:
        logger.warning(f"Could not push task result to HA state {target_sensor}: {e}")


def execute_task(task_id: str, task_config: dict, client: mqtt.Client, options: dict):
    logger.info(f"--- Executing Task: {task_id} ---")

    metrics = {}
    images = []
    loaded_image_names = []
    loaded_file_names = []
    timeseries_data = ""

    entity_map = task_config.get("entities", {})
    if isinstance(entity_map, dict):
        entity_list = list(entity_map.values())
        sensor_entities = {}
        camera_entities = {}
        calendar_entities = {}
        for key, value in entity_map.items():
            val = str(value)
            if val.startswith("camera.") or val.startswith("http"):
                camera_entities[key] = value
            elif val.startswith("calendar."):
                calendar_entities[key] = value
            else:
                sensor_entities[key] = value
    else:
        entity_list = list(entity_map)
        sensor_entities = {str(i): entity for i, entity in enumerate(entity_list)}
        camera_entities = {}
        calendar_entities = {}

    for key, cam_target in camera_entities.items():
        try:
            b64_data, mime_type = fetch_camera_snapshot(str(cam_target))
            images.append((b64_data, mime_type))
            loaded_image_names.append(key)
        except Exception as e:
            logger.warning(f"Camera snapshot for {cam_target} failed: {e}")

    file_entities = task_config.get("files", {}) or {}
    for key, target_url in file_entities.items():
        try:
            b64_data, mime_type = fetch_binary_file(str(target_url))
            images.append((b64_data, mime_type))
            loaded_file_names.append(f"{key} ({mime_type})")
        except Exception as e:
            logger.warning(f"Loading file {target_url} failed: {e}")

    for key, cal_id in calendar_entities.items():
        try:
            metrics[key] = fetch_calendar_events(str(cal_id), days=14)
        except Exception as e:
            logger.warning(f"Calendar fetch for {cal_id} failed: {e}")
            metrics[key] = "Calendar data unavailable."

    for key, spec in sensor_entities.items():
        metrics[key] = fetch_ha_state(str(spec))

    hours = int(task_config.get("hours", 24))
    sensor_history_map = {k: str(v).split(":", 1)[0] for k, v in sensor_entities.items()}
    if sensor_history_map and hours > 0:
        raw_data = fetch_ha_data(list(sensor_history_map.values()), hours)
        df = build_raw_dataframe(raw_data, sensor_history_map, get_local_timezone(options))
    else:
        df = pd.DataFrame()

    # A custom processor runs once, even without history (it may fetch its own data)
    processor_name = task_config.get("data_processor") or task_config.get("processor")
    processor_metrics = None
    raw_data = None
    if processor_name:
        try:
            processor_metrics, raw_data = run_processor(processor_name, df, task_config)
            metrics.update(processor_metrics or {})
            timeseries_data = raw_data if isinstance(raw_data, str) else json.dumps(raw_data, ensure_ascii=False, default=str)
        except Exception as e:
            logger.warning(f"Custom processor failed for task '{task_id}', using default aggregation: {e}")
            processor_name, processor_metrics, raw_data = None, None, None
    if not processor_name:
        if df.empty:
            if sensor_history_map and hours > 0:
                timeseries_data = "No time series data available."
        else:
            p_metrics, timeseries_data = process_default(df, task_config.get("resample", "1h"))
            for k, v in p_metrics.items():
                metrics.setdefault(k, v)

    for key, url in (task_config.get("urls", {}) or {}).items():
        metrics[key] = fetch_external_url(str(url))

    now = local_now(options)
    weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
    metrics["weekday"] = weekdays[now.weekday()]
    metrics["today"] = f"{now.day} {months[now.month - 1]}"
    metrics["now"] = format_datetime(now, options)

    target_sensor = task_config.get("target_sensor")
    history_text = "No history available."
    if target_sensor:
        history_file = os.path.join(os.path.dirname(TASKS_CONFIG_PATH), f"{target_sensor.replace('.', '_')}_history.json")
        if os.path.exists(history_file):
            try:
                with open(history_file, "r", encoding="utf-8") as f:
                    event_history = json.load(f)
                recent = [entry.get("text", "") for entry in event_history[:int(task_config.get("history_limit", 7))] if "text" in entry]
                if recent:
                    history_text = "\n---\n".join(recent)
            except Exception:
                pass
    metrics["history"] = history_text

    metrics_payload = processor_metrics if processor_metrics is not None else metrics.copy()

    custom_prompt = task_config.get("prompt")
    json_schema = task_config.get("response_schema")

    if custom_prompt:
        serialized_data = raw_data if isinstance(raw_data, str) else json.dumps(raw_data if raw_data is not None else metrics_payload, ensure_ascii=False)
        try:
            prompt_context = {
                "metrics": json.dumps(metrics, ensure_ascii=False),
                "data": serialized_data,
                "timeseries": timeseries_data or "No history requested.",
                **metrics
            }
            formatted_prompt = custom_prompt.format(**prompt_context)
        except (KeyError, IndexError, ValueError):
            formatted_prompt = f"{custom_prompt}\n\nCURRENT VALUES:\n{json.dumps(metrics, ensure_ascii=False, indent=2)}"
            if timeseries_data and timeseries_data not in ("No history requested.", "No time series data available."):
                formatted_prompt += f"\n\nMEASUREMENTS (JSON):\n{timeseries_data}"

        if loaded_image_names:
            image_names = ", ".join(loaded_image_names)
            formatted_prompt += f"\n\nIMAGE MATERIAL:\nAttached image source(s): {image_names}."
        if loaded_file_names:
            formatted_prompt += f"\n\nFILE MATERIAL:\nAttached file(s): {', '.join(loaded_file_names)}."

        # Errors propagate so a failed call never overwrites the last good result or its history
        provider = get_provider(task_config.get("provider"), options)
        result_payload = provider.generate(LLMRequest(
            prompt=formatted_prompt,
            model=provider.resolve_model(task_config.get("model"), options),
            temperature=float(task_config.get("temperature", 0.0)),
            schema=json_schema,
            attachments=images,
        ))
        result_payload.setdefault("updated_at", local_now(options).isoformat())
    else:
        if isinstance(raw_data, str):
            try:
                parsed_data = json.loads(raw_data)
            except Exception:
                parsed_data = raw_data
        else:
            parsed_data = raw_data

        result_payload = {
            "updated_at": local_now(options).isoformat(),
            **metrics,
            "data": parsed_data
        }

    audit_enabled = bool(options.get("audit_archive", True)) and task_config.get("audit", bool(custom_prompt))
    if audit_enabled:
        try:
            verdict_text = result_payload.get("text") or result_payload.get("summary") or result_payload.get("verdict") or json.dumps(result_payload, ensure_ascii=False)
            create_audit_archive(task_id, formatted_prompt if custom_prompt else "", str(verdict_text), metrics, timeseries_data, images)
        except Exception as e:
            logger.warning(f"Could not create audit archive for task '{task_id}': {e}")

    write_ha_target_state(task_config, result_payload, options)
    publish_task_state(client, task_id, result_payload)
    logger.info(f"Task '{task_id}' finished successfully.")


def run_all_tasks(client: mqtt.Client, options: dict):
    tasks_cfg = load_tasks_config()
    tasks = tasks_cfg.get("tasks", {})
    if not tasks:
        logger.info("No tasks configured in llm_tasks.yaml.")
        return

    for task_id, task_cfg in tasks.items():
        publish_task_discovery(client, task_id, task_cfg)

    for task_id, task_cfg in tasks.items():
        try:
            execute_task(task_id, task_cfg, client, options)
        except Exception as e:
            logger.exception(f"Error executing task '{task_id}': {e}")


def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        logger.info("Connected to MQTT Broker. Subscribing to trigger topics...")
        client.subscribe("ha_llm_runner/run")
        client.subscribe("ha_llm_runner/run/+")
        # Initial discovery and task run on container start
        options = load_options()
        run_all_tasks(client, options)
    else:
        logger.error(f"MQTT connection failed with code {rc}")


def on_message(client, userdata, msg):
    topic = msg.topic
    payload = msg.payload.decode("utf-8").strip()
    logger.info(f"Received MQTT trigger on {topic} (payload: '{payload}')")

    options = load_options()
    tasks_cfg = load_tasks_config()
    tasks = tasks_cfg.get("tasks", {})

    target_task = None
    if "/" in topic.replace("ha_llm_runner/run", ""):
        target_task = topic.split("/")[-1]
    elif payload and payload.lower() not in ("run", "all", "press", ""):
        target_task = payload

    if target_task:
        if target_task in tasks:
            try:
                execute_task(target_task, tasks[target_task], client, options)
            except Exception as e:
                logger.exception(f"Error running requested task '{target_task}': {e}")
        else:
            logger.warning(f"Requested task '{target_task}' not found in llm_tasks.yaml.")
    else:
        run_all_tasks(client, options)


def main():
    logger.info("Starting HA LLM Runner Add-on (Event-driven via MQTT)...")
    os.makedirs(PROCESSORS_DIR, exist_ok=True)

    options = load_options()
    host = options.get("mqtt_host", "core-mosquitto")
    port = int(options.get("mqtt_port", 1883))
    user = options.get("mqtt_user")
    password = options.get("mqtt_password")

    if hasattr(mqtt, "CallbackAPIVersion"):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ha_llm_runner")
    else:
        client = mqtt.Client(client_id="ha_llm_runner")

    if user:
        client.username_pw_set(user, password)

    client.on_connect = on_connect
    client.on_message = on_message

    logger.info(f"Connecting to MQTT Broker at {host}:{port}...")
    client.connect(host, port, 60)
    client.loop_forever()


if __name__ == "__main__":
    main()