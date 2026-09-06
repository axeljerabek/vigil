#!/usr/bin/env python3
"""
postprocess.py - Einstiegspunkt für die komplette Nachbearbeitung einer
fertigen Aufnahme. Ruft ai_analyze.py (Vision-Beschreibung + Themen) und
transcribe_audio.py (Sprache-zu-Text) SEQUENZIELL im selben Prozess auf.

WARUM SEQUENZIELL, NICHT ALS ZWEI PARALLELE subprocess.Popen-Aufrufe: beide
Schritte lesen und schreiben dieselbe <video>.ai.json (lesen, mit eigenem
Feld ergänzen, zurückschreiben). Liefen sie parallel, könnte je nachdem wer
zuletzt schreibt, die Beschreibung ODER das Transkript verloren gehen
(klassisches Lost-Update-Problem). Sequenziell in einem Prozess umgeht das
komplett, ohne Datei-Locking zu brauchen.

Jeder der beiden Schritte prüft selbst, ob sein Feature überhaupt aktiviert
ist (AI_ANALYSIS_ENABLED / TRANSCRIPTION_ENABLED) — hier wird bewusst immer
versucht, beide aufzurufen, kein Grund für Sonderfälle.

WARUM EIN PROZESSÜBERGREIFENDES LOCK: jede Kamera läuft als eigener
CameraAgent-Prozess und stößt postprocess.py bei Aufnahmeende komplett
unabhängig von den anderen Kameras an (fire-and-forget subprocess.Popen in
recorder_pipeline.py). Enden zwei Aufnahmen unterschiedlicher Kameras
zeitnah, würden ohne dieses Lock zwei postprocess.py-Instanzen gleichzeitig
YOLO/InsightFace/Whisper/CLAP-Modelle auf dieselbe GPU laden und um
Speicher/Rechenzeit konkurrieren. fcntl.flock() blockt automatisch, bis die
GPU frei ist — das ergibt implizit eine serielle Warteschlange, ohne dass
wir selbst eine bauen müssen.

Aufruf (von recorder_pipeline.py per subprocess.Popen, fire-and-forget):
    python3 postprocess.py <video_basename> <base_dir>
"""
import sys
import os
import json
import re
import fcntl
import signal

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(DIR)

import ai_analyze
import transcribe_audio
import face_recognize
import text_recognize
try:
    import mqtt_client
except ImportError:
    mqtt_client = None
try:
    import agent_webhook
except ImportError:
    agent_webhook = None

GPU_LOCK_PATH = os.path.join(DIR, '.postprocess.lock')

# Not-Obergrenze für die komplette Nachbearbeitung -- schützt vor hängendem Ollama/GPU-Kontention, das sonst die ganze Warteschlange blockieren würde (Timeouts pro HTTP-Call reichen dafür nicht).
POSTPROCESS_MAX_SECONDS = 20 * 60  # 20 Minuten für EIN Video, alle drei Schritte zusammen


class PostprocessTimeout(Exception):
    pass


def _watchdog_handler(signum, frame):
    raise PostprocessTimeout("Nachbearbeitung hat die Not-Obergrenze überschritten")


def acquire_gpu_lock():
    """Blockt, bis keine andere postprocess.py-Instanz mehr die GPU
    belegt. Gibt das offene File-Objekt zurück — muss am Ende mit
    release_gpu_lock() wieder freigegeben werden."""
    lock_fd = open(GPU_LOCK_PATH, 'w')
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    return lock_fd


def release_gpu_lock(lock_fd):
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        lock_fd.close()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: postprocess.py <video_basename> <base_dir>")
        sys.exit(1)
    video_basename, base_dir = sys.argv[1], sys.argv[2]

    # base_dir kann veraltet sein (Video evtl. schon archiviert, bis dieser Fire-and-Forget-Prozess läuft) -- Ordner hier neu auflösen, sonst schlägt die Analyse still fehl.
    video_filename = video_basename + '.mp4'
    if not os.path.exists(os.path.join(base_dir, video_filename)):
        archive_dir = os.path.join(base_dir, 'archive')
        if os.path.exists(os.path.join(archive_dir, video_filename)):
            print(f"ℹ️ {video_filename} wurde inzwischen archiviert — verwende den neuen Pfad für die Nachbearbeitung.")
            base_dir = archive_dir
        else:
            print(f"⚠️ {video_filename} weder unter {base_dir} noch im Archiv gefunden — wurde es zwischenzeitlich gelöscht? Nachbearbeitung übersprungen.")
            sys.exit(0)

    print(f"⏳ Warte ggf. auf GPU-Freigabe für {video_basename}...")
    lock_fd = acquire_gpu_lock()
    print(f"🔒 GPU-Lock erhalten, starte Nachbearbeitung für {video_basename}.")
    try:
        # Zweiter Check nötig -- das Video kann während der GPU-Lock-Wartezeit gelöscht worden sein, sonst entstehen verwaiste Sidecar-Dateien.
        if not os.path.exists(os.path.join(base_dir, video_filename)):
            archive_dir = os.path.join(base_dir, 'archive') if not base_dir.endswith('archive') else base_dir
            if os.path.exists(os.path.join(archive_dir, video_filename)):
                base_dir = archive_dir
            else:
                print(f"⚠️ {video_filename} wurde während der GPU-Wartezeit gelöscht — Nachbearbeitung übersprungen.")
                sys.exit(0)
        signal.signal(signal.SIGALRM, _watchdog_handler)
        signal.alarm(POSTPROCESS_MAX_SECONDS)
        try:
            ai_analyze.analyze(video_basename, base_dir)
            transcribe_audio.transcribe(video_basename, base_dir)
            face_recognize.recognize(video_basename, base_dir)
            text_recognize.recognize(video_basename, base_dir)
        finally:
            signal.alarm(0)  # Watchdog deaktivieren, egal ob normal fertig oder ausgelöst
    except PostprocessTimeout:
        print(f"🚨 [Watchdog] Nachbearbeitung für {video_basename} hat {POSTPROCESS_MAX_SECONDS}s überschritten "
              f"(vermutlich hängendes Ollama/GPU-Kontention) — abgebrochen, GPU-Lock wird freigegeben, "
              f"nächstes Video kann weiterlaufen.")
    finally:
        release_gpu_lock(lock_fd)

    # MQTT-Event nach GPU-Lock-Freigabe, damit ein langsamer Broker die nicht verzögert. Läuft auch nach Watchdog-Abbruch, mit dem was bis dahin in der .ai.json steht.
    if mqtt_client is not None or agent_webhook is not None:
        try:
            meta_path = os.path.join(base_dir, f"{video_basename}.ai.json")
            if os.path.exists(meta_path):
                with open(meta_path) as f:
                    meta = json.load(f)
                m = re.match(r"^(.+?)_EVENT_\d{8}_\d{6}$", video_basename)
                camera_name = m.group(1) if m else video_basename
                if meta.get("description"):
                    if mqtt_client is not None:
                        mqtt_client.publish_event_analyzed(
                            camera_name, meta.get("description"), meta.get("topics"), video_filename
                        )
                    if agent_webhook is not None:
                        agent_webhook.notify_event(
                            camera_name, video_filename, meta.get("description"), meta.get("topics"),
                            anomaly=meta.get("anomaly", False), anomaly_score=meta.get("anomaly_score")
                        )
        except Exception as e:
            print(f"⚠️ [MQTT/Webhook] Event-Publish nach Nachbearbeitung fehlgeschlagen: {e}")
