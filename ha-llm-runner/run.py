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
import shutil
import string
import threading
import time
import traceback
from urllib.parse import urlparse, unquote
from datetime import datetime, timezone, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
import requests
from requests.auth import HTTPDigestAuth
import pandas as pd
import paho.mqtt.client as mqtt
from babel import Locale, UnknownLocaleError
from babel.dates import format_date, format_skeleton, format_time

from llm_providers import LLMRequest, get_provider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("ha_llm_runner")

OPTIONS_PATH = "/data/options.json"
# Since 1.4.0: /config is the add-on's own folder (addon_config), Home Assistant's config is mounted at /homeassistant
CONFIG_DIR = "/config"
HA_CONFIG_DIR = "/homeassistant"
TASKS_CONFIG_PATH = os.path.join(CONFIG_DIR, "llm_tasks.yaml")
PROCESSORS_DIR = os.path.join(CONFIG_DIR, "processors")
MEMORY_DIR = os.path.join(CONFIG_DIR, "memory")
AUDIT_DIR = os.path.join(CONFIG_DIR, "audit")
LEGACY_TASKS_CONFIG_PATH = os.path.join(HA_CONFIG_DIR, "llm_tasks.yaml")
LEGACY_PROCESSORS_DIR = os.path.join(HA_CONFIG_DIR, "scripts", "processors")
LEGACY_CONFIG_PREFIX = "/config/"
MEMORY_SIZE = 30

HA_URL = os.environ.get("SUPERVISOR_URL", "http://supervisor/core")
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

DEFAULT_DATETIME_FORMAT = "%d.%m.%Y %H:%M:%S"
DEFAULT_LANGUAGE = "en"
TIMESERIES_PLACEHOLDERS = {"timeseries", "data"}
_ha_config: dict | None = None
_mqtt_client = None
_run_lock = threading.Lock()
TASK_STATUS: dict[str, dict] = {}
MQTT_STATUS = {"connected": False}


def safe_name(value: str) -> str:
    """File-system safe version of a task id or entity id."""
    return re.sub(r"[^A-Za-z0-9_\-]", "_", str(value)) or "_"


def legacy_path_candidates(path_value: str) -> list[str]:
    """Pre-1.4.0 absolute '/config/...' paths pointed into Home Assistant's config folder."""
    if path_value.startswith(LEGACY_CONFIG_PREFIX):
        return [os.path.join(HA_CONFIG_DIR, path_value[len(LEGACY_CONFIG_PREFIX):])]
    return []


def is_absolute(path_value: str) -> bool:
    return path_value.startswith("/") or os.path.isabs(path_value)


def resolve_data_path(path_value: str) -> str:
    """Path of a `files:` entry: absolute, or relative to Home Assistant's config, then to the add-on folder."""
    if is_absolute(path_value):
        candidates = [path_value, *legacy_path_candidates(path_value)]
    else:
        candidates = [os.path.join(HA_CONFIG_DIR, path_value), os.path.join(CONFIG_DIR, path_value)]
    return next((c for c in candidates if os.path.isfile(c)), candidates[0])


def get_ha_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HA_TOKEN") or ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_ha_config() -> dict:
    """Home Assistant's /api/config, cached after the first successful read."""
    global _ha_config
    if _ha_config is None:
        try:
            r = requests.get(f"{HA_URL}/api/config", headers=get_ha_headers(), timeout=10)
            r.raise_for_status()
            _ha_config = r.json() or {}
        except Exception as e:
            logger.warning(f"Could not read configuration from Home Assistant: {e}")
            return {}
    return _ha_config


def fetch_ha_time_zone() -> str:
    return str(fetch_ha_config().get("time_zone") or "")


def fetch_ha_language() -> str:
    return str(fetch_ha_config().get("language") or "")


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


def parse_locale(name: str) -> Locale | None:
    name = str(name or "").strip().replace("_", "-")
    if not name:
        return None
    for candidate in (name, name.split("-", 1)[0]):
        try:
            return Locale.parse(candidate, sep="-")
        except (ValueError, TypeError, UnknownLocaleError):
            continue
    return None


def get_locale(options: dict | None = None) -> Locale:
    """Option `language` > Home Assistant's configured language > English."""
    options = load_options() if options is None else options
    sources = (
        ("add-on option 'language'", lambda: options.get("language")),
        ("Home Assistant config", fetch_ha_language),
    )
    for source, read in sources:
        name = read()
        if not name:
            continue
        locale = parse_locale(name)
        if locale is not None:
            return locale
        logger.warning(f"Ignoring unknown language '{name}' from {source}.")
    return Locale.parse(DEFAULT_LANGUAGE)


def _clean_babel(text: str) -> str:
    # CLDR uses narrow no-break spaces (e.g. "5:45 PM"), which TTS engines and logs handle poorly
    return text.replace("\u202f", " ").replace("\u00a0", " ")


def localized_strftime(value: datetime, fmt: str, locale: Locale) -> str:
    """strftime, but day and month names (%A %a %B %b) come from the locale instead of the C locale."""
    names = {
        "A": lambda: format_date(value, "EEEE", locale=locale),
        "a": lambda: format_date(value, "EEE", locale=locale),
        "B": lambda: format_date(value, "MMMM", locale=locale),
        "b": lambda: format_date(value, "MMM", locale=locale),
    }

    def replace(match: re.Match) -> str:
        code = match.group(1)
        if code in names:
            return _clean_babel(names[code]()).replace("%", "%%")
        return match.group(0)

    return value.strftime(re.sub(r"%(.)", replace, fmt))


def format_datetime(value: datetime, options: dict | None = None) -> str:
    options = load_options() if options is None else options
    return localized_strftime(value, options.get("datetime_format") or DEFAULT_DATETIME_FORMAT, get_locale(options))


def builtin_placeholders(now: datetime, options: dict) -> dict:
    locale = get_locale(options)
    return {
        "weekday": _clean_babel(format_date(now, "EEEE", locale=locale)),
        "today": _clean_babel(format_skeleton("dMMMM", now, locale=locale)),
        "date": _clean_babel(format_date(now, "long", locale=locale)),
        "time": _clean_babel(format_time(now, "short", locale=locale)),
        "month": _clean_babel(format_date(now, "LLLL", locale=locale)),
        "year": str(now.year),
        "now": format_datetime(now, options),
    }


def prompt_fields(prompt: str) -> set[str]:
    """Top-level placeholder names used in a str.format prompt ({{...}} escapes excluded)."""
    try:
        parsed = list(string.Formatter().parse(prompt))
    except ValueError:
        return set()
    return {re.split(r"[.\[]", field, maxsplit=1)[0] for _, field, _, _ in parsed if field}

URL_CREDENTIALS = re.compile(r"(?<=://)[^/@\s]+@")


def redact_url(text) -> str:
    """Replaces `user:pass@` in any URL inside the text with `***@` (for logs and error messages)."""
    return URL_CREDENTIALS.sub("***@", str(text))


def fetch_external_url(url: str, max_chars: int = 15000) -> str:
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        text = r.text
        if len(text) > max_chars:
            logger.warning(f"Payload from {redact_url(url)} was truncated (> {max_chars} chars)")
            return text[:max_chars] + "\n... [TRUNCATED]"
        return text
    except Exception as e:
        logger.warning(redact_url(f"Fetch of external URL {url} failed: {e}"))
        return redact_url(f"ERROR: {e}")


def fetch_ha_states() -> list[dict]:
    """All entity states from Home Assistant; raises if Home Assistant cannot be reached."""
    r = requests.get(f"{HA_URL}/api/states", headers=get_ha_headers(), timeout=10)
    r.raise_for_status()
    return r.json() or []


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
    """Any file (image, audio, video, PDF, ...) from a URL or a local path (see resolve_data_path)."""
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
        path = resolve_data_path(target)
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


def prune_audit_archives(retention_days: int) -> int:
    if retention_days <= 0 or not os.path.isdir(AUDIT_DIR):
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for name in os.listdir(AUDIT_DIR):
        path = os.path.join(AUDIT_DIR, name)
        if name.endswith(".tar.gz") and os.path.isfile(path) and os.path.getmtime(path) < cutoff:
            try:
                os.remove(path)
                removed += 1
            except OSError as e:
                logger.warning(f"Could not delete old audit archive {name}: {e}")
    return removed


def create_audit_archive(task_name: str, prompt: str, verdict: str, metrics: dict, timeseries_data: str, images: list, options: dict | None = None) -> str:
    options = load_options() if options is None else options
    os.makedirs(AUDIT_DIR, exist_ok=True)

    timestamp = local_now(options).strftime("%Y%m%d_%H%M%S")
    tar_filename = os.path.join(AUDIT_DIR, f"audit_{safe_name(task_name)}_{timestamp}.tar.gz")

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

    prune_audit_archives(int(options.get("audit_retention_days", 30) or 0))
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


def tasks_from_config(data) -> dict:
    """Tasks under a top-level 'tasks:' key, or, as in the original prototype, directly at the top level."""
    if not isinstance(data, dict):
        return {}
    tasks = data["tasks"] if "tasks" in data else data
    return {str(k): v for k, v in tasks.items() if isinstance(v, dict)} if isinstance(tasks, dict) else {}


def load_tasks() -> dict:
    return tasks_from_config(load_tasks_config())


def legacy_memory_candidates(target_sensor: str) -> list[str]:
    # The add-on (1.3.x) kept it in HA's config folder, the original prototype next to its script in scripts/
    name = f"{target_sensor.replace('.', '_')}_history.json"
    return [os.path.join(HA_CONFIG_DIR, name), os.path.join(HA_CONFIG_DIR, "scripts", name)]


def migrate_legacy_storage() -> bool:
    """One-time copy of the pre-1.4.0 files from Home Assistant's config folder. The originals stay untouched."""
    if os.path.exists(TASKS_CONFIG_PATH) or not os.path.isfile(LEGACY_TASKS_CONFIG_PATH):
        return False
    logger.info(f"Migrating {LEGACY_TASKS_CONFIG_PATH} to the add-on config folder {CONFIG_DIR} ...")
    os.makedirs(CONFIG_DIR, exist_ok=True)
    shutil.copy2(LEGACY_TASKS_CONFIG_PATH, TASKS_CONFIG_PATH)

    if os.path.isdir(LEGACY_PROCESSORS_DIR):
        os.makedirs(PROCESSORS_DIR, exist_ok=True)
        for name in sorted(os.listdir(LEGACY_PROCESSORS_DIR)):
            source, target = os.path.join(LEGACY_PROCESSORS_DIR, name), os.path.join(PROCESSORS_DIR, name)
            if os.path.isfile(source) and not os.path.exists(target):
                shutil.copy2(source, target)
                logger.info(f"  processor {name}")

    for task_id, task_cfg in load_tasks().items():
        target_sensor = task_cfg.get("target_sensor")
        if not target_sensor:
            continue
        target = memory_path(task_id)
        source = next((c for c in legacy_memory_candidates(str(target_sensor)) if os.path.isfile(c)), None)
        if source and not os.path.exists(target):
            os.makedirs(MEMORY_DIR, exist_ok=True)
            shutil.copy2(source, target)
            logger.info(f"  memory of task '{task_id}' from {os.path.basename(source)}")
    return True


def memory_path(task_id: str) -> str:
    return os.path.join(MEMORY_DIR, f"{safe_name(task_id)}.json")


def load_memory(task_id: str) -> list[dict]:
    """Previous answers of a task, newest first."""
    path = memory_path(task_id)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            entries = json.load(f)
        return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
    except Exception as e:
        logger.warning(f"Could not read memory of task '{task_id}': {e}")
        return []


def save_memory(task_id: str, entries: list[dict]):
    os.makedirs(MEMORY_DIR, exist_ok=True)
    write_text_atomic(memory_path(task_id), json.dumps(entries, ensure_ascii=False, indent=2))


def append_memory(task_id: str, text: str, options: dict, keep: int = MEMORY_SIZE) -> list[dict]:
    entries = load_memory(task_id)
    entries.insert(0, {"time": format_datetime(local_now(options), options), "text": text})
    entries = entries[:max(keep, MEMORY_SIZE)]
    try:
        save_memory(task_id, entries)
    except Exception as e:
        logger.warning(f"Could not save memory of task '{task_id}': {e}")
    return entries


def clear_memory(task_id: str):
    if os.path.exists(memory_path(task_id)):
        os.remove(memory_path(task_id))


def write_text_atomic(path: str, content: str):
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    os.replace(tmp_path, path)


def result_text(result_payload) -> str:
    """The text that represents an answer in the memory and the target sensor."""
    if isinstance(result_payload, dict):
        for key in ("text", "summary", "status", "value", "result", "verdict"):
            if result_payload.get(key) not in (None, ""):
                return str(result_payload[key])
    return json.dumps(result_payload, ensure_ascii=False, default=str)


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


def remove_task_discovery(client: mqtt.Client, task_id: str):
    """Deletes the sensor and button of a task that was removed from llm_tasks.yaml."""
    clean_id = task_id.lower().replace("-", "_")
    for topic in (
        f"homeassistant/sensor/llm_{clean_id}/config",
        f"homeassistant/sensor/llm_{clean_id}/state",
        f"homeassistant/button/llm_run_{clean_id}/config",
    ):
        client.publish(topic, "", retain=True)


def sync_task_discovery(old_tasks: dict, new_tasks: dict, client: mqtt.Client | None = None):
    """Applies an edited llm_tasks.yaml to Home Assistant without restarting the add-on."""
    client = client or _mqtt_client
    if client is None:
        return
    for task_id in set(old_tasks) - set(new_tasks):
        remove_task_discovery(client, task_id)
    for task_id, task_cfg in new_tasks.items():
        publish_task_discovery(client, task_id, task_cfg)


def processor_candidates(path_value: str) -> list[str]:
    base = os.path.basename(path_value)
    if is_absolute(path_value):
        return [path_value, os.path.join(PROCESSORS_DIR, base), *legacy_path_candidates(path_value)]
    return [
        os.path.join(PROCESSORS_DIR, path_value),
        os.path.join(PROCESSORS_DIR, base),
        os.path.join(CONFIG_DIR, path_value),
        os.path.join(os.path.dirname(TASKS_CONFIG_PATH), path_value),
        os.path.join(LEGACY_PROCESSORS_DIR, base),
        os.path.join(HA_CONFIG_DIR, path_value),
        path_value,
    ]


def resolve_config_path(path_value: str) -> str:
    """Processor path: the add-on's processors/ folder first, then the pre-1.4.0 locations; `.py` is optional."""
    if not path_value:
        return path_value
    names = [path_value] if path_value.endswith(".py") else [path_value, f"{path_value}.py"]
    for name in names:
        for candidate in processor_candidates(name):
            if os.path.isfile(candidate):
                return os.path.normpath(candidate)
    return path_value


def run_processor(processor_name: str, df: pd.DataFrame, task_config: dict):
    candidate = resolve_config_path(processor_name)
    if not os.path.isfile(candidate):
        raise FileNotFoundError(f"Processor script not found: {processor_name}")

    spec = importlib.util.spec_from_file_location(os.path.basename(candidate).replace(".py", ""), candidate)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if not hasattr(module, "process"):
        raise AttributeError(f"Processor '{processor_name}' must export 'process(df, config)'")

    return module.process(df, task_config)


def write_ha_target_state(task_config: dict, result_payload: dict, options: dict | None = None, memory: list | None = None):
    """Mirrors the result and the task memory into an extra HA state (`target_sensor:`)."""
    target_sensor = task_config.get("target_sensor")
    if not target_sensor:
        return

    post_url = f"{HA_URL}/api/states/{target_sensor}"
    payload = {
        "state": local_now(options).isoformat(),
        "attributes": {
            "text": result_text(result_payload),
            "history": memory or [],
            "friendly_name": task_config.get("friendly_name", target_sensor)
        }
    }

    try:
        requests.post(post_url, headers=get_ha_headers(), json=payload, timeout=10).raise_for_status()
    except Exception as e:
        logger.warning(f"Could not push task result to HA state {target_sensor}: {e}")


def execute_task(task_id: str, task_config: dict, client: mqtt.Client, options: dict, dry_run: bool = False):
    logger.info(f"--- {'Previewing' if dry_run else 'Executing'} Task: {task_id} ---")

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

    # Images and files only go to the LLM; a data-only task (no prompt) doesn't need to download them
    custom_prompt = task_config.get("prompt")
    if not custom_prompt:
        camera_entities = {}
    for key, cam_target in camera_entities.items():
        try:
            b64_data, mime_type = fetch_camera_snapshot(str(cam_target))
            images.append((b64_data, mime_type))
            loaded_image_names.append(key)
        except Exception as e:
            logger.warning(redact_url(f"Camera snapshot for {cam_target} failed: {e}"))

    file_entities = (task_config.get("files", {}) or {}) if custom_prompt else {}
    for key, target_url in file_entities.items():
        try:
            b64_data, mime_type = fetch_binary_file(str(target_url))
            images.append((b64_data, mime_type))
            loaded_file_names.append(f"{key} ({mime_type})")
        except Exception as e:
            logger.warning(redact_url(f"Loading file {target_url} failed: {e}"))

    for key, cal_id in calendar_entities.items():
        try:
            metrics[key] = fetch_calendar_events(str(cal_id), days=14)
        except Exception as e:
            logger.warning(f"Calendar fetch for {cal_id} failed: {e}")
            metrics[key] = "Calendar data unavailable."

    for key, spec in sensor_entities.items():
        metrics[key] = fetch_ha_state(str(spec))

    hours = int(task_config.get("hours", 0))
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
            if raw_data is not None:
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

    # Entity, URL and processor values keep precedence over same-named built-ins
    for key, value in builtin_placeholders(local_now(options), options).items():
        metrics.setdefault(key, value)

    task_memory = load_memory(task_id)
    recent = [entry.get("text", "") for entry in task_memory[:int(task_config.get("history_limit", 7))] if entry.get("text")]
    metrics.setdefault("history", "\n---\n".join(recent) if recent else "No history available.")

    metrics_payload = processor_metrics if processor_metrics is not None else metrics.copy()

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
            series_in_prompt = bool(prompt_fields(custom_prompt) & TIMESERIES_PLACEHOLDERS)
        except (KeyError, IndexError, ValueError):
            formatted_prompt = f"{custom_prompt}\n\nCURRENT VALUES:\n{json.dumps(metrics, ensure_ascii=False, indent=2)}"
            series_in_prompt = False
        # Like the original prototype: requested history is always sent, as JSON, even if the prompt doesn't ask for it
        if timeseries_data and not series_in_prompt:
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

    outcome = {
        "prompt": formatted_prompt if custom_prompt else "",
        "result": result_payload,
        "attachments": loaded_image_names + loaded_file_names,
    }
    # A preview stops here: no audit archive, no memory, no Home Assistant state, nothing published
    if dry_run:
        return outcome

    audit_enabled = bool(options.get("audit_archive", True)) and task_config.get("audit", bool(custom_prompt))
    if audit_enabled:
        try:
            verdict_text = result_payload.get("text") or result_payload.get("summary") or result_payload.get("verdict") or json.dumps(result_payload, ensure_ascii=False)
            create_audit_archive(task_id, formatted_prompt if custom_prompt else "", str(verdict_text), metrics, timeseries_data, images, options)
        except Exception as e:
            logger.warning(f"Could not create audit archive for task '{task_id}': {e}")

    # LLM answers always go into the task memory ({history}); plain data tasks only when mirrored to a target_sensor
    if custom_prompt or task_config.get("target_sensor"):
        task_memory = append_memory(task_id, result_text(result_payload), options, keep=int(task_config.get("history_limit", 7)))
    write_ha_target_state(task_config, result_payload, options, task_memory)

    client = client or _mqtt_client
    if client is not None:
        publish_task_state(client, task_id, result_payload)
    else:
        logger.warning(f"MQTT not connected, result of task '{task_id}' was not published.")
    logger.info(f"Task '{task_id}' finished successfully.")
    return outcome


def preview_task(task_id: str, task_config: dict, options: dict) -> dict:
    """Runs a (possibly unsaved) task configuration without saving or publishing anything; errors are returned, not raised."""
    with _run_lock:
        started = time.monotonic()
        try:
            outcome = execute_task(task_id, task_config, None, options, dry_run=True) or {}
            return {"ok": True, **outcome, "duration": round(time.monotonic() - started, 2)}
        except Exception as e:
            logger.warning(redact_url(f"Preview of task '{task_id}' failed: {type(e).__name__}: {e}"))
            return {"ok": False, "error": redact_url(f"{type(e).__name__}: {e}"), "traceback": redact_url(traceback.format_exc()),
                    "duration": round(time.monotonic() - started, 2)}


def task_status(task_id: str) -> dict:
    return dict(TASK_STATUS.get(task_id, {"state": "idle"}))


def run_task(task_id: str, task_config: dict, client: mqtt.Client | None, options: dict) -> bool:
    """Runs one task with status tracking. Tasks run one at a time, whoever triggers them (MQTT, UI, start-up)."""
    status = TASK_STATUS.setdefault(task_id, {})
    status.update({"state": "queued", "queued_at": local_now(options).isoformat()})
    with _run_lock:
        started = time.monotonic()
        status.update({"state": "running", "started_at": local_now(options).isoformat(), "error": None})
        try:
            outcome = execute_task(task_id, task_config, client, options) or {}
            status.update({
                "state": "ok",
                "last_prompt": outcome.get("prompt", ""),
                "last_result": outcome.get("result"),
                "attachments": outcome.get("attachments", []),
            })
            return True
        except Exception as e:
            logger.exception(f"Error executing task '{task_id}': {e}")
            status.update({"state": "error", "error": f"{type(e).__name__}: {e}"})
            return False
        finally:
            status.update({
                "finished_at": local_now(options).isoformat(),
                "duration": round(time.monotonic() - started, 2),
            })


def run_task_async(task_id: str, task_config: dict, client: mqtt.Client | None, options: dict) -> threading.Thread:
    # Off the MQTT network loop: an LLM call can take a minute and would otherwise stall keep-alives
    TASK_STATUS.setdefault(task_id, {})["state"] = "queued"
    thread = threading.Thread(target=run_task, args=(task_id, task_config, client, options), name=f"task-{task_id}", daemon=True)
    thread.start()
    return thread


def run_all_tasks(client: mqtt.Client | None, options: dict):
    tasks = load_tasks()
    if not tasks:
        logger.info("No tasks configured in llm_tasks.yaml.")
        return

    for task_id, task_cfg in tasks.items():
        run_task(task_id, task_cfg, client, options)


def run_all_tasks_async(client: mqtt.Client | None, options: dict) -> threading.Thread:
    for task_id in load_tasks():
        TASK_STATUS.setdefault(task_id, {})["state"] = "queued"
    thread = threading.Thread(target=run_all_tasks, args=(client, options), name="run-all", daemon=True)
    thread.start()
    return thread


def on_connect(client, userdata, flags, rc, properties=None):
    global _mqtt_client
    if rc == 0:
        logger.info("Connected to MQTT Broker. Subscribing to trigger topics...")
        _mqtt_client = client
        MQTT_STATUS.update({"connected": True, "error": None})
        client.subscribe("ha_llm_runner/run")
        client.subscribe("ha_llm_runner/run/+")
        tasks = load_tasks()
        for task_id, task_cfg in tasks.items():
            publish_task_discovery(client, task_id, task_cfg)
    else:
        MQTT_STATUS.update({"connected": False, "error": f"connection refused (code {rc})"})
        logger.error(f"MQTT connection failed with code {rc}")


def on_disconnect(client, userdata, *args):
    MQTT_STATUS["connected"] = False
    logger.warning("Disconnected from MQTT Broker, reconnecting...")


def on_message(client, userdata, msg):
    topic = msg.topic
    payload = msg.payload.decode("utf-8").strip()
    logger.info(f"Received MQTT trigger on {topic} (payload: '{payload}')")

    options = load_options()
    tasks = load_tasks()

    target_task = None
    if "/" in topic.replace("ha_llm_runner/run", ""):
        target_task = topic.split("/")[-1]
    elif payload and payload.lower() not in ("run", "all", "press", ""):
        target_task = payload

    if target_task:
        if target_task in tasks:
            run_task_async(target_task, tasks[target_task], client, options)
        else:
            logger.warning(f"Requested task '{target_task}' not found in llm_tasks.yaml.")
    else:
        run_all_tasks_async(client, options)


def prepare_storage():
    try:
        migrate_legacy_storage()
    except Exception as e:
        logger.error(f"Migration of the pre-1.4.0 files failed: {e}")
    for folder in (PROCESSORS_DIR, MEMORY_DIR, AUDIT_DIR):
        os.makedirs(folder, exist_ok=True)
    if not os.path.exists(TASKS_CONFIG_PATH):
        logger.warning(f"No tasks yet. Create {TASKS_CONFIG_PATH} in the web UI or via the addon_configs share.")


def check_ha_api() -> bool:
    """Logs once at startup whether the Home Assistant API is usable, so auth problems show up immediately."""
    token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HA_TOKEN") or ""
    if not token:
        logger.error("No SUPERVISOR_TOKEN in the environment: Home Assistant data can't be read.")
        return False
    try:
        r = requests.get(f"{HA_URL}/api/", headers=get_ha_headers(), timeout=10)
        r.raise_for_status()
        logger.info("Home Assistant API reachable.")
        return True
    except Exception as e:
        logger.error(f"Home Assistant API not usable (token with {len(token)} characters): {e}")
        return False


def main():
    logger.info("Starting HA LLM Runner Add-on (Event-driven via MQTT)...")
    prepare_storage()
    check_ha_api()

    options = load_options()
    host = options.get("mqtt_host", "core-mosquitto")
    port = int(options.get("mqtt_port", 1883))
    user = options.get("mqtt_user")
    password = options.get("mqtt_password")

    try:
        import web
        web.start_web_server(sys.modules[__name__], port=int(os.environ.get("INGRESS_PORT", web.DEFAULT_PORT)))
    except Exception as e:
        logger.error(f"Web UI could not be started: {e}")

    if hasattr(mqtt, "CallbackAPIVersion"):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ha_llm_runner")
    else:
        client = mqtt.Client(client_id="ha_llm_runner")

    if user:
        client.username_pw_set(user, password)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    logger.info(f"Connecting to MQTT Broker at {host}:{port}...")
    # Asynchronous connect + retry: the web UI stays usable while the broker is unreachable
    client.connect_async(host, port, 60)
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()