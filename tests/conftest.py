import sys
from pathlib import Path

import pytest

# Mirror the container layout (/app/run.py + /app/llm_providers) so imports resolve the same way
ADDON_DIR = Path(__file__).resolve().parents[1] / "ha-llm-runner"
sys.path.insert(0, str(ADDON_DIR))


@pytest.fixture(autouse=True)
def isolated_time_zone(monkeypatch):
    # Never ask a real Home Assistant for its time zone during tests
    import run
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setattr(run, "_ha_config", {})


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    """Redirects the add-on folder (/config) and HA's folder (/homeassistant) into tmp_path."""
    import run
    addon_dir, ha_dir = tmp_path / "addon_config", tmp_path / "homeassistant"
    addon_dir.mkdir()
    ha_dir.mkdir()
    paths = {
        "CONFIG_DIR": addon_dir,
        "HA_CONFIG_DIR": ha_dir,
        "TASKS_CONFIG_PATH": addon_dir / "llm_tasks.yaml",
        "PROCESSORS_DIR": addon_dir / "processors",
        "MEMORY_DIR": addon_dir / "memory",
        "AUDIT_DIR": addon_dir / "audit",
        "LEGACY_TASKS_CONFIG_PATH": ha_dir / "llm_tasks.yaml",
        "LEGACY_PROCESSORS_DIR": ha_dir / "scripts" / "processors",
    }
    for name, value in paths.items():
        monkeypatch.setattr(run, name, str(value))
    monkeypatch.setattr(run, "TASK_STATUS", {})
    monkeypatch.setattr(run, "MQTT_STATUS", {"connected": False})
    monkeypatch.setattr(run, "_mqtt_client", None)
    return paths
