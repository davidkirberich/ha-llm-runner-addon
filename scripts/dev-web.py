"""Starts the add-on's web interface on this computer, without Home Assistant, MQTT or LLM calls.

    python scripts/dev-web.py                       # sample configuration in .dev-config/
    python scripts/dev-web.py --pull homeassistant.local   # copy of the production configuration
    python scripts/dev-web.py --live                # real task runs (see below)

Then open http://localhost:8099. Files are read from and saved to --config-dir. web.py and web/index.html
are loaded fresh on every start, so restart the script after changing them.

By default, Run only simulates a task: it waits two seconds and returns a dummy answer, so nothing is sent to
Home Assistant or the LLM and the memory stays untouched. With --live, tasks really run: set SUPERVISOR_URL
(e.g. http://homeassistant.local:8123), HA_TOKEN (a long-lived access token) and GEMINI_API_KEY first. The
results are then written to the real Home Assistant sensors.
"""
import argparse
import io
import os
import shutil
import subprocess
import sys
import tarfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_TASKS = """tasks:
  morning_briefing:
    name: Morning briefing
    target_sensor: sensor.llm_morning_briefing
    history_limit: 7
    entities:
      temperature: sensor.outdoor_temperature
    prompt: |
      Today is {weekday}, {today}. It is {temperature} degrees outside.
      Your last answers: {history}
"""


def pull(host: str, user: str, target: str):
    """Copies llm_tasks.yaml, processors/, memory/ and audit/ of the store version via SSH."""
    remote = (
        "set -e; cd /app_configs 2>/dev/null || cd /addon_configs; "
        "d=$(ls -d *_ha_llm_runner | grep -v '^local_' | head -1); [ -n \"$d\" ] || exit 3; "
        "cd \"$d\"; tar -cz --exclude=__pycache__ $(ls -d llm_tasks.yaml processors memory audit 2>/dev/null)"
    )
    print(f"Copying the production configuration from {host} ...", flush=True)
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", f"{user}@{host}", remote], capture_output=True)
    if result.returncode != 0:
        sys.exit(f"Copy failed: {result.stderr.decode(errors='replace').strip() or 'add-on folder not found'}")
    shutil.rmtree(target, ignore_errors=True)
    os.makedirs(target)
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:gz") as archive:
        archive.extractall(target, filter="data")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config-dir", default=os.path.join(REPO, ".dev-config"), help="default: .dev-config/")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--pull", metavar="HOST", help="replace --config-dir with a copy of the production configuration")
    parser.add_argument("--user", default="root", help="SSH user for --pull (default: root)")
    parser.add_argument("--live", action="store_true", help="run tasks for real instead of simulating them")
    args = parser.parse_args()

    config_dir = os.path.abspath(args.config_dir)
    if args.pull:
        pull(args.pull, args.user, config_dir)
    os.makedirs(config_dir, exist_ok=True)
    tasks_path = os.path.join(config_dir, "llm_tasks.yaml")
    if not os.path.exists(tasks_path):
        with open(tasks_path, "w", encoding="utf-8") as f:
            f.write(SAMPLE_TASKS)

    sys.path.insert(0, os.path.join(REPO, "ha-llm-runner"))
    import run as runner
    import web

    runner.CONFIG_DIR = config_dir
    runner.TASKS_CONFIG_PATH = tasks_path
    runner.PROCESSORS_DIR = os.path.join(config_dir, "processors")
    runner.MEMORY_DIR = os.path.join(config_dir, "memory")
    runner.AUDIT_DIR = os.path.join(config_dir, "audit")
    runner.OPTIONS_PATH = os.path.join(config_dir, "options.json")

    if not args.live:
        # No Home Assistant to ask for time zone and language
        runner._ha_config = {}

        def simulated_task(task_id, task_config, client, options):
            time.sleep(2)
            return {"prompt": str(task_config.get("prompt", "")), "result": f"(dev) simulated answer of '{task_id}'"}

        runner.execute_task = simulated_task

    web.start_web_server(runner, host="127.0.0.1", port=args.port)
    mode = "LIVE: tasks really run" if args.live else "task runs are simulated"
    print(f"Web UI: http://localhost:{args.port}  ({mode}; config: {config_dir}). Stop with Ctrl+C.", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
