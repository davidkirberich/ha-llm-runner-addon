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
    monkeypatch.setattr(run, "_ha_time_zone", "")
