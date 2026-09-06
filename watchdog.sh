#!/bin/bash
# watchdog.sh - Prüft, ob web_ui.py läuft UND antwortet. Für Cron gedacht,
# z.B. alle 5 Minuten: */5 * * * * /pfad/zu/watchdog.sh
#
# WICHTIG: Den RESTART_CMD unten an eure tatsächliche Startweise von
# web_ui.py anpassen (systemd-Service, eigenes Start-Skript, o.ä.) — hier
# nur ein Platzhalter über nohup, analog zu start_detached.sh.

cd "$(dirname "$0")" || exit 1

HEALTH_URL="http://127.0.0.1:19473/health"
LOG_FILE="./logs/watchdog.log"
PYTHON_EXE="./.venv/bin/python"
mkdir -p ./logs

# Anpassen falls web_ui.py anders gestartet wird (z.B. "systemctl restart vaelen-web"):
RESTART_CMD="nohup \"$PYTHON_EXE\" web_ui.py >> ./logs/web_ui_runtime.log 2>&1 &"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

if pgrep -f "web_ui.py" > /dev/null && curl -fsS --max-time 5 "$HEALTH_URL" > /dev/null 2>&1; then
    # Läuft und antwortet — im Normalfall bewusst still, kein Log-Spam alle 5 Minuten
    exit 0
fi

echo "[$(ts)] ⚠️ web_ui.py läuft nicht oder antwortet nicht auf $HEALTH_URL — versuche Neustart." >> "$LOG_FILE"
pkill -f "web_ui.py" 2>/dev/null
sleep 2
eval "$RESTART_CMD"
sleep 3

if curl -fsS --max-time 5 "$HEALTH_URL" > /dev/null 2>&1; then
    echo "[$(ts)] ✅ Neustart erfolgreich." >> "$LOG_FILE"
else
    echo "[$(ts)] ❌ Neustart fehlgeschlagen — bitte manuell prüfen." >> "$LOG_FILE"
fi
