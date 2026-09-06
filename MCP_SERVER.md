# MCP Server

Exposes vaelen's [Agent Control API](./AGENT_CONFIG.md) as proper MCP tools, for any MCP-compatible client (Claude Desktop, Hermes/OpenClaw if it speaks MCP, etc.) instead of raw HTTP calls.

**This is a thin wrapper, not a new permission surface.** Every tool here calls the exact same `/api/v1/agent/*` endpoints documented in AGENT_CONFIG.md, with the exact same gate (`agent_control_enabled` + per-capability toggles) enforced server-side. Running this server doesn't bypass any of that — it's just a different transport for calling the same, already-gated API.

## Setup

```bash
pip install "mcp[cli]" requests
```

1. Generate an API key from the dashboard's External API card.
2. Set two environment variables before running:
   ```bash
   export VAELEN_BASE_URL="http://localhost:19473/api/v1"
   export VAELEN_API_KEY="idg_xxxxxxxxxxxx"
   ```
3. Run it:
   ```bash
   python3 mcp_server.py
   ```
   Uses stdio transport by default — the standard way most MCP clients (including Claude Desktop) connect to a locally-running server.

For Claude Desktop, add to its MCP server config:
```json
{
  "mcpServers": {
    "vaelen": {
      "command": "python3",
      "args": ["/path/to/vaelen/mcp_server.py"],
      "env": {
        "VAELEN_BASE_URL": "http://localhost:19473/api/v1",
        "VAELEN_API_KEY": "idg_xxxxxxxxxxxx"
      }
    }
  }
}
```

## Tools

All 21 correspond directly to an Agent Control endpoint — see [AGENT_CONFIG.md](./AGENT_CONFIG.md) for what each one is gated by and what it actually does:

`get_capabilities`, `list_cameras`, `enable_camera`, `disable_camera`, `trigger_recording`, `stop_recording`, `quick_record`, `quick_record_status`, `set_notify_only`, `get_detections`, `get_settings`, `update_settings`, `pipeline_status`, `pipeline_start`, `pipeline_stop`, `search`, `get_event`, `get_summaries`, `get_system_status`, `reanalyze`, `train_anomaly_baselines`.

Each tool's docstring (visible to the calling model) explains what it does and, where relevant, which other tool to use instead for a similar-sounding task (e.g. `quick_record` vs `trigger_recording`, `stop_recording` vs `disable_camera`) — the same distinctions covered in the [Quick reference](./AGENT_CONFIG.md#quick-reference--the-right-tool-for-the-job).

## If a tool call fails

A failure here means the same thing it would over raw HTTP: check `get_capabilities` first to confirm the relevant permission is actually enabled, and confirm `VAELEN_BASE_URL`/`VAELEN_API_KEY` are correct and the vaelen dashboard process is running the current code (a route that 404s almost always means the server process needs restarting, not that this wrapper has the wrong path).
