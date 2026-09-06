# vaelen - Container-Image
#
# Basis: nvidia/cuda "runtime" (nicht "devel") — wir kompilieren nichts,
# PyTorch bringt seine eigenen CUDA-Bibliotheken über den cu128-Wheel mit.
# Die tatsächliche GPU-Nutzung passiert über das NVIDIA Container Toolkit
# auf dem Host, nicht durch irgendwas, das hier im Image gebaut wird.
FROM nvidia/cuda:12.8.1-runtime-ubuntu22.04

# ffmpeg: wird NUR von web_ui.py für die Video-Wiedergabe-Transcodierung im
# Dashboard gebraucht (subprocess-Aufruf) — die Aufnahme-Pipeline selbst
# nutzt PyAV direkt, braucht das ffmpeg-Binary nicht.
#
# Hinweis: ob das ffmpeg aus den Ubuntu-Paketquellen NVENC/NVDEC-Unterstützung
# mitbringt, hängt von der Ubuntu-Version ab und ist hier nicht garantiert.
# Kein Problem — die Pipeline fällt dann automatisch auf Software-Encoding
# zurück (das war schon vorher eingebaut), nur mit etwas mehr CPU-Last.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv ffmpeg git \
    && rm -rf /var/lib/apt/lists/*

# cuDNN 9 für onnxruntime-gpu (Gesichtserkennung/Transkription).
# PyTorch (YOLO/CLAP) bringt sein eigenes cuDNN über den pip-Wheel selbst
# mit — onnxruntime-gpu dagegen erwartet cuDNN als System-Bibliothek
# (libcudnn.so.9) und findet sie sonst nicht, auch wenn PyTorch im selben
# Image tadellos läuft. Ohne diesen Schritt fällt onnxruntime lautlos auf
# CPU zurück (siehe INSTALL.md's Face-Recognition-Abschnitt zum verwandten
# onnxruntime/onnxruntime-gpu-Namespace-Problem — das hier ist eine ANDERE,
# zusätzliche Ursache für dasselbe Symptom).
#
# UNVERIFIZIERT: dieser exakte Build-Schritt wurde nicht gegen eine echte
# GPU getestet. nvidia/cuda-Basis-Images haben normalerweise das NVIDIA-
# apt-Repo schon konfiguriert, sodass "cudnn9-cuda-12" direkt auflösbar
# sein sollte — falls dieser Schritt bei euch fehlschlägt oder eine
# überraschende Unterversion zieht (12.8 vs. z.B. 12.9), bitte melden.
RUN apt-get update && apt-get install -y --no-install-recommends \
    cudnn9-cuda-12 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Erst nur requirements.txt kopieren, damit Docker den pip-Install-Layer
# cached und nicht bei jeder Code-Änderung neu ausführt.
COPY requirements.txt .

# Fix für einen echten Packaging-Konflikt: insightface/faster-whisper hängen
# selbst am reinen (CPU-only) onnxruntime, das denselben Python-Modul-
# Namespace wie onnxruntime-gpu teilt — je nach pip-Installationsreihenfolge
# kann das CPU-Paket zuletzt geschrieben werden und onnxruntime-gpu lautlos
# überschreiben, ohne Fehlermeldung, nur stille CPU-Inferenz für
# Gesichtserkennung/Transkription. Unten erzwungen richtiggestellt. Bewusst
# NUR eine Warnung, kein Build-Abbruch, falls die GPU-Provider trotzdem
# fehlen — Gesichtserkennung/Transkription sind optional und standardmäßig
# aus, CPU-Fallback funktioniert weiterhin, nur langsamer (gleiche
# Philosophie wie beim NVENC/NVDEC-Fallback an anderer Stelle im Projekt).
RUN pip3 install --no-cache-dir --upgrade pip && \
    pip3 install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cu128 && \
    pip3 install --no-cache-dir -r requirements.txt && \
    pip3 uninstall -y onnxruntime onnxruntime-gpu && \
    pip3 install --no-cache-dir --force-reinstall --no-deps onnxruntime-gpu && \
    python3 -c "import onnxruntime as ort; providers = ort.get_available_providers(); print('onnxruntime providers:', providers); print('WARNUNG: CUDAExecutionProvider fehlt - Gesichtserkennung/Transkription laufen auf CPU statt GPU. Siehe INSTALL.md.' if 'CUDAExecutionProvider' not in providers else 'onnxruntime GPU-Unterstuetzung OK.')"

COPY . .

# Läuft standardmäßig als root im Container (üblich für GPU-Workloads, da
# /dev/nvidia* i.d.R. root-Zugriff braucht) — Volumes (alerts/, logs/) landen
# dadurch auch als root-owned auf dem Host. Für saubere Dateirechte auf dem
# Host ggf. per PUID/PGID + entsprechendem Entrypoint erweitern — hier
# bewusst weggelassen, um die Ersteinrichtung nicht zu verkomplizieren.

EXPOSE 19473
