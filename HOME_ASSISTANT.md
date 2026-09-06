# Home Assistant / MQTT Integration

vaelen can publish camera events to an MQTT broker, with optional Home Assistant auto-discovery — no YAML required to get the entities into HA.

## What you get

Per enabled camera, two entities:

* **`<Camera> Recording`** — a `binary_sensor` (device class `motion`) that turns `ON` while a recording is in progress (covers both the active detection phase and the post-roll buffer) and `OFF` once it finishes.
* **`<Camera> Last Event`** — a `sensor` holding the AI-generated description of the most recent recording, updated once post-processing finishes.

Both are grouped under one Home Assistant device per camera (`vaelen <Camera>`), so they show up together under Settings → Devices & Services → MQTT → Devices.

## Prerequisites

1. A running MQTT broker (Mosquitto is the common self-hosted choice; Home Assistant's own **Mosquitto broker add-on** works fine too if you run HA OS/Supervised).
2. The `paho-mqtt` Python package installed in vaelen's virtual environment:
   ```bash
   source .venv/bin/activate
   pip install paho-mqtt
   ```
   (Already listed in `requirements.txt` — a normal `pip install -r requirements.txt` picks it up.)
3. Home Assistant's **MQTT integration** configured and pointed at the same broker (Settings → Devices & Services → Add Integration → MQTT).

## Enabling it in vaelen

In the dashboard: **Settings → Home Assistant / MQTT**

| Field | Notes |
| :--- | :--- |
| Enable | Off by default |
| Broker host | IP or hostname of your MQTT broker |
| Broker port | Default `1883` (unencrypted) — use whatever port your broker actually listens on |
| Username / Password | Optional, only if your broker requires auth |
| Topic prefix | Default `vaelen` — change this if you're already using that namespace for something else, or if you run multiple vaelen instances and want to tell them apart |
| Publish Home Assistant MQTT Discovery config | On by default — turn off if you only want the raw topics for your own automations and don't want HA auto-creating entities |

No pipeline restart is needed — MQTT settings are read fresh (with a short cache) on every publish.

## Raw topics (useful even without Home Assistant)

If you'd rather wire this into something other than Home Assistant — Node-RED, openHAB, your own script — these are the topics published under `<prefix>/`:

| Topic | Payload | Retained |
| :--- | :--- | :--- |
| `<prefix>/<camera>/recording` | `ON` / `OFF` | Yes |
| `<prefix>/<camera>/last_event_summary` | Plain-text AI description (truncated to 255 chars) | Yes |
| `<prefix>/<camera>/event` | Full JSON: `{"camera", "filename", "description", "topics", "timestamp"}` | No |

Camera names are sanitized for the topic path (spaces and special characters become `_`), matching the name shown in the dashboard.

## Reliability note

MQTT publishing is deliberately **fire-and-forget**: the actual network call runs in a short-lived background thread, never on the recording pipeline's own thread. If your broker is down, slow, or unreachable, recordings and detection continue completely unaffected — you'll just see a warning in the log (`⚠ [MQTT] Publish an '...' fehlgeschlagen`) instead of any impact on the pipeline itself.

## Example automations

A couple of starting points once the entities exist in Home Assistant:

**Notify on any recording:**
```yaml
automation:
  - alias: "vaelen - notify on recording"
    trigger:
      - platform: state
        entity_id: binary_sensor.vaelen_entrance_recording
        to: "on"
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "Entrance camera"
          message: "Recording started"
```

**Notify with the AI description once analysis finishes:**
```yaml
automation:
  - alias: "vaelen - notify with description"
    trigger:
      - platform: state
        entity_id: sensor.vaelen_entrance_last_event
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "Entrance camera"
          message: "{{ trigger.to_state.state }}"
```

For anything needing the full topic list or the raw JSON event (e.g. to check which detection topics matched), subscribe to `<prefix>/<camera>/event` directly with an MQTT trigger instead of using the sensor entity.
