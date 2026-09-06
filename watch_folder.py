#!/usr/bin/env python3
"""
watch_folder.py - Überwacht einen konfigurierbaren Import-Ordner auf neue
Videodateien.

Zwei Modi, je nachdem ob eine neu entdeckte, wachsende Datei sich beim
ersten Anblick als streambar erweist (moov-Atom vor mdat, siehe
mp4_probe.py -- MPEG-TS-Dateien sind das grundsätzlich immer):

  Modus 1 ("live lesen"): die Datei wird SOFORT über eine tailende FIFO
  (live_tail.py) an eine echte CameraAgent-Instanz angehängt -- läuft
  durch dieselbe Erkennungs-/Aufnahme-Pipeline wie eine normale Kamera,
  nicht nur ein blinder Import am Ende. Braucht WATCH_FOLDER_LIVE_MODE_ENABLED.
  Genau dieselbe Technik bedient auch platform_bridge.py für 24/7-
  Plattform-Streams (YouTube/Twitch/...) -- beides "lies eine lokal
  kontinuierlich wachsende Quelle, als wäre sie live".

  Modus 2 ("warten bis fertig", Standard und einziger Modus, falls Modus 1
  aus ist oder die Datei sich als NICHT streambar erweist, z.B. klassisches
  MP4 mit moov am Ende): unverändert wie bisher -- Dateigröße wird
  periodisch geprüft, erst nach WATCH_FOLDER_STABILITY_SEC Sekunden ohne
  Änderung gilt die Datei als fertig und wird importiert.

Läuft als eigener Prozess, vom Master (recorder_pipeline.py) genauso
gestartet wie ein CameraAgent, wenn WATCH_FOLDER_ENABLED aktiv ist — nutzt
dieselbe stop.sh/start_detached.sh-Lebenszyklus-Verwaltung automatisch mit.
"""
import os
import sys
import time
import glob
import json
import shutil
import signal
import subprocess
import multiprocessing

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(DIR)
try:
    from config import ALERTS_DIR, SETTINGS_F, DETECTION_CLASSES, BROWSER_COMPATIBLE_VIDEO_CODECS
except ImportError:
    ALERTS_DIR = "./alerts"
    SETTINGS_F = "pipeline_settings.json"
    DETECTION_CLASSES = [0]
    BROWSER_COMPATIBLE_VIDEO_CODECS = {"h264", "vp9", "av1"}

import backfill_filmstrips

try:
    import mp4_probe
    import live_tail
except ImportError:
    mp4_probe = None
    live_tail = None

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".m4v", ".ts")
POLL_INTERVAL_SEC = 2.0
LIVE_FIFO_DIR_NAME = ".watchfolder_fifos"
# Wie lange eine Live-Quelle ohne Größenänderung als "beendet" gilt --
# deutlich großzügiger als die normale Stability-Prüfung (Modus 2), da eine
# echte Live-Quelle zeitweise stocken kann, ohne dass die Aufnahme wirklich
# vorbei ist.
LIVE_SOURCE_IDLE_TIMEOUT_SEC = 60
# Wie lange eine neu entdeckte Datei wiederholt auf Streamability geprüft
# wird, bevor bei weiterhin unklarem Ergebnis auf Modus 2 zurückgefallen
# wird -- reichlich Zeit für einen langsamen Schreiber, moov (falls
# fast-start) zu schreiben, aber nicht endlos abwarten.
PROBE_TIMEOUT_SEC = 15


def _load_settings():
    try:
        with open(SETTINGS_F) as f:
            return json.load(f)
    except Exception:
        return {}


class GracefulShutdown(BaseException):
    pass


def _video_codec_name(path, logger=print):
    """Liefert den Codec-Namen der ersten Videospur, oder None falls das
    nicht ermittelt werden kann (z.B. keine Videospur, Datei kaputt)."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30
        )
        name = result.stdout.strip()
        return name or None
    except Exception as e:
        logger(f"⚠️ [Watchfolder] Codec-Erkennung fehlgeschlagen für {path}: {e}")
        return None


# Browser-Wiedergabe im Dashboard ist mit diesen Video-Codecs zuverlässig
# kompatibel -- jetzt zentral in config.py (BROWSER_COMPATIBLE_VIDEO_CODECS),
# damit recorder_pipeline.py dieselbe Einschätzung nutzt. Alias hier, damit
# der Rest dieser Datei nicht umbenannt werden muss.
BROWSER_COMPATIBLE_CODECS = BROWSER_COMPATIBLE_VIDEO_CODECS


def _transcode_video_to_h264(src_path, logger=print):
    """Nur die Videospur zu H.264 transkodieren, Audio bleibt Copy (AAC ist
    ohnehin universell abspielbar, keine Notwendigkeit das anzufassen).
    NVENC-Versuch zuerst (passt zur GPU-Beschleunigung im Rest des Systems),
    Software-Fallback falls NVENC nicht verfügbar oder fehlschlägt."""
    tmp_out = os.path.splitext(src_path)[0] + "__transcode.mp4"
    # -hwaccel cuda vor der Eingabe: Decodieren läuft sonst in Software, nur Encodieren auf der GPU -- beobachtet als 400%+ CPU-Last trotz aktivem NVENC.
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-hwaccel", "cuda", "-i", src_path,
             "-c:v", "h264_nvenc", "-c:a", "copy", tmp_out],
            capture_output=True, text=True, timeout=1800
        )
        if result.returncode == 0 and os.path.exists(tmp_out) and os.path.getsize(tmp_out) > 0:
            return tmp_out
        logger(f"⚠️ [Watchfolder] GPU-Transkodierung fehlgeschlagen, versuche Software-Fallback: {result.stderr[-300:]}")
    except Exception as e:
        logger(f"⚠️ [Watchfolder] GPU-Transkodierungs-Fehler: {e}")

    # Software-Fallback bewusst ohne -hwaccel -- fehlt NVENC/CUDA für die
    # Kodierung, ist meist auch kein verlässlicher Hardware-Decode-Pfad da.
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", src_path,
             "-c:v", "libx264", "-c:a", "copy", tmp_out],
            capture_output=True, text=True, timeout=1800
        )
        if result.returncode == 0 and os.path.exists(tmp_out) and os.path.getsize(tmp_out) > 0:
            logger(f"ℹ️ [Watchfolder] NVENC nicht verfügbar, Software-Encoding (libx264) genutzt für {src_path}")
            return tmp_out
        logger(f"⚠️ [Watchfolder] Software-Transkodierung ebenfalls fehlgeschlagen: {result.stderr[-300:]}")
    except Exception as e:
        logger(f"⚠️ [Watchfolder] Software-Transkodierungs-Fehler: {e}")
    return None


def _ensure_mp4(src_path, logger=print):
    """Stellt sicher, dass die Datei (a) im .mp4-Container vorliegt und (b)
    einen im Dashboard-Player zuverlässig abspielbaren Video-Codec nutzt.

    Zwei getrennte Sorgen, zwei getrennte Kosten:
    - Container falsch (z.B. .mkv, .avi) -> günstiger Copy-Remux (kein
      Neu-Encoding, dieselbe Packet-Copy-Philosophie wie beim Rest des
      Systems).
    - Video-Codec inkompatibel (z.B. HEVC) -> echtes Transkodieren NUR der
      Videospur nötig, das kostet tatsächlich Zeit/GPU — aber ohne das würde
      die Datei im Dashboard nur Ton ohne Bild zeigen (genau das beobachtete
      Symptom bei einem per DaVinci Resolve exportierten HEVC-Import)."""
    codec = _video_codec_name(src_path, logger)
    needs_container_fix = not src_path.lower().endswith(".mp4")
    needs_transcode = codec is not None and codec not in BROWSER_COMPATIBLE_CODECS

    if not needs_container_fix and not needs_transcode:
        return src_path

    if needs_transcode:
        logger(f"🎞️ [Watchfolder] Video-Codec '{codec}' ist im Browser unzuverlässig abspielbar, transkodiere zu H.264: {src_path}")
        return _transcode_video_to_h264(src_path, logger)

    # Nur Container falsch, Codec bereits kompatibel -- günstiger Copy-Remux.
    tmp_out = os.path.splitext(src_path)[0] + "__remux.mp4"
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", src_path, "-c", "copy", tmp_out],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0 or not os.path.exists(tmp_out) or os.path.getsize(tmp_out) == 0:
            logger(f"⚠️ [Watchfolder] Remux nach MP4 fehlgeschlagen für {src_path}: {result.stderr[-300:]}")
            return None
        return tmp_out
    except Exception as e:
        logger(f"⚠️ [Watchfolder] Remux-Fehler für {src_path}: {e}")
        return None


def _passes_detection_filter(video_path, run_detection, model, logger=print):
    """Optionaler YOLO-Vorfilter: nur behalten, wenn eine der konfigurierten
    DETECTION_CLASSES irgendwo im Video vorkommt. Ohne aktivierten Filter
    (Standard) wird jede importierte Datei bedingungslos behalten — der
    Watchfolder ist dann ein reiner "alles reinkopierte landet im System"-
    Import, kein Ereignis-Filter wie bei den Live-Kameras."""
    if not run_detection or model is None:
        return True
    try:
        import cv2
        cap = cv2.VideoCapture(video_path)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        # 8 gleichmäßig verteilte Stichproben statt jedes Frames -- reicht für
        # eine grobe Ja/Nein-Entscheidung, ohne das ganze Video zu dekodieren.
        sample_count = 8
        found = False
        for i in range(sample_count):
            frame_idx = int(total * i / sample_count)
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ok, frame = cap.read()
            if not ok:
                continue
            results = model(frame, verbose=False)
            for box in results[0].boxes:
                if int(box.cls[0]) in DETECTION_CLASSES:
                    found = True
                    break
            if found:
                break
        cap.release()
        return found
    except Exception as e:
        logger(f"⚠️ [Watchfolder] Erkennungs-Vorfilter fehlgeschlagen, importiere sicherheitshalber trotzdem: {e}")
        return True


def _mark_processed(path):
    """Legt einen kleinen Marker neben der Originaldatei ab, der Größe und
    Änderungszeit zum Verarbeitungszeitpunkt festhält -- verhindert, dass
    eine Datei, die (mangels WATCH_FOLDER_DELETE_SOURCE, dem Standard) im
    Ordner liegen bleibt, beim nächsten Schleifendurchlauf erneut als 'neu'
    erkannt und endlos wiederverarbeitet wird. Eine echte Datei auf der
    Platte statt nur ein In-Memory-Zustand -- übersteht auch einen Neustart
    des Watchfolder-Prozesses. Ändert sich die Datei später wirklich (neue
    Aufnahme unter demselben Namen), stimmen Größe/Zeit nicht mehr überein
    und sie wird korrekt erneut verarbeitet."""
    try:
        stat = os.stat(path)
        with open(path + ".vaelenimported", "w") as f:
            json.dump({"size": stat.st_size, "mtime": stat.st_mtime}, f)
    except Exception:
        pass


def _already_processed(path):
    marker_path = path + ".vaelenimported"
    if not os.path.exists(marker_path):
        return False
    try:
        with open(marker_path) as f:
            marker = json.load(f)
        stat = os.stat(path)
        return marker.get("size") == stat.st_size and marker.get("mtime") == stat.st_mtime
    except Exception:
        return False


def process_file(src_path, source_name, delete_source, run_detection, model, logger=print):
    """Ein einzelner, vollständig geschriebener Fund aus dem Import-Ordner
    wird hierdurch komplett durchgeschleust: Container-Absicherung, optionaler
    Erkennungs-Filter, Umbenennung in die Standard-Namenskonvention, Filmstrip,
    Trigger-Screenshot, und schließlich postprocess.py für die KI-Analyse."""
    logger(f"📥 [Watchfolder] Neue Datei gefunden: {src_path}")

    mp4_path = _ensure_mp4(src_path, logger)
    if not mp4_path:
        return False

    if not _passes_detection_filter(mp4_path, run_detection, model, logger):
        logger(f"⏭️ [Watchfolder] Kein relevantes Objekt gefunden, verworfen: {src_path}")
        if mp4_path != src_path:
            os.remove(mp4_path)
        if delete_source:
            os.remove(src_path)
        return True

    ts = time.strftime("%Y%m%d_%H%M%S")
    basename = f"{source_name}_EVENT_{ts}"
    dest_path = os.path.join(ALERTS_DIR, basename + ".mp4")
    # Kollisionsschutz -- zwei Importe in derselben Sekunde sind unwahrscheinlich,
    # aber ein Zahlenanhängsel kostet nichts und verhindert ein stilles Überschreiben.
    n = 1
    while os.path.exists(dest_path):
        dest_path = os.path.join(ALERTS_DIR, f"{basename}_{n}.mp4")
        n += 1
        basename = os.path.splitext(os.path.basename(dest_path))[0]

    was_remuxed = (mp4_path != src_path)
    if delete_source or was_remuxed:
        # Entweder soll die Quelle sowieso weg (delete_source), oder mp4_path
        # ist bereits ein neu erzeugtes Remux-Temp-Derivat -- in beiden Fällen
        # spricht nichts dagegen, die Datei zu verschieben statt zu kopieren.
        shutil.move(mp4_path, dest_path)
    else:
        # Kein Remux nötig + Original soll bleiben -- kopieren, nicht verschieben (vorheriger Bug: verschwand trotz 'Original behalten').
        shutil.copy2(mp4_path, dest_path)
    if delete_source and was_remuxed and os.path.exists(src_path):
        os.remove(src_path)

    # Trigger-Screenshot: ein einzelner Frame, analog zum Live-Pfad, damit die
    # Kachel im Dashboard nicht leer bleibt.
    try:
        thumb_path = os.path.splitext(dest_path)[0] + ".jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-ss", "1", "-i", dest_path, "-frames:v", "1", thumb_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30
        )
    except Exception:
        pass

    # Filmstrip -- dieselbe Funktion, die auch backfill_filmstrips.py nutzt.
    thumbs_root = os.path.join(ALERTS_DIR, ".thumbs")
    os.makedirs(thumbs_root, exist_ok=True)
    count = int(_load_settings().get("FILMSTRIP_COUNT", 8)) or 8
    backfill_filmstrips.backfill_filmstrip(dest_path, thumbs_root, count)

    # Kleine Trigger-Metadaten -- markiert klar als Import, nicht als
    # "erkanntes Objekt", damit das im Dashboard nicht mit einer echten
    # Live-Erkennung verwechselt wird.
    try:
        with open(os.path.splitext(dest_path)[0] + ".trigger.json", "w") as f:
            json.dump({"source": "watch_folder_import", "original_filename": os.path.basename(src_path)}, f)
    except Exception:
        pass

    logger(f"✅ [Watchfolder] Importiert als {os.path.basename(dest_path)}")

    # KI-Analyse anstoßen -- derselbe GPU-gesperrte Pfad wie bei normalen
    # Aufnahmen, damit sich Watchfolder-Importe und Live-Kameras nicht um die
    # GPU streiten.
    try:
        subprocess.Popen([sys.executable, os.path.join(DIR, "postprocess.py"), basename, ALERTS_DIR])
    except Exception as e:
        logger(f"⚠️ [Watchfolder] postprocess.py konnte nicht gestartet werden: {e}")

    return True


def _write_live_status(live_sources):
    """Schreibt eine kleine Status-Datei mit allen aktuell laufenden Modus-1-
    Live-Quellen -- der einzige Weg für web_ui.py (ein komplett separater
    Prozess), überhaupt zu wissen, dass gerade ein dynamischer CameraAgent
    für den Watchfolder läuft. Ohne das gäbe es im Dashboard schlicht keine
    Möglichkeit zu sehen, ob Modus 1 überhaupt aktiv ist, außer im Log zu
    suchen."""
    status_path = os.path.join(ALERTS_DIR, ".watchfolder_live_status.json")
    try:
        entries = [
            {"name": entry["stream_name"], "source_path": path, "started_at": entry["started_at"]}
            for path, entry in live_sources.items()
        ]
        with open(status_path, "w") as f:
            json.dump({"active": entries, "updated_at": time.time()}, f)
    except Exception:
        pass


def _start_live_source(path, source_name, logger=print):
    """Startet Modus 1 für eine neu entdeckte, als streambar erkannte
    Datei: FIFO anlegen, Live-Tail starten, und eine echte CameraAgent-
    Instanz auf die FIFO ansetzen -- läuft danach durch dieselbe
    Erkennungs-/Aufnahme-Pipeline wie eine normale Kamera, nicht nur ein
    blinder Import am Ende. Gibt ein Dict mit den laufenden Komponenten
    zurück, oder None bei einem Fehler (Aufrufer fällt dann auf Modus 2
    zurück)."""
    try:
        from recorder_pipeline import CameraAgent
    except ImportError as e:
        logger(f"⚠️ [Watchfolder] CameraAgent konnte für Live-Modus nicht importiert werden, falle auf Modus 2 zurück: {e}")
        return None

    fifo_dir = os.path.join(ALERTS_DIR, LIVE_FIFO_DIR_NAME)
    os.makedirs(fifo_dir, exist_ok=True)
    safe_name = f"{source_name}_{os.path.basename(path)}".replace("/", "_")
    fifo_path = os.path.join(fifo_dir, f"{safe_name}.fifo")

    tailer = live_tail.GrowingFileTailer(path, fifo_path)
    try:
        tailer.start()
    except Exception as e:
        logger(f"⚠️ [Watchfolder] Live-Tail für '{path}' konnte nicht gestartet werden: {e}")
        return None

    stream_info = {
        "name": f"{source_name}_Live",
        "url": fifo_path,
        "enabled": True,
        "audio_enabled": True,
        "notify_only": False,
        "type": "VIDEO",
    }
    agent = CameraAgent(stream_info, half_precision=True)
    agent.start()
    logger(f"🎬 [Watchfolder] '{path}' ist streambar -- läuft jetzt live über '{stream_info['name']}' statt auf Fertigstellung zu warten.")
    return {
        "tailer": tailer, "agent": agent, "fifo_path": fifo_path,
        "last_size": -1, "last_change": time.time(),
        "stream_name": stream_info["name"], "started_at": time.time(),
    }


def _stop_live_source(entry, logger=print):
    """Beendet eine per _start_live_source() gestartete Live-Verarbeitung
    sauber -- CameraAgent zuerst (damit eine laufende Aufnahme noch
    ordentlich abgeschlossen wird), dann der Tailer, dann die FIFO von der
    Platte entfernen."""
    try:
        entry["agent"].stop_agent()
        entry["agent"].join(timeout=10)
    except Exception as e:
        logger(f"⚠️ [Watchfolder] Fehler beim Stoppen des Live-CameraAgent: {e}")
    try:
        entry["tailer"].stop()
        entry["tailer"].cleanup()
    except Exception as e:
        logger(f"⚠️ [Watchfolder] Fehler beim Stoppen des Live-Tailers: {e}")


class WatchFolderAgent(multiprocessing.Process):
    """Eigener Prozess, analog zu CameraAgent -- vom Master gestartet, wenn
    WATCH_FOLDER_ENABLED in den Settings aktiv ist."""

    def __init__(self):
        super().__init__(daemon=False)
        self._stop_event = multiprocessing.Event()

    def stop_agent(self):
        self._stop_event.set()

    def run(self):
        def _handle_signal(signum, frame):
            self._stop_event.set()
            raise GracefulShutdown()
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

        print("🚀 [Watchfolder] Prozess gestartet.")
        model = None
        seen = {}  # path -> (size, last_change_ts) -- Modus 2 (warten bis fertig)
        live_sources = {}  # path -> Dict von _start_live_source() -- Modus 1 (live lesen)
        pending_probe = {}  # path -> first_seen_ts -- noch keine endgültige Streamability-Antwort

        try:
            while not self._stop_event.is_set():
                settings = _load_settings()
                folder = (settings.get("WATCH_FOLDER_PATH") or "").strip()
                source_name = (settings.get("WATCH_FOLDER_SOURCE_NAME") or "Import").strip() or "Import"
                stability_sec = float(settings.get("WATCH_FOLDER_STABILITY_SEC", 5) or 5)
                delete_source = bool(settings.get("WATCH_FOLDER_DELETE_SOURCE", False))
                run_detection = bool(settings.get("WATCH_FOLDER_RUN_DETECTION", False))
                live_mode_enabled = bool(settings.get("WATCH_FOLDER_LIVE_MODE_ENABLED", False)) and mp4_probe is not None and live_tail is not None

                if not folder or not os.path.isdir(folder):
                    time.sleep(POLL_INTERVAL_SEC)
                    continue

                if run_detection and model is None:
                    try:
                        from ultralytics import YOLO
                        from config import MODEL_FILENAME
                        model = YOLO(MODEL_FILENAME)
                    except Exception as e:
                        print(f"⚠️ [Watchfolder] YOLO-Modell für Vorfilter konnte nicht geladen werden, importiere ungefiltert: {e}")

                current_files = set()
                for ext in VIDEO_EXTENSIONS:
                    current_files.update(glob.glob(os.path.join(folder, f"*{ext}")))

                now = time.time()

                # Laufende Live-Quellen (Modus 1) zuerst prüfen -- beendet
                # sich die Quelldatei (verschwindet oder wächst lange nicht
                # mehr), sauber stoppen, statt für immer eine tote FIFO offen
                # zu halten.
                for path in list(live_sources.keys()):
                    entry = live_sources[path]
                    if path not in current_files:
                        print(f"🎬 [Watchfolder] Live-Quelle '{path}' ist verschwunden -- stoppe.")
                        _stop_live_source(entry)
                        live_sources.pop(path, None)
                        continue
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        size = entry["last_size"]
                    if size != entry["last_size"]:
                        entry["last_size"] = size
                        entry["last_change"] = now
                    elif now - entry["last_change"] >= LIVE_SOURCE_IDLE_TIMEOUT_SEC:
                        print(f"🎬 [Watchfolder] Live-Quelle '{path}' seit {LIVE_SOURCE_IDLE_TIMEOUT_SEC}s ohne Änderung -- vermutlich beendet, stoppe.")
                        _stop_live_source(entry)
                        # WICHTIG: als verarbeitet markieren, BEVOR aus
                        # live_sources entfernt wird -- sonst würde dieselbe,
                        # unverändert liegen gebliebene Datei (Standard:
                        # WATCH_FOLDER_DELETE_SOURCE ist aus) im nächsten
                        # Durchlauf wieder als "neu" erkannt und der Live-
                        # Modus endlos neu gestartet.
                        _mark_processed(path)
                        live_sources.pop(path, None)

                # Dateien, deren Streamability beim letzten Versuch noch
                # UNKNOWN war (z.B. gerade erst angelegt, noch kein moov
                # geschrieben), erneut prüfen -- sonst würde eine Datei, die
                # zufällig im allerersten Sichtungsmoment noch zu wenig
                # Daten enthielt, für immer fälschlich in Modus 2 landen,
                # obwohl Sekunden später genug geschrieben wäre, um sie
                # korrekt als streambar zu erkennen.
                for path in list(pending_probe.keys()):
                    if path not in current_files:
                        pending_probe.pop(path, None)
                        continue
                    probe_result = mp4_probe.probe_mp4_streamability(path)
                    if probe_result == mp4_probe.STREAMABLE:
                        live_entry = _start_live_source(path, source_name)
                        pending_probe.pop(path, None)
                        if live_entry is not None:
                            live_sources[path] = live_entry
                        else:
                            try:
                                seen[path] = (os.path.getsize(path), now)
                            except OSError:
                                pass
                    elif probe_result == mp4_probe.NOT_STREAMABLE:
                        # Endgültige Antwort: definitiv kein moov-first --
                        # sofort in Modus 2 übernehmen, nicht weiter abwarten.
                        pending_probe.pop(path, None)
                        try:
                            seen[path] = (os.path.getsize(path), now)
                        except OSError:
                            pass
                    elif now - pending_probe[path] >= PROBE_TIMEOUT_SEC:
                        # Immer noch unklar nach reichlich Wartezeit -- eher
                        # eine sehr langsam schreibende oder ungewöhnliche
                        # Quelle als ein Formatproblem; nicht ewig weiter
                        # abfragen, lieber auf Modus 2 zurückfallen.
                        print(f"🎬 [Watchfolder] '{path}' nach {PROBE_TIMEOUT_SEC}s immer noch nicht eindeutig einzuordnen -- falle auf Modus 2 zurück.")
                        pending_probe.pop(path, None)
                        try:
                            seen[path] = (os.path.getsize(path), now)
                        except OSError:
                            pass

                for path in current_files:
                    if path in live_sources or path in pending_probe:
                        continue  # werden oben bereits behandelt
                    if path not in seen and _already_processed(path):
                        # Liegt unverändert seit der letzten Verarbeitung da
                        # (Standard: WATCH_FOLDER_DELETE_SOURCE ist aus) --
                        # NICHT erneut als "neu" behandeln, sonst Endlosschleife.
                        continue
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        continue
                    if path not in seen:
                        # Neu entdeckte Datei: bei aktiviertem Live-Modus SOFORT
                        # prüfen, ob sie streambar ist.
                        if live_mode_enabled:
                            is_ts = path.endswith(".ts")  # MPEG-TS braucht kein moov, grundsätzlich immer streambar
                            if is_ts:
                                live_entry = _start_live_source(path, source_name)
                                if live_entry is not None:
                                    live_sources[path] = live_entry
                                    continue
                            else:
                                probe_result = mp4_probe.probe_mp4_streamability(path)
                                if probe_result == mp4_probe.STREAMABLE:
                                    live_entry = _start_live_source(path, source_name)
                                    if live_entry is not None:
                                        live_sources[path] = live_entry
                                        continue
                                elif probe_result == mp4_probe.UNKNOWN:
                                    # Noch keine endgültige Antwort möglich --
                                    # NICHT sofort auf Modus 2 festlegen,
                                    # sondern bei den nächsten Durchläufen
                                    # weiter beobachten (siehe pending_probe
                                    # oben).
                                    pending_probe[path] = now
                                    continue
                                # NOT_STREAMABLE fällt durch zu seen[] unten.
                        seen[path] = (size, now)
                    else:
                        old_size, last_change = seen[path]
                        if size != old_size:
                            seen[path] = (size, now)
                        elif now - last_change >= stability_sec:
                            process_file(path, source_name, delete_source, run_detection, model)
                            # Falls die Quelle liegen bleibt (Standard:
                            # WATCH_FOLDER_DELETE_SOURCE ist aus), verhindert
                            # der Marker eine endlose Neuverarbeitung beim
                            # nächsten Durchlauf.
                            if os.path.exists(path):
                                _mark_processed(path)
                            seen.pop(path, None)

                # Verwaiste Einträge (Datei zwischenzeitlich verschwunden) aufräumen
                for path in list(seen.keys()):
                    if path not in current_files:
                        seen.pop(path, None)

                # Verwaiste .vaelenimported-Marker aufräumen (Original inzwischen
                # gelöscht) -- rein kosmetisch, sonst sammeln sich mit der Zeit
                # nutzlose kleine Dateien im Watchfolder an.
                for marker_path in glob.glob(os.path.join(folder, "*.vaelenimported")):
                    if not os.path.exists(marker_path[:-len(".vaelenimported")]):
                        try:
                            os.remove(marker_path)
                        except OSError:
                            pass

                # Einmal pro Durchlauf reicht -- die Status-Datei muss nur
                # "irgendwann bald" stimmen, kein Grund, sie an jeder
                # einzelnen Änderungsstelle im Code separat zu pflegen.
                _write_live_status(live_sources)

                time.sleep(POLL_INTERVAL_SEC)
        except GracefulShutdown:
            print("🛑 [Watchfolder] Shutdown-Signal empfangen.")
        except Exception as e:
            print(f"💥 [Watchfolder] Prozess-Crash: {e}")
        finally:
            for entry in live_sources.values():
                _stop_live_source(entry)
            _write_live_status({})
            print("🛑 [Watchfolder] Prozess beendet.")


if __name__ == "__main__":
    agent = WatchFolderAgent()
    agent.run()
