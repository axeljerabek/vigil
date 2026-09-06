#!/bin/bash
# stop.sh - Stoppt die vaelen-Pipeline sauber.
#
# recorder_pipeline.py fängt SIGTERM jetzt selbst ab (siehe dortige
# Fixes) und schließt laufende Aufnahmen sauber (Flush + close), bevor der
# Prozess beendet wird. Das braucht kurz Zeit — deshalb hier aktiv pollen
# statt eines festen "sleep 2", und erst nach einer Grace Period hart
# nachhelfen (SIGKILL), falls ein Prozess doch mal hängt.
#
# WICHTIGER FIX: die Kamera-Worker laufen als multiprocessing-Kindprozesse
# mit der 'spawn'-Startmethode — deren Kommandozeile lautet dann generisch
# "python3 -c from multiprocessing.spawn import spawn_main; ..." und
# enthält "recorder_pipeline.py" NICHT mehr. Das bisherige reine
# Pattern-Matching (pkill -f) hat darum nie die eigentlichen Kamera-Worker
# erreicht, nur den Master — bei jedem Neustart blieben die Worker als
# Waisen zurück und liefen (inklusive Aufnahme!) einfach unbemerkt weiter,
# während ein neuer Master zusätzliche, neue Worker startete. Jetzt wird
# zusätzlich die beim Start gemerkte Master-PID genutzt, um über die
# ECHTE Eltern-Kind-Beziehung (pkill -P) gezielt auch die Worker zu
# treffen. Das bisherige Pattern-Matching bleibt als Sicherheitsnetz
# bestehen (z.B. falls die PID-Datei fehlt oder veraltet ist).

cd "$(dirname "$0")" || exit 1

PATTERN="recorder_pipeline.py"
PID_FILE="./pipeline.pid"
GRACE_SECONDS=15

echo "Stopping vaelen Pipeline..."

# Gezielt über die gemerkte Master-PID: erst deren direkte Kindprozesse
# (die eigentlichen Kamera-Worker), dann den Master selbst.
if [ -f "$PID_FILE" ]; then
    MASTER_PID=$(cat "$PID_FILE")
    if [ -n "$MASTER_PID" ] && kill -0 "$MASTER_PID" 2>/dev/null; then
        echo "Beende Kind-Prozesse von Master-PID $MASTER_PID..."
        pkill -TERM -P "$MASTER_PID" 2>/dev/null
        kill -TERM "$MASTER_PID" 2>/dev/null
    fi
    rm -f "$PID_FILE"
fi

if ! pgrep -f "$PATTERN" > /dev/null; then
    echo "Pipeline war nicht (mehr) aktiv."
    exit 0
fi

# Sicherheitsnetz: SIGTERM an alles, was noch auf das Skript-Namens-Pattern
# matcht (fängt z.B. den Master, falls die PID-Datei fehlte/veraltet war).
pkill -TERM -f "$PATTERN"

waited=0
while pgrep -f "$PATTERN" > /dev/null; do
    if [ "$waited" -ge "$GRACE_SECONDS" ]; then
        echo "⚠️  Pipeline reagiert nach ${GRACE_SECONDS}s nicht auf SIGTERM — erzwinge SIGKILL."
        pkill -KILL -f "$PATTERN"
        sleep 1
        break
    fi
    sleep 1
    waited=$((waited + 1))
done

if pgrep -f "$PATTERN" > /dev/null; then
    echo "❌ Konnte nicht alle Pipeline-Prozesse beenden. Bitte manuell prüfen: pgrep -fa \"$PATTERN\""
    exit 1
fi

echo "✅ Pipeline Stopped (nach ${waited}s)."
