# Agent Control

Lets an AI agent (Hermes, OpenClaw, or anything else calling the same API) operate vaelen directly — toggle cameras, tune settings, start/stop the pipeline, and search — instead of only submitting jobs. Built on top of the [External API](./REMOTE_API.md); uses the same API keys and the same `/api/v1/` base.

**Off by default.** Nothing in this section does anything until you explicitly turn it on in `agent_config.json`.

## Quick reference — the right tool for the job

| You want to... | Use | Not |
| :--- | :--- | :--- |
| Record something right now, for a specific duration, regardless of whether the pipeline is even running | `POST /cameras/<name>/quick_record` (form param `duration`) | ~~`/trigger`~~ — that needs the pipeline already running |
| Start an event-style recording on a camera whose process is already live | `POST /cameras/<name>/trigger` | |
| End a recording that's currently in progress | `POST /cameras/<name>/stop` | ~~`disable`~~ — disable does not touch an active recording |
| Turn a camera on/off for future use (not right now) | `POST /cameras/<name>/enable` or `/disable` | Takes up to ~15s to actually take effect, it's not instant |
| Find out what you're currently allowed to do | `GET /capabilities` | Always call this first if unsure — works even with everything else disabled |
| See what a notify-only camera has spotted without recording | `GET /detections` | |
| Read the description/topics/transcript of a specific recording | `GET /events/<filename>` | ~~Searching the filesystem~~ — you likely don't have filesystem access anyway, and this is the direct, reliable way to get it |

**Common mistake to avoid:** a 404 means the route doesn't exist on the server you're talking to — it is never a sign that the documentation itself is wrong. If a documented route 404s, the most likely cause is that the server process hasn't picked up the latest code yet (needs a restart), not a wrong path. Ask the human to verify before guessing at alternate paths.

## The permission model

`agent_config.json` has one master switch and a per-capability toggle:

```json
{
  "agent_control_enabled": false,
  "capabilities": {
    "search": { "enabled": true },
    "cameras_toggle": { "enabled": true },
    "pipeline_control": { "enabled": true },
    "settings_change": { "enabled": true },
    "delete": { "enabled": false },
    "export": { "enabled": false }
  }
}
```

Both the master switch **and** the specific capability must be `true` for a call to succeed — turning the master switch on doesn't retroactively grant every capability, each stays off unless you also flip it. Editing the file takes effect immediately, no restart needed (read fresh on every request).

| Capability | Risk | What it allows |
| :--- | :--- | :--- |
| `search` | Low | Read-only. Search recordings by description, topic, transcript, person. |
| `cameras_toggle` | Low | Enable/disable individual cameras. Never touches URLs or credentials. |
| `pipeline_control` | Medium | Start/stop the whole recording pipeline. While stopped, nothing records. |
| `manual_trigger` | Medium | Force-start a recording on any camera right now, switch a camera into notify-only mode (YOLO keeps detecting, but recording waits for an explicit trigger instead of starting automatically), and read recent detections. Off by default even though most other capabilities default on — creates real recordings and uses storage. |
| `settings_change` | Medium | Change a fixed allowlist of tuning settings (see below). Credentials, URLs, and export destinations are never reachable through this capability — enforced in code, not just by convention. |
| `delete` | High | **Not implemented.** No route exists for it. Listed here as a placeholder for the decision, not a working toggle. |
| `export` | Medium | **Not implemented.** Same as above. |

## Notify-only mode: putting the agent "in front of" recording

Normally, a YOLO detection immediately starts a recording. `manual_trigger` adds a second mode: switch a camera to **notify-only**, and detections stop auto-recording — they just get reported (a file the agent can poll, plus an MQTT event if configured). The agent decides whether that report is worth an actual recording, and if so, calls the trigger endpoint.

```
POST /agent/cameras/<name>/notify_only/enable    # switch this camera to report-only
POST /agent/cameras/<name>/notify_only/disable   # back to normal auto-record
GET  /agent/detections                            # what's been seen recently, per camera
POST /agent/cameras/<name>/trigger                # start a recording on this camera right now
```

Two things worth knowing:
- **`notify_only` takes effect after that camera's process restarts** (stop/start the pipeline), not instantly — it's read once when the camera process starts, the same way most per-camera settings are.
- **The camera still has to be running and detecting** for any of this to work — turning a camera fully off (`cameras_toggle`) means nothing is watching it, so there's nothing to notify about. What varies with notify-only is whether a detection *automatically* records, not whether detection happens at all.

## Settings allowlist

`settings_change` can only touch: `CONFIDENCE_THRESHOLD`, `TARGET_FPS`, `PRE_ROLL_SEC`, `POST_ROLL_SEC`, `DETECTION_CLASSES`, `AI_TOPICS`, `AI_TOPICS_THRESHOLD`, `AI_TOPICS_ENABLED`, `AI_ANALYZE_MAX_FRAMES`, `ANOMALY_DETECTION_ENABLED`, `FACE_MIN_CONFIDENCE`.

`DETECTION_CLASSES` is the list of COCO class IDs YOLO watches for — an agent with `settings_change` can already change what triggers a recording (e.g. `{"DETECTION_CLASSES": [0, 16]}` for person + dog) without needing `manual_trigger` at all.

Anything outside this list — MQTT credentials, camera URLs, export paths, watchfolder paths — is rejected with a 403, even if `settings_change` is enabled. A request that mixes an allowed and a disallowed key is rejected entirely; nothing partially applies.

## Orientation endpoint (start here)

```
GET /api/v1/agent/capabilities
```

One call, tells an agent everything it needs before doing anything else: which capabilities are currently enabled, the risk level and description of each, which settings keys it's allowed to touch — and, only for capabilities that are actually on, the concrete data that goes with them (camera list if `cameras_toggle` is enabled, pipeline running/stopped if `pipeline_control` is enabled). Always reachable with a valid API key regardless of the master switch — it's read-only self-description, not an action, so there's nothing to gate. Saves an agent from finding out what it can do by trial and error (and the resulting stream of 403s).

## Proactive notifications (agent webhook)

Instead of polling, an agent can be notified automatically after each analyzed recording. Configure `AGENT_WEBHOOK_URL` in Settings → Agent Webhook (or `pipeline_settings.json` directly) — vaelen POSTs a JSON payload there once analysis finishes:

```json
{
  "event": "recording_analyzed",
  "camera": "Entrance",
  "filename": "Entrance_EVENT_20260905_120000.mp4",
  "description": "A delivery van pulls up and a package is left at the door.",
  "topics": {"delivery": 92},
  "anomaly": false,
  "anomaly_score": null,
  "timestamp": 1788600000.0
}
```

Check "Only notify for anomalies" to only get called for events flagged by Anomaly Detection (`"event": "anomaly"` instead) — useful if you want the agent to only react to the unusual cases, not every delivery. Same fire-and-forget guarantee as MQTT: an unreachable agent never delays or affects the pipeline.

## Endpoints

All under `/api/v1/agent/`, same `Authorization: Bearer <key>` / `X-API-Key` auth as the rest of the External API.

| Method & path | Capability | Notes |
| :--- | :--- | :--- |
| `GET /capabilities` | *(always reachable)* | Orientation call — see above. |
| `GET /cameras` | `cameras_toggle` | Name, enabled, audio_enabled — URL never included. |
| `POST /cameras/<name>/enable` | `cameras_toggle` | Optional JSON/form body `{"audio_enabled": true/false}` also sets audio in the same call. Omit it to leave audio unchanged. Takes effect within ~15s (the pipeline's monitoring interval), not instantly. |
| `POST /cameras/<name>/disable` | `cameras_toggle` | Same optional `audio_enabled` body as above. Same ~15s delay to actually stop the running process. |
| `POST /cameras/<name>/audio/enable` | `cameras_toggle` | Audio only — doesn't touch the camera's enabled state. |
| `POST /cameras/<name>/audio/disable` | `cameras_toggle` | Audio only — doesn't touch the camera's enabled state. |
| `GET /settings` | `settings_change` | Only allowlisted keys are returned, even reading. |
| `POST /settings` | `settings_change` | JSON body of key/value pairs, allowlist enforced. |
| `GET /pipeline/status` | `pipeline_control` | |
| `POST /pipeline/start` | `pipeline_control` | |
| `POST /pipeline/stop` | `pipeline_control` | |
| `GET /search?q=...` | `search` | Same underlying search as the dashboard. |
| `GET /events/<filename>` | `search` | Full metadata for one recording (description, topics, transcript, faces, anomaly status). Checks both active and archived recordings. |
| `GET /summaries` | `search` | The same daily/weekly summaries shown on the dashboard. |
| `GET /system_status` | `search` | Hardware stats — CPU/RAM/VRAM, GPU temperature. |
| `POST /reanalyze/<filename>` | `manual_trigger` | Re-runs the AI analysis pipeline on an existing recording. Runs in the background. |
| `POST /anomaly/train` | `manual_trigger` | Retrains anomaly-detection baselines for all cameras with enough history. Form param `lookback_days` (default 30). |
| `POST /cameras/<name>/trigger` | `manual_trigger` | Force-start a recording right now. Rejected (400) if the camera is disabled. |
| `POST /cameras/<name>/stop` | `manual_trigger` | Ends a currently active recording immediately. **Not the same as `cameras_toggle`/disable** — disable only affects the next pipeline start, it does not stop an already-running recording. |
| `POST /cameras/<name>/quick_record` | `manual_trigger` | Ad-hoc recording **independent of the pipeline** — works even if the pipeline is stopped or the camera is disabled there. Form param `duration` (seconds, default 30, capped at 300). Returns a `job_id` immediately (202). |
| `GET /quick_record/<job_id>` | `manual_trigger` | Status of a quick-record job: `recording` → `done`/`failed`, with `output_path` once finished. |

### Quick record vs. trigger

Two different tools for "record something now":

- **`/trigger`** — only works while the pipeline is running and that camera's process is already alive. Starts a normal event-style recording (pre-roll + post-roll), not a fixed duration. Use `/stop` (not `disable`) to end it exactly when you want.
- **`/quick_record`** — a completely separate, lightweight path that connects to the camera directly via ffmpeg for exactly the requested duration. Works whether or not the pipeline is running at all. This is the right tool for "just record a minute of this camera right now."
| `POST /cameras/<name>/notify_only/enable` | `manual_trigger` | Switch to report-only. Takes effect on next camera process restart. |
| `POST /cameras/<name>/notify_only/disable` | `manual_trigger` | Back to normal auto-record. |
| `GET /detections` | `manual_trigger` | Most recent detected classes per camera, most recent first. |

## Why delete and export aren't here yet

Deliberate. Both are meaningfully higher-stakes than the rest (delete is irreversible; export can move data off the box). They're listed in the config as a placeholder for the decision, but no route implements them — enabling the flag today does nothing, by design. If/when they're built, they'll ship with the same allowlist-and-explicit-testing approach as everything else here, not bolted on quickly.

## Example (curl)

```bash
# Start here -- see what's currently allowed
curl https://your-vaelen-host:19473/api/v1/agent/capabilities \
  -H "Authorization: Bearer idg_xxxxxxxxxxxx"

# Check what's currently allowed to run
curl https://your-vaelen-host:19473/api/v1/agent/pipeline/status \
  -H "Authorization: Bearer idg_xxxxxxxxxxxx"

# Disable a camera
curl -X POST https://your-vaelen-host:19473/api/v1/agent/cameras/Backyard/disable \
  -H "Authorization: Bearer idg_xxxxxxxxxxxx"

# Search
curl "https://your-vaelen-host:19473/api/v1/agent/search?q=delivery" \
  -H "Authorization: Bearer idg_xxxxxxxxxxxx"
```
