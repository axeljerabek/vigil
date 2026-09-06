"""
mcp_server.py — MCP server wrapper around vaelen's Agent Control API.

Lets any MCP-compatible client (Claude, Hermes/OpenClaw if it speaks MCP,
etc.) operate vaelen through proper MCP tools instead of raw HTTP calls.
This is a thin wrapper -- every tool here just calls the same
/api/v1/agent/* endpoints documented in AGENT_CONFIG.md, with the same
permission gate (agent_control_enabled + per-capability toggles) enforced
server-side exactly as before. Running this server does NOT bypass any of
that -- it's a different way to call the same, already-gated API.

Configuration via environment variables:
  VAELEN_BASE_URL  -- e.g. http://localhost:19473/api/v1 (default shown)
  VAELEN_API_KEY   -- generate one from the dashboard's External API card

Run with: python3 mcp_server.py
(stdio transport by default -- the standard way most MCP clients,
including Claude Desktop, connect to a local server)
"""
import os
import requests
from mcp.server.mcpserver import MCPServer

VAELEN_BASE_URL = os.environ.get("VAELEN_BASE_URL", "http://localhost:19473/api/v1").rstrip("/")
VAELEN_API_KEY = os.environ.get("VAELEN_API_KEY", "")
TIMEOUT = 20

mcp = MCPServer("vaelen")


def _headers():
    return {"Authorization": f"Bearer {VAELEN_API_KEY}"}


def _get(path, params=None):
    try:
        r = requests.get(f"{VAELEN_BASE_URL}{path}", headers=_headers(), params=params, timeout=TIMEOUT)
        return r.json()
    except requests.RequestException as e:
        return {"error": f"Could not reach vaelen at {VAELEN_BASE_URL}: {e}"}


def _post(path, data=None):
    try:
        r = requests.post(f"{VAELEN_BASE_URL}{path}", headers=_headers(), data=data, timeout=TIMEOUT)
        return r.json()
    except requests.RequestException as e:
        return {"error": f"Could not reach vaelen at {VAELEN_BASE_URL}: {e}"}


@mcp.tool()
def get_capabilities() -> dict:
    """Check what this agent is currently allowed to do on vaelen -- call this
    first if unsure. Works even if agent control is fully disabled."""
    return _get("/agent/capabilities")


@mcp.tool()
def list_cameras() -> dict:
    """List all cameras with their enabled/audio status. Never includes URLs
    or credentials."""
    return _get("/agent/cameras")


@mcp.tool()
def enable_camera(name: str, audio_enabled: bool | None = None) -> dict:
    """Enable a camera for future recording. Takes up to ~15s to actually
    start the process, not instant. Optionally also set its audio state in
    the same call."""
    data = {}
    if audio_enabled is not None:
        data["audio_enabled"] = audio_enabled
    return _post(f"/agent/cameras/{name}/enable", data=data)


@mcp.tool()
def disable_camera(name: str, audio_enabled: bool | None = None) -> dict:
    """Disable a camera for future recording. Does NOT stop an already-active
    recording -- use stop_recording for that."""
    data = {}
    if audio_enabled is not None:
        data["audio_enabled"] = audio_enabled
    return _post(f"/agent/cameras/{name}/disable", data=data)


@mcp.tool()
def trigger_recording(name: str) -> dict:
    """Start an event-style recording on a camera whose process is already
    running (see list_cameras). Only works if the pipeline is live -- use
    quick_record instead if the pipeline might be stopped."""
    return _post(f"/agent/cameras/{name}/trigger")


@mcp.tool()
def stop_recording(name: str) -> dict:
    """End a recording that's currently in progress, right now. This is the
    correct way to stop an active recording -- disable_camera does not."""
    return _post(f"/agent/cameras/{name}/stop")


@mcp.tool()
def quick_record(name: str, duration_seconds: int = 30) -> dict:
    """Record a fixed-duration clip from a camera, independent of whether the
    main pipeline is even running. Returns a job_id immediately -- use
    quick_record_status to check on it. This is the right tool for 'just
    record N seconds of this camera right now'."""
    return _post(f"/agent/cameras/{name}/quick_record", data={"duration": duration_seconds})


@mcp.tool()
def quick_record_status(job_id: str) -> dict:
    """Check on a quick_record job: recording -> done/failed, with the file
    path and AI-analysis outcome once finished."""
    return _get(f"/agent/quick_record/{job_id}")


@mcp.tool()
def set_notify_only(name: str, notify_only: bool) -> dict:
    """Switch a camera between normal (auto-record on detection) and
    notify-only (detection still runs, but only reports -- recording waits
    for an explicit trigger_recording call). Takes effect on the camera's
    next process restart, not instantly."""
    action = "enable" if notify_only else "disable"
    return _post(f"/agent/cameras/{name}/notify_only/{action}")


@mcp.tool()
def get_detections() -> dict:
    """Most recently reported detections per camera -- the way to see what a
    notify-only camera has spotted without it having recorded anything."""
    return _get("/agent/detections")


@mcp.tool()
def get_settings() -> dict:
    """Read the currently changeable tuning settings (confidence threshold,
    FPS, AI topics, etc). Only returns allowlisted keys -- never credentials
    or paths."""
    return _get("/agent/settings")


@mcp.tool()
def update_settings(**kwargs) -> dict:
    """Change tuning settings. Only a fixed allowlist of keys can be changed
    this way (see get_settings for what's currently set) -- credentials,
    camera URLs, and export destinations are never reachable through this,
    regardless of what's passed. A request mixing an allowed and disallowed
    key is rejected entirely."""
    return _post("/agent/settings", data=kwargs)  # sent as form data; server also accepts JSON


@mcp.tool()
def pipeline_status() -> dict:
    """Check whether the recording pipeline is currently running."""
    return _get("/agent/pipeline/status")


@mcp.tool()
def pipeline_start() -> dict:
    """Start the recording pipeline. While stopped, no camera records."""
    return _post("/agent/pipeline/start")


@mcp.tool()
def pipeline_stop() -> dict:
    """Stop the recording pipeline entirely."""
    return _post("/agent/pipeline/stop")


@mcp.tool()
def search(query: str) -> dict:
    """Search recordings by description, topic, transcript, or person name.
    Read-only, returns up to 50 results sorted by relevance."""
    return _get("/agent/search", params={"q": query})


@mcp.tool()
def get_event(filename: str) -> dict:
    """Full metadata for one specific recording -- description, topics,
    transcript, faces, anomaly status. Checks both active and archived
    recordings. This is the right tool for 'what does this recording say',
    not a filesystem search."""
    return _get(f"/agent/events/{filename}")


@mcp.tool()
def get_summaries() -> dict:
    """The daily/weekly narrative summaries, same as shown on the dashboard
    -- good for 'what happened today' without searching individual events."""
    return _get("/agent/summaries")


@mcp.tool()
def get_system_status() -> dict:
    """Hardware status: CPU/RAM/VRAM usage, GPU temperature."""
    return _get("/agent/system_status")


@mcp.tool()
def reanalyze(filename: str) -> dict:
    """Re-run the AI analysis (description, topics, faces, transcript) on an
    existing recording. Runs in the background."""
    return _post(f"/agent/reanalyze/{filename}")


@mcp.tool()
def train_anomaly_baselines(lookback_days: int = 30) -> dict:
    """Retrain the per-camera anomaly-detection baselines. Cameras with fewer
    than 15 recordings in the lookback window are skipped, not force-trained."""
    return _post("/agent/anomaly/train", data={"lookback_days": lookback_days})


if __name__ == "__main__":
    if not VAELEN_API_KEY:
        print("⚠️  VAELEN_API_KEY is not set -- every tool call will fail with 401. "
              "Generate a key from the dashboard's External API card and set the "
              "VAELEN_API_KEY environment variable before running this server.")
    mcp.run(transport="stdio")
