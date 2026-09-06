#!/bin/bash
# start_detached.sh - Startet die vaelen-Pipeline losgelöst vom aufrufenden Prozess.

# Robust gegen abweichendes Arbeitsverzeichnis (z.B. falls das Skript mal
# nicht mit cwd=PROJECT_ROOT aufgerufen wird): immer relativ zum eigenen
# Skript-Ordner arbeiten, statt uns auf den Aufrufer zu verlassen.
cd "$(dirname "$0")" || exit 1

LOG_FILE="./logs/pipeline_runtime.log"
mkdir -p ./logs

# Bare-Metal-Deployment nutzt eine venv (.venv/bin/python) — Docker-Deployment
# installiert Pakete dagegen direkt systemweit per pip3 (siehe Dockerfile,
# kein "python3 -m venv" dort), web_ui.py läuft im Container schon lange
# erfolgreich mit reinem "python3". start_detached.sh bestand bisher IMMER
# auf der venv und schlug im Container-Kontext deshalb zuverlässig mit
# "VENV executable not found" fehl — kein Bug, sondern zwei unterschiedliche
# Deployment-Arten, die dasselbe Skript nie beide bedient hatte. Jetzt: venv
# nutzen, falls vorhanden, sonst automatisch auf System-Python zurückfallen.
if [ -f "./.venv/bin/python" ]; then
    PYTHON_EXE="./.venv/bin/python"
else
    PYTHON_EXE="$(command -v python3)"
fi

# Verhindert doppelte Instanzen (z.B. Doppelklick auf Start, bevor die UI den
# Status aktualisiert hat) — zwei parallele Pipelines würden sich bei
# denselben Kamera-Streams und Ausgabedateien in die Quere kommen.
if pgrep -f "recorder_pipeline.py" > /dev/null; then
    echo "⚠️  [$(date)] Pipeline läuft bereits — Start übersprungen." | tee -a "$LOG_FILE"
    exit 0
fi

echo "🚀 [$(date)] Launching vaelen Pipeline..." | tee -a "$LOG_FILE"

if [ -f "./.venv/bin/python" ]; then
    VENV_SITES=$(find .venv -name "site-packages" -type d | head -n 1)
    if [ -n "$VENV_SITES" ]; then
        export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$VENV_SITES"
        echo "🔍 Python Library Path Injected: $VENV_SITES" | tee -a "$LOG_FILE"
    fi
fi

if [ -z "$PYTHON_EXE" ] || [ ! -x "$PYTHON_EXE" ]; then
    echo "❌ ERROR: Kein ausführbares Python gefunden (weder .venv noch System-python3)!" | tee -a "$LOG_FILE"
    exit 1
fi

nohup "$PYTHON_EXE" recorder_pipeline.py >> "$LOG_FILE" 2>&1 &
PID=$!
echo "$PID" > ./pipeline.pid
echo "✅ Pipeline started with PID: $PID (via $PYTHON_EXE)" | tee -a "$LOG_FILE"
