# 🛡️ VAELEN - CONFIGURATION MANUAL

**Version:** 2.0
**Purpose:** Complete reference for every configurable parameter — both in `config.py` and in the live dashboard Settings.

---

## 0. Where does a setting actually live? (Read this first)

This is the single most important thing that changed since v1.0 of this manual: **`config.py` is no longer where you edit most settings.**

* `config.py` still holds the **camera list** (`STREAMS`) and the **starting defaults** for everything else — these are read once, at process start.
* Almost every operational setting (detection sensitivity, recording timing, thumbnails, retention, theme, AI analysis, ...) is now stored in **`pipeline_settings.json`**, edited live from the **dashboard's Settings page**, and takes effect either immediately (no restart) or after an automatic background restart of the pipeline — the dashboard tells you which.
* `config.py` reads `pipeline_settings.json` once at import time as a *fallback/default source* for the handful of values that still require a full pipeline restart to change (`YOLO_VERSION`, `MODEL_SIZE`, `TARGET_FPS`, `PRE_ROLL_SEC`, `POST_ROLL_SEC`, `CONFIDENCE_THRESHOLD`, `DETECTION_CLASSES`). Everything else in `pipeline_settings.json` is read directly, live, by whichever component needs it — deliberately, so those settings apply without a restart.

**Bottom line:** unless you're changing the camera list itself, use the dashboard, not this file.

---

## 1. CORE SYSTEM PATHS (AUTO-MANAGED)

All paths are resolved relative to the project root. You do not need to set these manually unless you want to override the default directory structure.

*   **`PROJECT_ROOT`**: The absolute path of your installation folder.
*   **`ALERTS_DIR`**: Destination for all `.mp4` event files, trigger screenshots, and their metadata sidecars.
    - *Location*: `{PROJECT_ROOT}/alerts/`
    - Contains an `archive/` subfolder with the identical structure for permanently kept recordings.
*   **`LOG_DIR`**: Storage for system and per-camera logs.
    - *Location*: `{PROJECT_ROOT}/logs/`
*   **`MODEL_PATH`**: The absolute path to your active YOLO weights (`.pt`), auto-downloaded on first use if missing.
*   **`OVERRIDE_F`**: `{PROJECT_ROOT}/stream_overrides.json` — which cameras are currently toggled on/off from the dashboard (independent of the `enabled` default in `STREAMS`).
*   **`SETTINGS_F`**: `{PROJECT_ROOT}/pipeline_settings.json` — where essentially everything described in this manual actually lives. Safe to inspect directly; edit it through the dashboard, not by hand, to avoid an invalid combination of values.

---

## 2. STREAM CONFIGURATION (`STREAMS` in `config.py`)

The `STREAMS` list defines the active cameras. This is the one thing you still edit directly in `config.py`. Each entry is a dictionary:

| Key | Type | Description |
| :--- | :--- | :--- |
| **`name`** | `string` | Unique ID (used in logs, filenames, alerts, and dashboard grouping). |
| **`url`** | `string` | The RTMP/RTSP stream origin URL. |
| **`enabled`**| `boolean`| Default state; can be overridden per-camera live from the dashboard (stored in `stream_overrides.json`, takes precedence over this default). |
| **`type`** | `string` | Currently supports `"VIDEO"`. |

**Example Configuration:**
```python
STREAMS = [
    {"name": "Entrance_Main", "url": "rtmp://192.168.1.50/pi/test", "enabled": True, "type": "VIDEO"},
    {"name": "Garden_North",  "url": "rtmp://192.168.1.50/garden/live", "enabled": False, "type": "VIDEO"}
]
```

---

## 3. AI MODEL SELECTION (dashboard: Settings → KI-Modell)

*   **`YOLO_VERSION`**: `"v10"`, `"v12"`, or `"v26"`. See the model comparison table in `README.md` for the trade-offs between them. Changing this restarts the pipeline (a different model needs to be loaded).
*   **`MODEL_SIZE`**: `"n"`, `"s"`, `"m"`, `"b"` (v10 only), `"l"`, or `"x"` — Nano through Extra-Large. Bigger sizes are more accurate but slower and use more VRAM.
*   The pipeline auto-detects your GPU generation at startup and safely enables/disables FP16 and cuDNN via a staged self-test — no manual tuning needed here regardless of which card you run (RTX 2060 through RTX 5090).

---

## 4. DETECTION & AI PARAMETERS (dashboard: Settings → Erkennung)

Controls the sensitivity of the detection engine.

*   **`DETECTION_CLASSES`**: **[CRITICAL]** A list of integer IDs representing the objects you want to track (COCO classes). Picked via the category checklist in the dashboard, not typed by hand.
    - `0`: Person (Default)
    - `1`: Bicycle
    - `2`: Car
    - *Example*: `[0, 2]` triggers alerts for both humans and vehicles.
*   **`CONFIDENCE_THRESHOLD`**: A float (`0.0` to `1.0`). How certain the AI must be before it triggers an alert.
*   **`SHOW_DETECTION_BOXES`**: Whether live camera previews and the live-view lightbox draw detection boxes in real time (color-coded per class). Purely cosmetic, reuses inference the pipeline already runs — no extra GPU cost either way.

---

## 5. TEMPORAL & RECORDING LOGIC (dashboard: Settings → Aufnahme-Timing)

Controls buffer management and video duration per event.

*   **`PRE_ROLL_SEC`**: Seconds of "pre-event" footage kept in the circular buffer, written to the *start* of the `.mp4` when an event triggers.
    - *Warning*: high values increase memory use.
*   **`POST_ROLL_SEC`**: Seconds to keep recording **after** the target object has left the frame, to capture the "exit" phase.
*   **`TARGET_FPS`**: Target frames per second for the output video. Also used to throttle how often the pipeline runs inference at all — source frames arriving faster than this are skipped before the (comparatively expensive) BGR conversion and detection step.

---

## 6. THUMBNAILS, FILMSTRIP & DISPLAY (dashboard: Settings → Anzeige / Speicher)

*   **`THUMBNAIL_FPS`** (0.5–5): Refresh rate for camera grid previews and the live-view lightbox. Live, no restart.
*   **`FILMSTRIP_COUNT`** (0 = off): Number of small+large preview frames captured per recording, starting right after pre-roll. Small frames (with detection boxes) power the hover-to-scrub preview in the dashboard; large frames (kept raw) are what gets sent to Ollama if AI analysis is enabled.
*   **`FILMSTRIP_INTERVAL_SEC`**: Seconds between filmstrip captures.
*   **`RETENTION_DAYS`** (0 = never): Auto-delete unarchived recordings older than this. Archived recordings are never touched.
*   **`THEME`**: `"dark"` or `"light"`.

---

## 7. OPTIONAL AI SCENE DESCRIPTION (dashboard: Settings → KI-Videoanalyse)

All off by default — no Ollama instance required unless enabled.

*   **`AI_ANALYSIS_ENABLED`**: Master on/off switch.
*   **`OLLAMA_URL`**: Endpoint of your Ollama instance, e.g. `http://localhost:11434`. A live reachability badge in this settings section shows whether it's currently answering.
*   **`OLLAMA_VISION_MODEL`**: Which model to send the filmstrip frames to. Pick from the dropdown of tested presets, or "Eigenes Modell…" for anything else you've pulled into Ollama. Note: Ollama has no native video-file input — every model gets the same image sequence (the large filmstrip frames), never the raw `.mp4`.
*   **`AI_ANALYZE_MAX_FRAMES`** (1–64): How many filmstrip frames to send per analysis. More frames = more context but more tokens/time per request.
*   Result is written as `<recording>.ai.json` (shown in the dashboard) and `<recording>.mp4.xmp` (Immich-compatible sidecar; note Immich's XMP support for *video* files specifically is worth verifying against your own Immich version).
*   A manual re-analyze button is available per recording in the dashboard (requires `FILMSTRIP_COUNT` > 0 for that recording).

---

## 8. OPERATIONAL MODES

Currently implemented:

*   **`RECORDING_MODE = "EVENT_DRIVEN"`** (only mode, not currently switchable)
    - **Workflow**: `IDLE` → *Detection Found* → `RECORDING` (Pre-roll + Event + Post-roll) → `IDLE`.
    - Efficient by design — only consumes disk/CPU/GPU encode time while something is actually happening.

---

## 9. LOGGING & MONITORING

Several distinct log files exist:

1.  **`logs/system_main.log`**: The `system_logger` — orchestrator lifecycle, GPU detection at startup, global errors.
2.  **`logs/{camera_name}.log`**: Per-camera detail logs — connection state, detection events, encoding.
3.  **`logs/pipeline_runtime.log`**: Combined stdout/stderr of the recording pipeline process when started via `start_detached.sh` — this is what the dashboard's built-in **Log** panel displays (last 100 lines, auto-refreshing while open), and where the optional AI-analysis subprocess's own output lands too.
4.  **`logs/watchdog.log`**: Written only if you've set up `watchdog.sh` via cron — records restart attempts of the dashboard.

For quick health checks without opening a log file at all: the dashboard's Hardware & System Status panel shows CPU/RAM/VRAM/GPU temp/disk/NVENC/NVDEC live, and `/health` is an unauthenticated endpoint suitable for external monitoring.

---

*Manual updated to reflect the current dashboard-first configuration model. Partly generated with AI assistance, consistent with the rest of this project.*
