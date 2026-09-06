# Installation Guide: vaelen

This document describes the process of installing `vaelen` on a new Linux system (optimized for NVIDIA GPU setups).

> **Looking for the fastest path instead?** See [DOCKER.md](./DOCKER.md) for a Docker-based install — no venv, no manual Python/CUDA setup, just Docker + the NVIDIA Container Toolkit. The steps below are for a bare-metal install (venv, `init_venv.sh`, system packages) — pick one or the other, not both.

## 1. Prerequisites

Before proceeding with the Python installation, your system must provide the necessary hardware foundations:

*   **NVIDIA Driver:** Ensure a modern NVIDIA driver is installed. For current-generation GPUs (RTX 40xx/50xx, e.g. Blackwell/RTX 5090) you need a driver new enough to support **CUDA 12.8+** — check the "CUDA Version" shown at the top right of `nvidia-smi`.
    *   Verify with: `nvidia-smi`
*   **CUDA Toolkit:** The CUDA toolkit should be available and compatible with your driver (recommended: `12.8` or newer).
*   **ffmpeg:** Required as a system binary — not just the Python bindings — for on-the-fly video transcoding when you play back a recording in the dashboard (the recording pipeline itself uses PyAV directly and does not need this, but the web UI's playback route does).
*   **System Packages:** You need Python 3, the venv module, and ffmpeg.
    ```bash
    sudo apt update
    sudo apt install python3 python3-pip python3-venv git ffmpeg -y
    ```

## 2. Clone the Repository

Clone the repository onto your target machine:
```bash
git clone https://github.com/axeljerabek/vaelen
cd vaelen
```

## 3. Setup Virtual Environment (Python venv)

To keep your system clean, we use an isolated virtual environment (`.venv`). This prevents conflicts with other Python packages on your machine.

> **Shortcut:** `init_venv.sh` does steps 3 and 4 below in one go (creates the venv fresh and installs everything from `requirements.txt`). Run `bash init_venv.sh` and skip ahead to [Section 5](#5-configuration-crucial) if you'd rather not do this by hand.

1.  **Create the venv:**
    ```bash 
    python3 -m venv .venv
    ```
2.  **Activate the environment:**
    ```bash
    source .venv/bin/activate
    ```
    *(After activation, you should see `(.venv)` prepended to your terminal prompt.)*

## 4. Install Dependencies

This installs the AI models (Ultralytics/YOLO) and the web components. `requirements.txt` already pins PyTorch's index URL to the CUDA 12.8 wheels at the top of the file, so a plain install pulls in a CUDA-capable build directly — no separate PyTorch step needed:

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

The YOLO model file (`.pt`) is downloaded automatically on first start, based on the `YOLO_VERSION`/`MODEL_SIZE` selected in `config.py` (or later, live, in the dashboard Settings).

### Optional: Ollama (AI scene descriptions)

If you want the optional "describe what happened in this recording" feature, you'll additionally need a running [Ollama](https://ollama.com) instance (commonly run in its own Docker container) with a vision-capable model pulled, e.g.:
```bash
docker exec -it <container-name> ollama pull llava:latest
```
This is entirely optional — vaelen records and detects normally with no Ollama installed at all. Everything related to this (enable/disable, endpoint URL, which model) is configured later, live, in the dashboard under Settings → KI-Videoanalyse.

### Optional: Audio Trigger and Semantic Search (no extra install step)

The audio trigger (CLAP) and semantic search (`sentence-transformers`) packages are already included in `requirements.txt`, so nothing extra to install here. Their actual models (a few hundred MB each) download automatically from Hugging Face the first time you enable and use each feature — not during this install step. Both are off unless you turn them on in Settings.

### Optional: Face Recognition and Speech Transcription — a real gotcha with `onnxruntime`

`insightface` (face recognition) and `faster-whisper` both declare a dependency on plain `onnxruntime` (CPU-only) — but `requirements.txt` also installs `onnxruntime-gpu` for actual GPU acceleration. Both packages install into the **same** `onnxruntime` Python module, so whichever one physically writes its files last silently wins — with no error, no warning, just quiet CPU-only inference from then on regardless of which one `pip show` claims is "installed." This is a known packaging quirk of `onnxruntime`/`onnxruntime-gpu`, not specific to this project.

**If you used `init_venv.sh`, this is already handled** — it runs the fix-up below automatically after installing and prints the resulting providers so you can see at a glance whether GPU support made it through. Docker builds do the same, as a build-time warning (not a hard failure — GPU acceleration for this one optional feature isn't worth blocking the whole image over).

**If you installed manually** (or just want to double-check), verify:
```bash
python3 -c "import onnxruntime; print(onnxruntime.get_available_providers())"
```
If `CUDAExecutionProvider` is missing from the list (only `CPUExecutionProvider`/`AzureExecutionProvider` show up), fix it with:
```bash
pip uninstall -y onnxruntime onnxruntime-gpu
pip install --force-reinstall --no-deps onnxruntime-gpu
```
The `--no-deps` matters — without it, reinstalling can immediately pull the CPU package back in via insightface/faster-whisper's own dependency chain and overwrite the GPU one again. Worth re-running this check after any future `pip install -r requirements.txt` outside of `init_venv.sh` (e.g. a manual upgrade), since the collision can silently reoccur depending on the order pip happens to install things in — that order isn't guaranteed to match `requirements.txt`'s line order.

## 5. Configuration (Crucial!)

Before starting, copy the example config file:
```bash
cp config.py.example config.py
```
`config.py` itself now needs essentially no editing for a normal setup — it only holds infrastructure paths and, if you want non-default starting values, `YOLO_VERSION`/`MODEL_SIZE`. Everything you'd expect to configure by hand — the **camera list** (add/edit/remove, name + RTMP/RTSP URL), FPS, detection thresholds, thumbnails, retention, theme, AI analysis, and more — is set up **live in the dashboard** after first start (Settings → Cameras is where you add your cameras; see `manual.md` for the full reference of what lives where).

Just make sure the project directory permissions allow writing to `alerts/` and `logs/` before starting.

## 6. Starting the System

vaelen is two separate processes: the **recording pipeline** (`recorder_pipeline.py`, one worker per camera) and the **web dashboard** (`web_ui.py`). The dashboard's Start/Stop button controls the pipeline process; the dashboard itself needs to be started separately.

**Start the web dashboard:**
```bash
python3 web_ui.py
```
The dashboard will be accessible at `http://0.0.0.0:19473`. From there, use the Start button in the pipeline control bar to launch the recording pipeline — or start it directly:
```bash
./start_detached.sh
```

**Running in Background (Optional):**
Use the provided shell scripts (`start_detached.sh` / `stop.sh` for the pipeline, your own wrapper such as `start_web_ui.sh` for the dashboard) to manage both processes cleanly and run them in the background. For unattended/production setups, consider wrapping both in systemd services, and optionally pairing `watchdog.sh` with a cron job to auto-restart the dashboard if it ever becomes unresponsive (see `manual.md`).

---

*⚠️ **Note:** If you are using a very new GPU (e.g., RTX 5090), double-check that PyTorch was actually installed with CUDA support (step 4 above) rather than a CPU-only build. To verify, run:*
```bash
python3 -c "import torch; print('CUDA available:', torch.cuda.is_available()); print('Device:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none')"
```
