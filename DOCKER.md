# Running vaelen with Docker

This is the fastest way to get vaelen running, especially if you don't want to manage a Python venv, CUDA wheels, and system packages by hand. One container, built from the project's image, running the dashboard — which starts/stops the recording pipeline itself, exactly like on a bare-metal install.

**Honest caveat up front:** this setup hasn't been verified against real GPU hardware by me (the assistant that wrote it) — I don't have a GPU or a Docker daemon available while writing this. The YAML structure and logic below have been checked carefully, but please treat the very first run as a real test, not a done deal, and report back anything that doesn't match reality. (This is also why the container split below got fixed after an actual real-world test caught it — worth keeping in mind for anything else here too.)

## Why one container, not two

An earlier version of this setup split the recording pipeline and the dashboard into two separate containers. That was wrong, and broke two things if you tried it: the pipeline started immediately and couldn't be stopped from the dashboard, and it never picked up cameras added later through the dashboard's Settings → Cameras page.

The reason: `web_ui.py` starts and stops `recorder_pipeline.py` by running `start_detached.sh`/`stop.sh` as its own **child process** — not over the network, not via any API. That only works if both live in the same process namespace. Split across two containers, the dashboard's container literally cannot see or signal the pipeline process running in the other one — the Start/Stop button has nothing to act on, and the pipeline (started as that container's own main process, with no camera config yet) just runs uncontrolled from the moment the container starts. One container, matching the original single-machine design, avoids all of this.

## Prerequisites

* **Docker** with the modern `docker compose` (V2) plugin — check with `docker compose version`. If you only have the old standalone `docker-compose` (hyphenated, V1), GPU passthrough below may not work reliably; either upgrade, or switch to the `runtime: nvidia` fallback commented in `docker-compose.yml`.
* **[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)** installed on the host — this is what actually lets a container see the GPU at all. Verify it works before touching this project at all:
  ```bash
  docker run --rm --gpus all nvidia/cuda:12.8.1-runtime-ubuntu22.04 nvidia-smi
  ```
  If that doesn't show your GPU, nothing below will work either — fix that first.
* A locally hosted [Ollama](https://ollama.com) instance if you want AI scene descriptions — this project's Docker setup does **not** include Ollama; point `OLLAMA_URL` in the dashboard Settings at wherever yours runs.

## Setup

1. **Clone the repo:**
   ```bash
   git clone https://github.com/axeljerabek/vaelen
   cd vaelen
   ```

2. **Create your config:**
   ```bash
   cp config.py.example config.py
   ```
   No camera setup needed here anymore — cameras are added later, live, in the dashboard under Settings → Cameras. `config.py` itself only needs editing if you want non-default `YOLO_VERSION`/`MODEL_SIZE` starting values.

3. **Pre-create the files Docker needs to bind-mount as files, not folders.** This is the single most important step and the easiest one to skip. If a bind-mounted host path doesn't exist yet, Docker creates a **directory** there instead of a file — and the app will fail to open it (or, for the YOLO model file specifically, silently skip its own auto-download, since `config.py`'s download check now treats a real empty file correctly, but a whole *directory* where a file was expected is a different, harder failure).
   ```bash
   touch pipeline_settings.json stream_overrides.json search_index.db faces.db streams.json
   touch yolo26x.pt   # match this to whatever YOLO_VERSION/MODEL_SIZE you set in config.py
   mkdir -p alerts logs
   ```
   The YOLO model file can stay empty (0 bytes) — the pipeline will detect that and download it properly on first start. `pipeline_settings.json`/`stream_overrides.json`/`streams.json` will be filled in the first time you save Settings/Cameras in the dashboard (an empty `streams.json` just means no cameras configured yet — add your first one in Settings → Cameras); `search_index.db`/`faces.db` are filled in automatically once search indexing / face recognition run.

4. **Build and start:**
   ```bash
   docker compose up -d --build
   ```
   First start downloads the base CUDA image, installs everything, and then downloads the YOLO model from inside the container — this can take a while depending on your connection. Watch progress with:
   ```bash
   docker compose logs -f vaelen
   ```

5. **Open the dashboard:** `http://<host-ip>:19473`, add your cameras under Settings → Cameras, then use the Start button in the pipeline control bar — same as the bare-metal install, since this is now genuinely the same single-process-tree design, just containerized.

## Notes specific to this setup

* **`network_mode: host`** — cameras are typically local RTMP streams on your home network, and host networking avoids extra port-mapping/firewall fiddling. This does mean the container shares the host's network namespace directly (fine for a home server, worth knowing if you're hardening a shared machine).
* **NVENC/NVDEC inside the container** (hardware video encode/decode, separate from CUDA compute) depends on whether the `ffmpeg` build inside the image actually has NVENC support compiled in, which varies by Ubuntu version — this is not guaranteed by this Dockerfile. If it doesn't work, the pipeline already has an automatic, self-healing fallback to software encode/decode built in (from long before this Docker setup existed) — you'd just see somewhat higher CPU use per stream than the bare-metal install, not a broken pipeline.
* **Face recognition may silently run on CPU inside the container**, for either of two distinct reasons — worth telling apart, since they need different fixes:
  1. `insightface`/`faster-whisper` pull in plain `onnxruntime` as a dependency, which can overwrite `onnxruntime-gpu`'s files in the same Python module namespace with no error at all. See INSTALL.md's Face Recognition section for the fix.
  2. `onnxruntime-gpu` needs cuDNN as a **system library** (`libcudnn.so.9`) — unlike PyTorch, which bundles its own cuDNN inside the pip wheel itself. The base CUDA image doesn't include this, so it's installed explicitly in the Dockerfile (`cudnn9-cuda-12`) — this exact step is **unverified against real hardware**; if the build fails on it or pulls a surprising sub-version, please report back.

  Check which one (if either) is happening: `docker compose exec vaelen python3 -c "import onnxruntime; print(onnxruntime.get_available_providers())"` — if `CUDAExecutionProvider` is missing, check the container's build log for the cuDNN install step, and separately try the fix from INSTALL.md for the namespace-collision case.
* **Model/embedding caches** (CLAP, sentence-transformers, Whisper, InsightFace) are kept in named Docker volumes (`model-cache`, `ultralytics-cache`), not bind-mounted — so they survive container restarts and rebuilds without you needing to manage the exact cache folder structure on the host.
* **File ownership:** the container runs as root by default (common for GPU workloads, since `/dev/nvidia*` access typically needs it), so files written into `alerts/` and `logs/` on the host will be root-owned. If that's a problem for you, you'll need to add your own `PUID`/`PGID` handling — not included here, to keep the initial setup simple.
* **Updating:** `git pull`, then `docker compose up -d --build` again. Your `alerts/`, `logs/`, settings files, and model caches are untouched — everything that matters is either bind-mounted or in a named volume, never baked into the image layer.

## Uninstalling / starting fresh

```bash
docker compose down          # stop and remove the container, keep volumes and bind-mounted data
docker compose down -v       # also remove the named volumes (model caches — re-downloads next time)
```
Your `alerts/`, `logs/`, and config files are on the host filesystem regardless — neither command touches those.
