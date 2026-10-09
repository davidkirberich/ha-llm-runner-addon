# HA LLM Runner Add-on

A modular Home Assistant Add-on for orchestrating LLM-powered data processing pipelines, predictive insights, and time-series analyses using Google Gemini and MQTT Discovery.

---

## Features

- **Persistent Across Reboots via MQTT Discovery:** Automatically provisions entities in Home Assistant with `retain: true`. No volatile REST states, eliminating data loss during host or core restarts.
- **Strictly Typed Structured Outputs:** Enforces valid JSON directly at the API level via Google Gemini, avoiding fragile regex or text parsing.
- **Full Recorder Integration:** Publishes primary states for immediate tracking while storing structured arrays (e.g., multi-day records) in entity attributes for long-term database storage.
- **Custom Local Processors:** Allows to integrate custom preprocessing pipelines from `/config/scripts/processors/` before feeding data to the LLM.
- **Zero Core Dependency Conflicts:** Runs in an isolated Docker container with dedicated Python packages, independent of Home Assistant Core updates.

---

## Architecture

```text
[ Home Assistant Core ] 
       │  (History REST API: Read-only)
       ▼
[ HA LLM Runner (Container) ] ◄──► [ Google Gemini API ] (Structured JSON)
       │
       ▼  (MQTT Discovery + Retain)
[ Mosquitto Broker ] ────► [ Home Assistant State Machine & Recorder ]

# License
This project is licensed under the GNU General Public License v3.0.