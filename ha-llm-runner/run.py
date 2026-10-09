import os
import sys
import time
import json
import logging
import importlib.util
from datetime import datetime, timezone, timedelta

import yaml
import requests
import pandas as pd
import paho.mqtt.publish as publish
from croniter import croniter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("ha_llm_runner")

OPTIONS_PATH = "/data/options.json"
TASKS_CONFIG_PATH = "/config/llm_tasks.yaml"
PROCESSORS_DIR = "/config/scripts/processors"

HA_URL = os.environ.get("SUPERVISOR_URL", "http://supervisor/core")
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
DEFAULT_MODEL = "gemini-3.5-flash-lite"


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


def fetch_ha_history(entities: list[str], hours: int) -> pd.DataFrame:
    if not entities:
        return pd.DataFrame()

    now_utc = datetime.now(timezone.utc)
    start_utc = now_utc - timedelta(hours=hours)

    endpoint = f"{HA_URL}/api/history/period/{start_utc.isoformat()}"
    params = {
        "end_time": now_utc.isoformat(),
        "filter_entity_id": ",".join(entities),
        "minimal_response": "1"
    }
    headers = {
        "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        res = requests.get(endpoint, headers=headers, params=params, timeout=60)
        res.raise_for_status()
        raw_data = res.json()
    except Exception as e:
        logger.error(f"Failed to fetch history from HA API: {e}")
        return pd.DataFrame()

    dfs = []
    for entity_series in raw_data:
        if not entity_series:
            continue
        entity_id = entity_series[0].get("entity_id")
        series_records = []
        for state_obj in entity_series:
            ts = state_obj.get("last_changed")
            val = state_obj.get("state")
            if ts and val not in (None, "unavailable", "unknown"):
                try:
                    num_val = float(val)
                    series_records.append({"timestamp": ts, entity_id: num_val})
                except ValueError:
                    continue
        if series_records:
            s_df = pd.DataFrame(series_records)
            s_df["timestamp"] = pd.to_datetime(s_df["timestamp"])
            s_df.set_index("timestamp", inplace=True)
            dfs.append(s_df)

    if not dfs:
        return pd.DataFrame()

    combined = pd.concat(dfs, axis=1)
    combined.sort_index(inplace=True)
    combined.ffill(inplace=True)
    if combined.index.tz is None:
        combined = combined.tz_localize("UTC")
    combined = combined.tz_convert("Europe/Berlin")
    return combined


def call_gemini_structured(prompt: str, schema: dict | None, api_key: str, model: str) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    generation_config = {
        "response_mime_type": "application/json"
    }
    if schema:
        generation_config["response_schema"] = schema

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": generation_config
    }

    headers = {"Content-Type": "application/json"}
    res = requests.post(url, headers=headers, json=payload, timeout=60)
    res.raise_for_status()

    res_data = res.json()
    candidate = res_data["candidates"][0]
    raw_text = candidate["content"]["parts"][0]["text"]
    return json.loads(raw_text)


def publish_mqtt_discovery(task_id: str, task_config: dict, result_data: dict, mqtt_options: dict):
    clean_id = task_id.lower().replace("-", "_")
    base_topic = f"homeassistant/sensor/llm_{clean_id}"
    config_topic = f"{base_topic}/config"
    state_topic = f"{base_topic}/state"

    # Universeller Fallback für den primären Entitäts-State
    generic_fallback_template = (
        "{{ value_json.state if value_json.state is defined "
        "else (value_json.status if value_json.status is defined "
        "else (value_json.summary if value_json.summary is defined else 'OK')) }}"
    )
    primary_state_template = task_config.get("state_template", generic_fallback_template)

    discovery_payload = {
        "name": task_config.get("name", f"LLM {task_id.replace('_', ' ').title()}"),
        "unique_id": f"llm_runner_{clean_id}",
        "state_topic": state_topic,
        "value_template": primary_state_template,
        "json_attributes_topic": state_topic,
        "icon": task_config.get("icon", "mdi:brain"),
        "device": {
            "identifiers": ["ha_llm_runner"],
            "name": "HA LLM Runner Engine",
            "model": "Modular LLM Processor",
            "manufacturer": "Custom Automation"
        }
    }

    for key in ["unit_of_measurement", "device_class", "state_class"]:
        if key in task_config:
            discovery_payload[key] = task_config[key]

    auth = None
    if mqtt_options.get("mqtt_user"):
        auth = {
            "username": mqtt_options["mqtt_user"],
            "password": mqtt_options.get("mqtt_password", "")
        }

    host = mqtt_options.get("mqtt_host", "core-mosquitto")
    port = int(mqtt_options.get("mqtt_port", 1883))

    # 1. Discovery Config (retained)
    publish.single(
        topic=config_topic,
        payload=json.dumps(discovery_payload),
        retain=True,
        hostname=host,
        port=port,
        auth=auth
    )

    # 2. State & Attributes (retained)
    publish.single(
        topic=state_topic,
        payload=json.dumps(result_data),
        retain=True,
        hostname=host,
        port=port,
        auth=auth
    )
    logger.info(f"Published state to MQTT: {state_topic}")


def run_processor(processor_name: str, df: pd.DataFrame, task_config: dict):
    os.makedirs(PROCESSORS_DIR, exist_ok=True)
    processor_path = os.path.join(PROCESSORS_DIR, f"{processor_name}.py")
    if not os.path.exists(processor_path):
        raise FileNotFoundError(f"Processor script not found: {processor_path}")

    spec = importlib.util.spec_from_file_location(processor_name, processor_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if not hasattr(module, "process"):
        raise AttributeError(f"Processor '{processor_name}' must export function 'process(df, config)'")

    return module.process(df, task_config)


def execute_tasks():
    options = load_options()
    tasks_cfg = load_tasks_config()
    tasks = tasks_cfg.get("tasks", {})

    api_key = options.get("gemini_api_key")
    global_model = options.get("gemini_model", DEFAULT_MODEL)

    if not tasks:
        logger.info("No tasks defined in llm_tasks.yaml.")
        return

    for task_id, task_config in tasks.items():
        logger.info(f"--- Running Task: {task_id} ---")
        try:
            entity_map = task_config.get("entities", {})
            entity_list = list(entity_map.values()) if isinstance(entity_map, dict) else list(entity_map)
            hours = int(task_config.get("hours", 24))

            df = fetch_ha_history(entity_list, hours=hours)

            processor_name = task_config.get("processor")
            metrics, raw_data = {}, None
            if processor_name:
                metrics, raw_data = run_processor(processor_name, df, task_config)

            custom_prompt = task_config.get("prompt")
            json_schema = task_config.get("response_schema")
            task_model = task_config.get("model", global_model)

            if custom_prompt and api_key:
                serialized_data = raw_data if isinstance(raw_data, str) else json.dumps(raw_data, ensure_ascii=False)
                formatted_prompt = custom_prompt.format(
                    metrics=json.dumps(metrics, ensure_ascii=False),
                    data=serialized_data
                )
                result_payload = call_gemini_structured(formatted_prompt, json_schema, api_key, task_model)
            else:
                # Reiner Prozessor-Modus ohne LLM-Inferenz
                if isinstance(raw_data, str):
                    try:
                        parsed_data = json.loads(raw_data)
                    except Exception:
                        parsed_data = raw_data
                else:
                    parsed_data = raw_data

                result_payload = {
                    "updated_at": datetime.now().isoformat(),
                    **metrics,
                    "data": parsed_data
                }

            publish_mqtt_discovery(task_id, task_config, result_payload, options)
            logger.info(f"Task '{task_id}' processed successfully.")

        except Exception as e:
            logger.exception(f"Error executing task '{task_id}': {e}")


def main():
    logger.info("Starting HA LLM Runner Add-on...")
    os.makedirs(PROCESSORS_DIR, exist_ok=True)
    execute_tasks()

    options = load_options()
    cron_expr = options.get("schedule_cron", "0 6,13,20 * * *")
    logger.info(f"Cron scheduling initialized: '{cron_expr}'")

    while True:
        try:
            now = datetime.now()
            cron = croniter(cron_expr, now)
            next_run = cron.get_next(datetime)
            sleep_seconds = max(1, (next_run - now).total_seconds())
            logger.info(f"Next execution at {next_run.strftime('%Y-%m-%d %H:%M:%S')} (sleeping {int(sleep_seconds)}s)")
            time.sleep(sleep_seconds)
            execute_tasks()
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()