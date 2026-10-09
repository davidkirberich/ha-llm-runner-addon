"""Starts the add-on's web interface on this computer with invented sample data.

    python scripts/dev-web.py            # then open http://localhost:8099
    python scripts/dev-web.py --port 8199

There is no connection to Home Assistant, MQTT or an LLM: every network connection that does not stay on this
computer is blocked, and Run only simulates a task (dummy answer after two seconds).

The sample data in .dev-config/ (ignored by git) is recreated on every start, so changes made in the web
interface are gone after a restart. web.py and web/index.html are loaded fresh on every start as well.
"""
import argparse
import base64
import ipaddress
import json
import os
import shutil
import socket
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(REPO, ".dev-config")

SAMPLE_TASKS = """\
tasks:
  plant_watering:
    name: Plant watering advice
    icon: mdi:sprout
    target_sensor: sensor.dev_plant_watering
    history_limit: 5
    hours: 24
    resample: 1h
    entities:
      soil_moisture: sensor.dev_balcony_soil_moisture
      outdoor_temperature: sensor.dev_outdoor_temperature
    prompt: |
      Today is {weekday}, {today}. Soil moisture: {soil_moisture} %, outside: {outdoor_temperature} degrees.
      Should the balcony plants be watered today? One sentence.
      Your last answers: {history}

  cat_feeder:
    name: Cat feeder check
    icon: mdi:cat
    entities:
      feeder_cam: "https://example.com/feeder/snapshot.jpg"
      feeder_weight: sensor.dev_feeder_weight
    prompt: |
      Look at the picture of the food bowl. Is there enough food left? The scale shows {feeder_weight} g.

  garage_door:
    name: Garage door summary
    icon: mdi:garage
    target_sensor: sensor.dev_garage_summary
    hours: 12
    data_processor: example_processor
    entities:
      door: cover.dev_garage_door
    prompt: |
      Summarise how often the garage door was opened in the last 12 hours.
      {data}

  weekly_plan:
    name: Weekly plan
    icon: mdi:calendar-week
    entities:
      calendar: calendar.dev_family
    prompt: |
      Write a short overview of the appointments of the coming week.
      {calendar}
"""

SAMPLE_PROCESSOR = '''\
"""Example processor: receives the history as a pandas DataFrame and returns the text for {data}."""


def process(df, task_config):
    return f"{len(df)} values"
'''

SAMPLE_MEMORY = {
    "plant_watering": [
        "No watering needed today, the soil is still moist.",
        "Water the plants this evening, it will be hot and dry.",
        "Light watering in the morning is enough.",
    ],
    "garage_door": ["The garage door was opened twice, both times in the morning."],
}

# 1x1 pixel PNG, so the audit view has an image to show
SAMPLE_IMAGE = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="


def block_external_network():
    """Allows connections to this computer only, so nothing can reach Home Assistant, MQTT or an LLM."""
    original_connect = socket.socket.connect

    def guarded_connect(sock, address):
        host = address[0] if isinstance(address, tuple) else address
        try:
            local = ipaddress.ip_address(str(host).split("%")[0]).is_loopback
        except ValueError:
            local = str(host).lower() == "localhost"
        if not local:
            raise ConnectionRefusedError(f"dev-web.py: network access to {host} is blocked")
        return original_connect(sock, address)

    socket.socket.connect = guarded_connect
    original_getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host not in (None, "localhost", "127.0.0.1", "::1"):
            raise socket.gaierror(f"dev-web.py: name lookup of {host} is blocked")
        return original_getaddrinfo(host, *args, **kwargs)

    socket.getaddrinfo = guarded_getaddrinfo


def create_sample_data(runner):
    shutil.rmtree(CONFIG_DIR, ignore_errors=True)
    os.makedirs(runner.PROCESSORS_DIR)
    with open(runner.TASKS_CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(SAMPLE_TASKS)
    with open(os.path.join(runner.PROCESSORS_DIR, "example_processor.py"), "w", encoding="utf-8") as f:
        f.write(SAMPLE_PROCESSOR)
    options = {"language": "en", "audit_retention_days": 30, "gemini_model": "dev-model"}
    with open(runner.OPTIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(options, f, indent=2)

    for task_id, texts in SAMPLE_MEMORY.items():
        runner.save_memory(task_id, [{"time": f"0{i + 1}.01.2026 07:00:00", "text": t} for i, t in enumerate(texts)])

    runner.create_audit_archive(
        "plant_watering", "Today is Monday ... Should the balcony plants be watered today?",
        "No watering needed today, the soil is still moist.", {"model": "dev-model", "duration_s": 1.2},
        json.dumps([{"time": "2026-01-05T06:00:00", "soil_moisture": 41.0}]), [], options)
    runner.create_audit_archive(
        "cat_feeder", "Look at the picture of the food bowl.", "The bowl is about half full.",
        {"model": "dev-model", "duration_s": 2.4}, "", [(SAMPLE_IMAGE, "image/png")], options)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()

    block_external_network()
    sys.path.insert(0, os.path.join(REPO, "ha-llm-runner"))
    import run as runner
    import web

    runner.CONFIG_DIR = CONFIG_DIR
    runner.TASKS_CONFIG_PATH = os.path.join(CONFIG_DIR, "llm_tasks.yaml")
    runner.PROCESSORS_DIR = os.path.join(CONFIG_DIR, "processors")
    runner.MEMORY_DIR = os.path.join(CONFIG_DIR, "memory")
    runner.AUDIT_DIR = os.path.join(CONFIG_DIR, "audit")
    runner.OPTIONS_PATH = os.path.join(CONFIG_DIR, "options.json")
    runner._ha_config = {}

    def simulated_task(task_id, task_config, client, options):
        time.sleep(2)
        return {"prompt": str(task_config.get("prompt", "")), "result": f"(dev) simulated answer of '{task_id}'"}

    runner.execute_task = simulated_task
    create_sample_data(runner)

    web.start_web_server(runner, host="127.0.0.1", port=args.port)
    print(f"Web UI: http://localhost:{args.port}  (sample data, simulated runs). Stop with Ctrl+C.", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
