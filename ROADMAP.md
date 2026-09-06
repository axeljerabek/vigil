# vaelen — Roadmap

Ongoing, prioritized list. Worked through incrementally, not all at once.

---

## Low priority / opportunistic

- [ ] *(deferred)* **VAE/autoencoder on raw frames** for anomaly detection — only worth pursuing if the Isolation Forest approach hits real limits. Would be the first custom-trained model in the system, needs a GPU training pipeline, meaningfully more effort.

## Open from earlier sessions

- [ ] Memory-usage safeguard for encode-mode cameras (MJPEG/USB) — ~1.7GB/camera at 1080p/10s pre-roll; flagged, no decision made yet
- [ ] MJPEG/USB camera encoding path — built and unit-tested, not yet verified against real hardware
- [ ] Backup feature (storage management priorities 1-3 are done; backup remains)
- [ ] License plate recognition: currently reads any text in the vehicle's bounding box rather than localizing the plate region specifically — a tighter crop (lower portion of the vehicle box) is the planned next step
- [ ] Cross-recording/cross-camera understanding ("Ask vaelen" reasoning across a whole day, linking a named face and a named vehicle into one tracked entity, routine-aware anomaly detection) — the building blocks (named people, named vehicles, semantic search, an LLM already in the loop) exist; the layer that stitches them together does not yet

---

## Done

**Rename:** IDguard PRO → vaelen → Vaelen. The second rename (this session) was necessary — "vaelen" turned out to collide with an existing commercial NVR product (3xLOGIC VAELEN) and several other open-source/commercial projects across security, broadcast-media (Telestream Vantage/MAM), and AI-tooling spaces. Renamed across the codebase, GUI, docs, Docker setup, and architecture diagram; GitHub repo renamed; systemd service renamed; `.gitignore` cleaned up in the process (runtime data — anomaly models, live HLS segments, databases — had been accidentally committed).

**Watchfolder live mode:** a growing file (or a YouTube/Twitch/Vimeo URL) dropped into the watchfolder is probed for streamability and, if recognized (MPEG-TS, or MP4 with a fast-start moov atom), connected immediately as a live camera source running the full detection pipeline — instead of waiting for the file to finish. A persistent per-file marker prevents endless reprocessing of a source left in place after import. Platform sources bridge through `yt-dlp`.

**Ask vaelen:** a prominent, Google/Gemini-style natural-language input on the dashboard. Runs the question through the existing semantic search index, hands the matched recordings to a local Ollama LLM with an explicit instruction to answer only from what's actually there, and shows the answer with the source recordings linked underneath.

**License plate recognition (foundation):** vehicle detection (car/truck/bus/motorcycle) on the same filmstrip frames already used for faces, EasyOCR on the crop, multiple OCR text regions combined (a plate is frequently split into more than one detected region). Named vehicles persist and auto-match on future sightings, same model as faces.

**Storage management:** recordings folder independently configurable from the app's install location (`ALERTS_DIR_OVERRIDE`), with safe fallback to the default on any problem with the override path. Live Storage Status view — free/used space and write access per configured folder.

**Dashboard restructure:** Cameras, System Settings, Hardware Status, Storage, External API, and Log moved out of the main dashboard into a separate page (`config.html`), opened via a gear icon in a lightbox iframe — cut `dashboard.html` from ~3600 to ~2400 lines. Two real cross-page dependencies found and fixed in the process: the old combined status-poll function was mixing hardware stats with dashboard-only concerns (recent/archived lists, REC badges, tab title), and the camera-list-save handler needed `postMessage` to tell the parent page to refresh its live grid, since the config page runs in a separate iframe document.

**Lightbox review speed:** previous/next navigation (arrow keys or on-screen arrows) between recordings, auto-advance to the next recording after deleting one, playback speed (up to 16x) and autoplay preference remembered across recordings, one-key delete with Enter-to-confirm, and a visible highlight when a recording matched a configured topic.

**Visual redesign:** cyan-accented "nebula" glassmorphism theme (ambient background glow, translucent cards, Plus Jakarta Sans), replacing the original amber theme; new logo icon.

**Agent Control:** camera toggle, settings (allowlisted), pipeline start/stop, search, manual trigger/stop, quick-record (pipeline-independent ad-hoc recording), event details, summaries, system status, reanalyze, anomaly training. Per-capability permission config, master switch, off by default. Proactive notifications (agent webhook) after each analyzed event, with an anomaly-only filter. Delete and export intentionally excluded. Fixed a real bug where enabling/disabling a camera through the API never actually started/stopped its process — the master pipeline now reconciles against `streams.json` periodically instead of only reading it at startup.

**MCP Server:** 21 tools wrapping the full Agent Control API for Claude Desktop and other MCP clients, tested against the real MCP protocol (not just the underlying functions). `MCP_SERVER.md`.

**Pose Estimation & Behavior Detection** (turned out to be much more than just fall detection): fall detection (torso-angle heuristic, temporal confirmation), raised-hands distress signal, loitering, fast-movement/running detection, sustained close-proximity detection, and head-orientation/gaze logging — six independent, individually-toggleable signals from one shared pose model at zero extra GPU cost. Fall/distress/movement/proximity all force a recording; gaze and pointing are informational only.

**Face recognition — critical persistence fix:** named people were silently losing their recognition embedding once every video that ever showed them was deleted (the centroid got wiped to `NULL`). Fixed by archiving a person's face photo permanently the moment they're identified, and by no longer deleting a named person's face rows/embeddings when their source video is removed — only truly unassigned/cluster faces get cleaned up. Also added: user-chosen profile photo per person, a genuine "delete permanently" action (distinct from the existing soft "un-name"), and the ability to name/merge a *selected subset* of faces within a mixed cluster instead of always the whole group.

**Docs overhaul:** README rewritten multiple times (structure, then leading with a hook and differentiators, then again around the "understanding over video" positioning with Ask vaelen as the centerpiece); `MANUAL.md` fully brought current (pose/behavior, anomaly detection, MQTT, agent webhook, export, watchfolder, MCP — grew from 9 to 20 sections); `AGENT_CONFIG.md` gained a quick-reference "right tool for the job" table after a real mix-up between `trigger`/`quick_record`/`stop`/`disable`; `config.py.example` fixed (stale "IDENTITY-GUARD PRO" header, a duplicated `BROWSER_COMPATIBLE_VIDEO_CODECS` line, a German leftover in `COCO_CLASS_NAMES`); a full technical blog post written.

**Earlier sessions:** RTMP+RTSP, MJPEG/USB encode path, Watchfolder import (mode 2), HEVC re-transcode (GPU-accelerated), process orchestration fixes (restart detection, systemd service, worker naming), post-process watchdog (a stuck Ollama/GPU no longer blocks the whole queue), per-video notes (XMP export), shared export subfolders, export content checkboxes (video/metadata/thumbs), delete-after-export option, star rating (xmp:Rating), daily/weekly LLM summaries (dashboard card + cron example, plus regenerate/delete), Home Assistant/MQTT integration (HA discovery, `HOME_ASSISTANT.md`), Isolation Forest anomaly detection (dashboard card + cron example), README logo, External API for remote control (job submission, API-key auth, status polling, webhook callbacks with retry, video/segment export, `REMOTE_API.md`), UI style cleanup (reduced padding/radius, more screen space for media).
