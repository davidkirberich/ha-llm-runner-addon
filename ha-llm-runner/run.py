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
import paho.mqtt.client as mqtt

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


def extract_interaction_text(res_data: dict) -> str:
    """Extrahiert den Antworttext aus der Google Interactions API Response."""
    if "output_text" in res_data and res_data["output_text"]:
        return res_data["output_text"]

    steps = res_data.get("steps", [])
    chunks = []
    for step in steps:
        if step.get("type") == "model_output":
            for block in step.get("content", []):
                if block.get("type") == "text" and "text" in block:
                    chunks.append(block["text"])
    if chunks:
        return "".join(chunks)

    for out in res_data.get("outputs", []):
        if out.get("type") == "text" and "text" in out:
            chunks.append(out["text"])
    if chunks:
        return "".join(chunks)

    raise ValueError(f"No textual output found in interaction response: {res_data}")


def call_gemini_interactions(prompt: str, schema: dict | None, api_key: str, model: str) -> dict:
    """Ruft die Google Gemini Interactions API mit nativem response_format auf."""
    url = "https://generativelanguage.googleapis.com/v1beta/interactions"

    response_format = {
        "type": "text",
        "mime_type": "application/json"
    }
    if schema:
        response_format["schema"] = schema

    payload = {
        "model": model,
        "input": prompt,
        "response_format": response_format
    }

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key
    }

    res = requests.post(url, headers=headers, json=payload, timeout=60)
    res.raise_for_status()

    raw_text = extract_interaction_text(res.json())
    return json.loads(raw_text)


def publish_task_discovery(client: mqtt.Client, task_id: str, task_config: dict):
    """Erzeugt Sensor- und Button-Entitäten in HA via MQTT Discovery."""
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

    # 2. Button Discovery (generiert button.run_llm_<task_id> in Home Assistant)
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


def run_processor(processor_name: str, df: pd.DataFrame, task_config: dict):
    os.makedirs(PROCESSORS_DIR, exist_ok=True)
    processor_path = os.path.join(PROCESSORS_DIR, f"{processor_name}.py")
    if not os.path.exists(processor_path):
        raise FileNotFoundError(f"Processor script not found: {processor_path}")

    spec = importlib.util.spec_from_file_location(processor_name, processor_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if not hasattr(module, "process"):
        raise AttributeError(f"Processor '{processor_name}' must export 'process(df, config)'")

    return module.process(df, task_config)


def execute_task(task_id: str, task_config: dict, client: mqtt.Client, options: dict):
    logger.info(f"--- Executing Task: {task_id} ---")
    api_key = options.get("gemini_api_key")
    global_model = options.get("gemini_model", DEFAULT_MODEL)

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
        result_payload = call_gemini_interactions(formatted_prompt, json_schema, api_key, task_model)
    else:
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
        # Initial Discovery und Datenlauf bei Container-Start
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