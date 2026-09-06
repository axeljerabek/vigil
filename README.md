# vigil

<img src="vigil-logo.svg" alt="vigil logo" width="1000">

> **A note on the name:** this project is mid-rename — "vigil" collides with an existing commercial NVR product and several other open-source projects. Treat every mention below as a placeholder until the rename lands; the architecture and features are what matter.

**Most NVR software is fundamentally about video: capture it, store it, let you scroll through it faster. vigil starts from a different premise — video is just the raw material. What actually matters is the information that several specialized AI models extract from it, working together, and increasingly, what you can *ask* about that information in plain language instead of scrubbing through a timeline yourself.**

A single clip already gets a plain-language description, a topic classification, a transcript, and named faces — four independent models handing off to each other automatically. But the real point isn't any one of those signals in isolation. It's that they accumulate into something you can question. **Ask vigil** is where this becomes concrete: type a real question — *"was anyone at the door yesterday afternoon who wasn't a delivery?"* — and vigil searches by meaning across every recording's accumulated understanding, then has a local LLM read the relevant ones and actually answer you, with the source recordings linked underneath. That's not a search bar with autocomplete. It's the system's own accumulated understanding of your property, made queryable.

Instead of one AI trying to do everything, vigil chains together several small, specialized models — a detector, an audio listener, a pose analyzer, a vision-language model, a face model, a text-embedding model — each doing the one job it's good at, with automatic handoffs between them. Everything runs on your own GPU. No cloud, no subscription, nothing leaves your network.

![Architecture Overview](architecture_overview.png)

[→ Full detailed architecture](ARCHITECTURE.html)

---

## What makes vigil different

**You can ask it questions, not just search it.** Type a real question into "Ask vigil" — not keywords, an actual sentence — and it finds the recordings that matter via semantic search, then hands them to a local LLM to synthesize an honest, direct answer with the specific recordings linked underneath. If nothing in your footage actually answers the question, it says so instead of guessing. This is the clearest expression of vigil's whole premise: the value isn't the video, it's the understanding built on top of it, and that understanding should be something you can have a conversation with.

**It hears what it can't see.** An independent audio model (CLAP) listens continuously, with zero fixed categories — type "glass breaking" or "dog barking" into the dashboard in plain English and it's live immediately, no retraining. A break-in two rooms away, in the dark, out of frame — vigil still knows, and that knowledge feeds into the same searchable pool everything else does.

**It notices when someone's in trouble.** One pose model, reused at zero extra GPU cost from a person already spotted by the main detector, reads six independent behavior signals: a fall, a raised-hands distress gesture, loitering, unusually fast movement, sustained close proximity between people, and which way someone's facing. Each is its own switch with its own threshold.

**It finds the recording that doesn't fit — without you writing a single rule.** A per-camera Isolation Forest model learns what "normal" looks like over time and flags statistical outliers automatically, recycling the same embedding semantic search already computes. Zero extra GPU cycles.

**It learns — and doesn't forget.** Once a face is identified, its recognition data is archived independently of any single video. Clean up old footage a year later, and vigil still recognizes that person tomorrow. The same philosophy now extends to vehicles: a recognized license plate can be named once and matched automatically on every future sighting, independent of the video it first appeared in.

**It treats a live stream as a live stream, not a file to wait for.** Point vigil at a folder and it doesn't just wait for finished video files — if what lands there is actually a growing, streamable source (a live camera export, an MPEG-TS feed, a YouTube/Twitch/Vimeo URL), it connects immediately and runs the exact same detection pipeline a real camera would, instead of sitting idle until the file is "done."

**An AI agent can run it for you.** A native MCP server exposes the same tools any script can call over the REST API — so an agent like Claude can check what a camera saw, toggle cameras, kick off a recording, search past events, and increasingly, *ask* about them the same way you would. A layered, off-by-default permission system decides exactly what it's allowed to touch; deletion is intentionally unreachable no matter what's toggled on.

**It records on demand, pipeline running or not.** Ask for thirty seconds from a specific camera right now, and it just happens — connects directly, records the exact duration, runs the same AI analysis afterward — whether or not the detection pipeline is even active.

**It knows where it's putting your footage — and warns you before it's a problem.** Recordings can live on a different drive than the app itself, with a live storage-status view (free space, write access) for every configured location, so "the disk quietly filled up" isn't a surprise you find out about the hard way.

---

## Where this is headed

The individual AI signals above — description, topics, transcript, faces, plates, pose, anomaly score — are still fundamentally *per-recording*. The next real step for vigil isn't another detector; it's making that understanding *compound across recordings, cameras, and time* the way a person's memory does. Concretely, that means: Ask vigil reasoning across a whole day or week instead of a single clip's context window, a face and a named vehicle getting linked into one "entity" the system tracks together ("the blue sedan that visits every Tuesday, driven by someone already recognized at the door"), and a persistent, queryable model of *routines* — so an anomaly isn't just "statistically unlike this camera's history" but "unlike what this person or vehicle normally does." The building blocks (named people, named vehicles, semantic search, an LLM already in the loop) are already there; what's missing is the layer that stitches them into one continuous understanding instead of many separate ones.

---

## vigil vs. Motion vs. Frigate

| | Motion / MotionEye | Frigate NVR | vigil |
| :--- | :--- | :--- | :--- |
| Core premise | Record on pixel change | Record on object detection | Understand, then let you *ask* about it |
| Trigger | Pixel difference | Object detection (YOLO) | YOLO + audio (CLAP) + pose/behavior |
| Natural-language query | None | Semantic search | Semantic search **+ LLM-synthesized answers with cited sources** |
| Model interaction | None | Isolated / parallel | Detection feeds the description prompt; all signals share one search index |
| Face recognition | None | External / third-party | In-process; identity survives video deletion |
| Vehicle / plate recognition | None | None | In-process, same "named entity" model as faces |
| Anomaly detection | None | None | Isolation Forest, zero extra GPU cost |
| Live import sources | — | — | Watchfolder treats a growing file, or a YouTube/Twitch/Vimeo URL, like a real camera |
| Agent control | None | None | Native MCP server + REST, permissioned |
| Ad-hoc recording | — | — | Works even with the pipeline stopped |
| Home Assistant | Basic webhooks | Deep UI integration | Native MQTT auto-discovery |
| Photo manager export | Standard MP4 | Internal DB/API | Immich-compatible XMP sidecars |

**[Motion/MotionEye](https://motion-project.github.io/)** triggers on pixel differences — reliable, decades-old, but it can't tell a cloud shadow from an intruder. Choose it if you want a simple daemon on minimal hardware with no GPU.

**[Frigate](https://frigate.video/)** is the closer, more mature comparison — it already does AI detection, semantic search, scene descriptions, transcription, and face recognition, with a bigger community and wider hardware support (Coral, Hailo, Apple Silicon). If you want a battle-tested, broadly-compatible NVR, **Frigate is very likely the better choice today.**

Choose vigil if the thing you actually want is an answer, not a recording — one strong GPU doing real reasoning about what it's seen and heard, queryable in plain language, with the option to hand an AI agent the keys.

---

## What it can do

**Ask & understand**

- 🆕 **Ask vigil** — a real question in, semantic search across everything vigil has seen, a local LLM answer out, with the specific source recordings linked
- YOLO object detection (v10/v12/26, your choice) as the trigger — not blind pixel-diffing
- CLAP audio triggers on sound alone, matched against categories *you* type
- **Six independent pose/behavior signals** from one model, reused at zero extra cost: fall detection, raised-hands distress gesture, loitering, fast movement, sustained close proximity, and head/gaze orientation
- Ollama vision model writes a plain-language description of every clip, aware of what you're actually watching for
- User-defined topic classification (`break-in`, `mail carrier`, anything) with confidence scores
- Whisper transcribes speech; InsightFace recognizes and groups people — **identity persists even after the source video is deleted**
- 🆕 **License plate recognition** (foundation) — vehicle detection + OCR on the same filmstrip frames, matched against named vehicles the same way faces are
- Isolation Forest anomaly detection flags recordings that don't match a camera's usual pattern

**Connect & control**

- **MCP Server** — native tools exposing the full agent-control surface to any MCP-compatible client (Claude Desktop, etc.), not just raw HTTP
- **External API** — submit video for processing, get a webhook when it's done, pull a video segment or the full enriched metadata back
- **Quick Record** — ask for N seconds from any camera right now, independent of whether the detection pipeline is even running
- **Agent Control** — let an AI agent toggle cameras, tune settings, start/stop recording, search, retrain anomaly baselines, and get proactively notified via webhook — through a permission system with a master switch and per-capability toggles. **Off by default.**
- Home Assistant / MQTT integration with auto-discovery

**Capture**

- RTMP, RTSP, MJPEG, USB/V4L2 webcams, or a watched import folder — any mix, per camera
- 🆕 **Live watchfolder import** — a growing file or a YouTube/Twitch/Vimeo URL dropped into the watchfolder is treated as a live camera the moment it's recognized as streamable, not held until it "finishes"
- Packet-copy recording where possible (~1000× cheaper than re-encoding), automatic real-encoding fallback for cameras that need it
- Automatic HEVC/codec fixing so everything actually plays back in a browser

**Find, review & manage**

- Semantic search across descriptions, topics, transcripts, and people — by meaning, not just keywords
- Daily/weekly AI-written summaries of what happened
- Star ratings, personal notes, a user-chosen profile photo per person, and full Immich-compatible XMP export
- 🆕 **Storage management** — point recordings at a different drive than the app itself, with a live per-folder disk-space and write-access dashboard so a filling disk doesn't sneak up on you
- 🆕 **Settings tucked out of the way** — Cameras, System Settings, Hardware Status, Storage, External API, and the Log live behind a single gear icon instead of cluttering the main view; the dashboard itself is just cameras and recent activity
- 🆕 Faster review: previous/next navigation and keyboard shortcuts in the lightbox, playback speed and autoplay preference remembered across recordings, one-key delete with Enter-to-confirm, and a visible highlight when a recording matched one of your topics

Every AI feature above is **off by default** and independently toggleable. Run it as a pure detection-triggered recorder, or turn on everything.

---

## Documentation

| | |
| :--- | :--- |
| [INSTALL.md](./INSTALL.md) | Setup, virtual environment, dependencies |
| [DOCKER.md](./DOCKER.md) | Containerized install |
| [REMOTE_API.md](./REMOTE_API.md) | External API — job submission, webhooks, video/segment delivery |
| [AGENT_CONFIG.md](./AGENT_CONFIG.md) | Agent Control — permissions, endpoints, quick-reference, rollout |
| [MCP_SERVER.md](./MCP_SERVER.md) | MCP server setup — tools, Claude Desktop config |
| [HOME_ASSISTANT.md](./HOME_ASSISTANT.md) | MQTT setup, entities, example automations |

---

<details>
<summary><strong>Full feature details (click to expand)</strong></summary>

### Ask vigil
* A dedicated, prominent input on the main dashboard — a real question, not a keyword search.
* Runs the question through the existing semantic search index (the same one powering the regular search bar) to find the most relevant recordings — descriptions, topics, transcripts, and named people all count.
* Hands the matched recordings' timestamps, cameras, and descriptions to a locally hosted Ollama LLM with an explicit instruction to answer only from what's actually there, and to say so honestly if nothing found actually answers the question — not to guess or fill in gaps.
* The answer is shown with the specific source recordings linked underneath, so it's checkable rather than a black box.
* No new AI model or infrastructure — this is the existing search index and the existing Ollama connection, recombined into a conversational front end.

### Camera Input
* **RTMP, RTSP, MJPEG, and local USB/V4L2 webcams** (`/dev/video0` etc.) — just point a camera's URL field at any of these, no separate configuration needed. RTSP connections use TCP transport by default (more robust against packet loss than the ffmpeg default of UDP).
* **Automatic recording-path selection per camera:** cameras whose source codec plays back reliably in a browser (H.264, VP9, AV1) are recorded via packet copy — the already-compressed stream is written straight to disk, no re-encoding, ~1000× cheaper per frame. Cameras that don't (MJPEG, most raw USB webcam feeds) are recorded via real encoding instead (NVENC-accelerated where available, software fallback otherwise). Decided automatically per camera at connection time.
* **Watchfolder Import:** point vigil at a folder instead of a camera, and every video file dropped in there gets treated one of two ways. If a fast streamability probe recognizes it as an already-playable format (MPEG-TS, or an MP4 with its index at the front), it's connected immediately as a **live source** — a real, dynamically spawned camera process running the full detection pipeline, appearing as a status banner on the dashboard the moment it's active. Anything else falls back to the original behavior: wait for the file to stop growing, then import it as a finished recording. A persisted marker prevents a file that's left in place (rather than deleted after import) from being re-processed on every poll.
* **Platform sources:** a YouTube, Twitch, or Vimeo URL dropped into the watchfolder is resolved and bridged through `yt-dlp` into the same live-source path as a streamable file — auto-restarting if the underlying URL expires, decoupling vigil's own reconnect logic from platform-specific stream lifetimes.
* **Automatic playback-codec fix on import:** if an imported file's codec won't play reliably in a browser (HEVC being the common offender), it's transcoded to H.264 automatically — GPU-accelerated where available. Already-compatible video is only re-wrapped, never needlessly re-encoded.
* **Quick Record:** an ad-hoc, fixed-duration recording from any camera, completely independent of the detection pipeline's state — works even with the pipeline stopped or that camera disabled everywhere else.

### Detection & Recording
* **Switchable AI Backends:** YOLOv10, YOLOv12, and YOLO26, Nano through Extra Large.
* **Event-Driven Recording** with configurable pre-roll/post-roll buffers.
* **"Why did this trigger" readout** on every recording — object class and confidence, or the matched audio category — right in the dashboard.
* **Live Detection-Box Overlays** in the camera grid and live-view lightbox, reusing inference the pipeline already runs.
* **Trigger Screenshots** with the detection box drawn in, plus a confidence badge on the thumbnail.
* **Configurable Filmstrip Thumbnails** — reservoir sampling across the entire event plus a guaranteed final-frame slot, so long events still get full coverage.
* **Resilient by design:** cameras auto-restart on crash/disconnect; a failed AI model load keeps retrying in the background instead of going silently blind.
* **GPU-Aware Startup** detects the installed GPU (Turing through Blackwell) and self-tests FP16/cuDNN safety — same codebase runs unmodified from an RTX 2060 to an RTX 5090.
* **Accurate Timing:** wall-clock-based frame timestamps, so a network stall shows as a pause, not sped-up playback.
* **Race-Safe Model Download** for the YOLO checkpoint on first run.
* **Manual trigger/stop:** force-start or immediately end a recording on a specific camera on demand.

### Pose Estimation & Behavior Detection (optional)
* One small pose model, run only on frames where a person is already detected by the main detector — no extra GPU cost on empty scenes.
* **Fall detection:** torso-angle heuristic, confirmed over several consecutive frames so a brief bend doesn't read as a fall.
* **Raised-hands distress signal:** both wrists held well above the shoulders, confirmed over a shorter window than a fall.
* **Loitering:** a person staying in roughly the same spot for longer than a configurable duration.
* **Fast movement:** speed measured in "body heights per second," not pixels — the same real-world pace reads the same regardless of distance from the camera.
* **Close proximity:** flags two or more people staying near each other for a sustained period.
* **Head orientation:** facing the camera vs. facing away — informational, not an alert.

### Optional AI Scene Description & Topic Classification (Ollama)
* Hands the filmstrip frames to a locally hosted **Ollama** vision model for a plain-language description.
* **Detection-aware prompt:** built dynamically around your configured YOLO classes and topics.
* **Topic classification:** your own categories with a 0–100 confidence score each — a sort/filter signal, not a calibrated probability.
* **Model picker** with tested presets plus a free-text option.
* **Live connectivity badge** in Settings.
* Written as both dashboard JSON and an **Immich-compatible XMP sidecar**.
* Manual **re-analyze button**, also available to an agent via the API.

### Audio Trigger (CLAP)
* Triggers purely from **sound**, independent of visual detection.
* [CLAP](https://github.com/LAION-AI/CLAP) compares live audio against **freely typed categories**, not a fixed class list.
* Runs in its own background thread per camera — can never block or delay recording.
* Live-editable categories, no restart needed.

### Speech Transcription (Whisper)
* [faster-whisper](https://github.com/SYSTRAN/faster-whisper), `tiny` through `large-v3`, GPU-accelerated with CPU fallback.
* Coordinated (not parallel) with description/topic analysis so they never clobber the same metadata file.

### Face Recognition (InsightFace)
* Detects and embeds faces on the same filmstrip frames the description stage already uses.
* **Model pack is your choice:** `buffalo_s/m/l` or `antelopev2`.
* Unmatched faces grouped via **DBSCAN clustering**.
* **Identity persists independently of any single video** — a named person's photo and embedding survive deleting every video that ever showed them.
* Dedicated **People** section: named people with a chosen representative photo, unnamed clusters you can name or merge.
* **Fully correctable:** reject a false detection, unassign a face, or merge specific faces from a mixed cluster into a different person.
* **Two levels of removing a person:** "un-name" (keeps data, re-clusterable) vs. "delete permanently."

### License Plate Recognition (optional, foundation)
* Vehicle detection (car/truck/bus/motorcycle) on the same filmstrip frames already used for faces, cropped and passed through EasyOCR.
* Multiple OCR text regions within one vehicle crop are combined rather than only trusting the single highest-confidence fragment — a plate is frequently split into more than one detected text region.
* Recognized plates matched against named vehicles by exact text, the same "named entity survives video deletion" model as faces — name a vehicle once, every future sighting of that plate auto-links to it.
* Still a foundation: currently reads any text in the vehicle's bounding box rather than localizing the plate region specifically, so brand lettering or livery text can occasionally be picked up alongside the actual plate. A tighter crop (lower portion of the vehicle box, where plates actually sit) is the planned next step.

### Semantic Search
* Searches descriptions, topics, transcripts, **and named people** — by exact text *and* by meaning.
* Powered by a local sentence-embedding model (`all-MiniLM-L6-v2`) in a lightweight SQLite index.
* Falls back to plain text search if the embedding model isn't installed.
* Same index also powers **Ask vigil**'s retrieval step.

### Daily & Weekly Summaries
* Natural-language recap of a day/week's events, generated from existing AI descriptions.
* Dashboard button or cronjob.
* Any summary can be regenerated or deleted directly from the dashboard.

### Home Assistant / MQTT (optional)
* Per-camera "Recording" motion sensor and "Last Event" description sensor to any MQTT broker.
* Home Assistant MQTT Discovery — entities appear automatically, no YAML.
* Fire-and-forget: publishing never delays or affects recording. See [HOME_ASSISTANT.md](./HOME_ASSISTANT.md).

### Anomaly Detection (optional)
* Flags recordings that are statistical outliers versus a camera's own recent history.
* Isolation Forest per camera, reusing the semantic-search embedding already computed for every event.
* Needs a baseline: at least 15 analyzed recordings per camera in the lookback window; cameras with less history are skipped, not force-trained.

### Storage Management
* Recordings folder is independently configurable from the app's own install location — point it at a larger or external drive without moving the whole project.
* Changing it only affects new recordings going forward; existing footage is left exactly where it is, deliberately not auto-migrated.
* A live **Storage Status** view shows free/used space and write access for every configured folder (recordings, export destination, watchfolder) — an early warning before a disk actually fills up, not an error message after.

### External API (Remote Control)
* Submit video for the same processing pipeline as a live recording, with per-job topics overriding the global setting.
* Job-based: instant job ID, poll for status or get a webhook callback (with retry) when done.
* Delivers the processed video, an arbitrary time-range clip (stream-copy, no re-encoding), and full enriched metadata.
* Own API-key auth, separate from the dashboard session. See [REMOTE_API.md](./REMOTE_API.md).

### Agent Control (optional, off by default)
* An AI agent can operate the pipeline directly through the same API — toggle cameras, tune settings, start/stop the pipeline, force-trigger or stop a specific recording, quick-record on demand, search, ask, read event/summary/system-status details, and retrain anomaly baselines.
* **Two-layer permission gate:** a master switch plus a per-capability toggle.
* **Settings changes are further restricted to a fixed allowlist enforced in code.**
* **Delete and export are intentionally not implemented** for agent use.
* **Proactive notifications:** configure a webhook URL and vigil pushes an event to it after every analyzed recording. See [AGENT_CONFIG.md](./AGENT_CONFIG.md).

### MCP Server
* Exposes the Agent Control surface as native MCP tools instead of raw HTTP calls — for Claude Desktop or any other MCP-compatible client.
* A thin wrapper, not a new permission surface: every tool calls the exact same gated API endpoint. See [MCP_SERVER.md](./MCP_SERVER.md).

### Export
* Bundles video, screenshot, all sidecar metadata, and the filmstrip folder into one named folder per event.
* Local path (direct copy) or remote `user@host:/path` (via `rsync`).
* Choose exactly what's included, remembered for next time.
* Optional delete-original-after-export, only ever after a confirmed successful export.

### Web Dashboard
* **Cameras and Recent Recordings are the whole main view** — Cameras, System Settings, Hardware Status, Storage, External API, and the Log live behind a single gear icon that opens as an overlay, instead of stacking a dozen collapsible sections on the page you actually look at day to day.
* **Ask vigil** front and center at the top — a real question in, a synthesized answer with linked sources out.
* Card-style thumbnails with duration, day-grouped lists, filterable by camera/person/topic.
* Lightbox built for fast review: previous/next navigation (arrow keys or on-screen arrows), remembered playback speed and autoplay preference across recordings, one-key delete with Enter-to-confirm, a visible highlight when a recording matched a configured topic, and automatic advance to the next recording after deleting one.
* CSRF-protected, non-blocking settings changes; `/health` endpoint + watchdog script for external monitoring.

### Utilities
* `backfill_thumbnails.py`, `backfill_filmstrips.py`, `backfill_search_index.py` — retroactively generate thumbnails/filmstrips/search entries for older recordings.
* `cluster_faces.py` — on-demand face grouping.
* None of these touch the live pipeline — safe to run anytime, safe to re-run.

</details>

<details>
<summary><strong>Model comparisons (YOLO, Face Recognition)</strong></summary>

### YOLO

| Model | Best for | Trade-offs |
| :--- | :--- | :--- |
| **YOLOv10** | Lean, predictable real-time detection | Lower peak accuracy than v12/26, but very consistent latency (NMS-free) |
| **YOLOv12** | Maximum accuracy, GPU headroom to spare | Higher VRAM/CPU cost |
| **YOLO26** | Best all-round default, especially on constrained hardware | Newest of the three; up to ~43% faster CPU inference than the previous generation |

**Rule of thumb:** YOLO26 by default. YOLOv10 for the simplest latency profile. YOLOv12 only with GPU headroom to spare.

### Face Recognition

| Model Pack | Best for | Trade-offs |
| :--- | :--- | :--- |
| **buffalo_s** | Fastest, lowest resource use | May miss more at odd angles or in poor light |
| **buffalo_m** | Balanced default | Middle ground on speed vs. accuracy |
| **buffalo_l** | Best accuracy of the buffalo packs | Larger, more compute per frame |
| **antelopev2** | Highest accuracy overall | Largest/slowest; vigil auto-fixes a known packaging quirk on first load |

</details>

<details>
<summary><strong>Tech stack</strong></summary>

* **Language:** Python 3.x
* **Inference:** PyTorch with CUDA
* **Computer Vision:** Ultralytics (YOLOv10/v12/26, pose models), OpenCV, PyAV
* **Video I/O:** PyAV/ffmpeg — NVDEC decode, packet-copy recording where possible, NVENC fallback otherwise
* **Web:** Flask
* **AI Analysis / Ask vigil:** [Ollama](https://ollama.com)
* **Audio Trigger:** [CLAP](https://github.com/LAION-AI/CLAP) via `transformers`
* **Transcription:** [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
* **Face Recognition:** [InsightFace](https://github.com/deepinsight/insightface) + `onnxruntime`, `scikit-learn` (DBSCAN + Isolation Forest)
* **License Plate OCR:** [EasyOCR](https://github.com/JaidedAI/EasyOCR)
* **Semantic Search:** `sentence-transformers` (`all-MiniLM-L6-v2`) + SQLite
* **Agent Integration:** MCP (`mcp` SDK), REST/webhooks, MQTT
* **Live platform ingestion:** `yt-dlp`
* **Export:** local copy or `rsync`
* **Process management:** threading, multiprocessing, subprocess

</details>

<details>
<summary><strong>Project structure</strong></summary>

* `web_ui.py`: Flask dashboard — routes, settings, camera management, search, Ask vigil, export, People API.
* `config.html`: Settings, Hardware Status, Storage, External API, and Log — a separate page opened via the dashboard's gear icon, keeping the main dashboard focused on cameras and recent activity.
* `recorder_pipeline.py`: Core detection/recording — one process per camera, GPU-aware startup, packet-copy recording, filmstrip capture, pose/behavior detection.
* `pose_fall_detection.py`: Fall, raised-hands, head-orientation, and pointing-gesture heuristics from pose keypoints.
* `loitering_detection.py`: Position-based loitering, movement-speed, and proximity detection.
* `watch_folder.py`: Folder-based import, including live-source detection and platform-URL bridging.
* `platform_bridge.py`, `mp4_probe.py`, `live_tail.py`: Support modules for treating a growing file or platform URL as a live camera source.
* `quick_record.py`: Ad-hoc, pipeline-independent fixed-duration recording.
* `daily_summary.py`: Daily/weekly narrative summaries; shares its Ollama-calling logic with Ask vigil.
* `mqtt_client.py`: MQTT / Home Assistant integration.
* `agent_webhook.py`: Proactive event notifications to an agent, fire-and-forget.
* `anomaly_detection.py`: Per-camera anomaly detection.
* `mam_api.py`: External API — job submission, status, webhooks, plus gated agent-control routes.
* `mcp_server.py`: MCP server exposing the agent-control API as native tools.
* `agent_permissions.py`, `agent_config.json`: Agent Control permission gate.
* `postprocess.py`: Sequences description/topics, transcription, face recognition, and license plate recognition for a finished recording.
* `ai_analyze.py`: Ollama scene analysis and topic classification; writes dashboard metadata + Immich XMP.
* `audio_trigger.py`: CLAP-based audio trigger.
* `transcribe_audio.py`: Whisper transcription.
* `face_recognize.py`, `faces_db.py`, `cluster_faces.py`: Face detection, permanent identity storage, and clustering.
* `plate_recognize.py`, `plates_db.py`: Vehicle detection, license plate OCR, and permanent vehicle identity storage.
* `search_index.py`: SQLite-backed full-text + semantic search index — also the retrieval layer behind Ask vigil.
* `backfill_thumbnails.py`, `backfill_filmstrips.py`, `backfill_search_index.py`: Retroactive utilities.
* `helpers.py`, `config.py`: Shared utilities and system-wide configuration.
* `templates/dashboard.html`, `templates/config.html`, `static/`: Dashboard UI, dark and light themes.
* `start_detached.sh`, `stop.sh`, `watchdog.sh`: Pipeline lifecycle scripts.
* `Dockerfile`, `docker-compose.yml`: Containerized setup.
* `alerts/`: Recorded events + metadata, with an `archive/` subfolder, `.people_photos/` and `.vehicle_photos/` permanent identity archives. Location is independently configurable — see Storage Management.
* `logs/`: Application and system logs.
* `search_index.db`, `faces.db`, `plates.db`, `streams.json`, `pipeline_settings.json`, `stream_overrides.json`: Local, gitignored runtime data.

</details>

---

## Hardware Requirements

* **GPU:** NVIDIA, CUDA 12.8+ recommended (RTX 20-series through 50-series) — auto-detects capability and degrades gracefully.
* **OS:** Linux (Ubuntu recommended).
* **Memory:** 8GB+ RAM.
* **Optional:** [Ollama](https://ollama.com) with a vision model for AI descriptions and Ask vigil — `llava` is the most broadly reliable choice.

**Tested configurations:**

| System | Specs | Load | Status |
| :--- | :--- | :--- | :--- |
| Intel NUC 11 Enthusiast | 32 GB RAM, RTX 2060 (6 GB) | 4 streams, 30 FPS FullHD | Stable |
| High-End Workstation | Core Ultra 9 285K, 64 GB RAM, RTX 5090 (32 GB) | 8 streams, 30 FPS FullHD | Stable |

---

## Acknowledgements

Built on [YOLOv10](https://github.com/THU-MIG/yolov10), [YOLOv12](https://github.com/sunsmarterjie/yolov12), YOLO26, and [Ultralytics](https://github.com/ultralytics/ultralytics). Optional components: [Ollama](https://ollama.com), [CLAP](https://github.com/LAION-AI/CLAP), [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [InsightFace](https://github.com/deepinsight/insightface), [EasyOCR](https://github.com/JaidedAI/EasyOCR), [sentence-transformers](https://www.sbert.net/), the [MCP](https://modelcontextprotocol.io/) SDK, [yt-dlp](https://github.com/yt-dlp/yt-dlp).

Parts of this code were written with AI (Google Gemini, Claude, Claude Code, and local models). The architecture and the work of making it all run reliably was a person's job.

<details>
<summary>Citation</summary>

```bibtex
@article{wang2024yolov10,
  title={YOLOv10: Real-Time End-to-End Object Detection},
  author={Wang, Ao and Chen, Hui and Liu, Lihao and Chen, Kai and Lin, Zijia and Han, Jungong and Ding, Guiguang},
  journal={arXiv preprint arXiv:2405.14458},
  year={2024}
}
```
</details>

## Disclaimer

Intended for educational and private security purposes. You're responsible for ensuring your use of surveillance technology — especially AI description, transcription, face recognition, and license plate recognition — complies with local privacy and data protection law. Face recognition, license plate recognition, and transcription carry meaningfully higher privacy stakes than object detection alone.
