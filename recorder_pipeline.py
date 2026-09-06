import json
import os
import sys
import subprocess
import time
import signal
import random
import threading
import queue
import datetime
import multiprocessing
from collections import deque
from fractions import Fraction  # nur für den Encode-Modus (MJPEG/USB-Kameras) gebraucht

try:
    from audio_trigger import AudioTrigger
except ImportError:
    AudioTrigger = None  # Optionales Feature — Pipeline läuft unverändert ohne es

try:
    import pose_fall_detection
except ImportError:
    pose_fall_detection = None  # Optionales Feature — Pipeline läuft unverändert ohne es

try:
    import loitering_detection
except ImportError:
    loitering_detection = None  # Optionales Feature — Pipeline läuft unverändert ohne es

try:
    import platform_source
except ImportError:
    platform_source = None  # Optionales Feature — Pipeline läuft unverändert ohne es

try:
    import platform_bridge
except ImportError:
    platform_bridge = None  # Optionales Feature — Pipeline läuft unverändert ohne es

# CPU-Thread-Wildwuchs von PyTorch/OpenBLAS global drosseln
os.environ["OMP_NUM_THREADS"] = "2"
os.environ["OPENBLAS_NUM_THREADS"] = "2"
os.environ["MKL_NUM_THREADS"] = "2"
os.environ["VECLIB_MAXIMUM_THREADS"] = "2"
os.environ["NUMEXPR_NUM_THREADS"] = "2"

# Fix 4: cuDNN war hart deaktiviert (Kommentar: "cuDNN Sublibrary Mismatch"),
# was auf einer aktuellen GPU spürbar Performance kostet, da cuDNN die
# GPU-beschleunigten Convolutions übernimmt. Jetzt wird cuDNN standardmäßig
# versucht — mit einem automatischen Selbsttest beim Modell-Laden (siehe
# unten): schlägt der fehl, wird cuDNN pro Prozess automatisch deaktiviert
# und neu geladen, statt manuell raten zu müssen. DISABLE_CUDNN=1 erzwingt
# weiterhin das alte, garantiert sichere Verhalten ohne jeden Selbsttest.
DISABLE_CUDNN = os.environ.get("DISABLE_CUDNN", "0") == "1"

# 1. PATH RESOLUTION
DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(DIR)

try:
    from config import (
        STREAMS, ALERTS_DIR, MODEL_PATH, PRE_ROLL_SEC, SETTINGS_F, STREAMS_F,
        POST_ROLL_SEC, TARGET_FPS, DETECTION_CLASSES, CONFIDENCE_THRESHOLD,
        get_stream_logger, system_logger, YOLO_VERSION, BROWSER_COMPATIBLE_VIDEO_CODECS
    )
except ImportError as e:
    print(f"❌ CRITICAL ERROR: Could not load config.py: {e}")
    sys.exit(1)

# Optional -- fehlt paho-mqtt oder ist die Datei nicht vorhanden, soll das
# die Aufnahme-Pipeline nicht zum Absturz bringen. mqtt_client.publish() ist
# selbst schon fehlertolerant, das ist hier nur eine zusätzliche
# Absicherungsebene für dieses besonders sensible File.
try:
    import mqtt_client
except ImportError:
    mqtt_client = None

# Für den REC-Indikator im Dashboard: aktueller Zustand pro Stream, von
# web_ui.py gelesen (kein *.mp4-Glob-Konflikt durch führenden Punkt).
STATUS_DIR = os.path.join(ALERTS_DIR, '.status')
os.makedirs(STATUS_DIR, exist_ok=True)

# Filmstrip-Schreiben läuft im Hintergrund-Thread statt im Capture-Loop -- vermeidet I/O-Stalls, die sonst als Ruckeln sichtbar würden.
_filmstrip_write_queue = queue.Queue()

def _filmstrip_writer_loop():
    import cv2  # lokal wie an anderer Stelle im CameraAgent — hält den Master-
    # Prozess leicht, nur Kamera-Prozesse, die den Thread tatsächlich starten,
    # zahlen die Importkosten.
    while True:
        item = _filmstrip_write_queue.get()
        if item is None:
            break
        try:
            kind, path, payload = item
            if kind == 'jpg':
                cv2.imwrite(path, payload)
            elif kind == 'json':
                with open(path, 'w') as f:
                    json.dump(payload, f)
        except Exception:
            pass
        finally:
            _filmstrip_write_queue.task_done()

threading.Thread(target=_filmstrip_writer_loop, daemon=True).start()

# Live-Vorschau (Box-Zeichnen, Resize, Encode) läuft ebenfalls im Hintergrund-Thread, nicht im Haupt-Loop -- lief kontinuierlich, unabhängig von aktiver Aufnahme.
_shared_frame_write_queue = queue.Queue()

def _draw_boxes_with_labels(cv2, img, boxes, names):
    """Zeichnet Box + Klassenname + Konfidenz — Ersatz für results[0].plot(),
    aber mit reinen (GPU-losgelösten) Werten, sicher über Zeit-/Thread-
    Grenzen hinweg aufzuheben. box-Zeilen: x1,y1,x2,y2,conf,cls_id.

    Wird auf dem VOLLEN Kamerabild gezeichnet (z.B. 1920px), das Ergebnis
    aber meist erst DANACH auf ~640px runterskaliert — eine feste
    Schriftgröße wäre nach diesem Resize kaum noch lesbar. Skaliert daher
    proportional zur tatsächlichen Bildbreite (640px als Referenz, worauf
    die Basiswerte kalibriert sind), damit nach dem Resize immer dieselbe
    lesbare Endgröße rauskommt, unabhängig von der Kamera-Auflösung."""
    scale = max(1.0, img.shape[1] / 640.0)
    font_scale = 0.55 * scale
    thickness = max(1, round(1.5 * scale))
    box_thickness = max(1, round(2 * scale))
    for b in boxes:
        x1, y1, x2, y2 = map(int, b[:4])
        conf = float(b[4]) if len(b) > 4 else None
        cls_id = int(b[5]) if len(b) > 5 else None
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 220, 0), box_thickness)
        if conf is not None:
            cls_name = names.get(cls_id, str(cls_id)) if names and cls_id is not None else (str(cls_id) if cls_id is not None else '')
            label = f"{cls_name} {conf:.2f}" if cls_name else f"{conf:.2f}"
            (tw, th), baseline = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
            pad = round(6 * scale)
            cv2.rectangle(img, (x1, max(0, y1 - th - baseline - pad)), (x1 + tw + pad * 2, y1), (0, 220, 0), -1)
            cv2.putText(img, label, (x1 + pad, max(th, y1 - baseline // 2 - 2)), cv2.FONT_HERSHEY_SIMPLEX, font_scale, (0, 0, 0), thickness, cv2.LINE_AA)

def _shared_frame_writer_loop():
    import cv2
    frames_dir = os.path.join(ALERTS_DIR, '.frames')
    os.makedirs(frames_dir, exist_ok=True)
    while True:
        item = _shared_frame_write_queue.get()
        if item is None:
            break
        try:
            name, img_bgr, boxes, names = item
            source = img_bgr
            if boxes is not None and len(boxes) > 0:
                try:
                    source = img_bgr.copy()
                    _draw_boxes_with_labels(cv2, source, boxes, names)
                except Exception:
                    source = img_bgr
            small = cv2.resize(source, (640, max(1, int(source.shape[0] * 640 / source.shape[1]))))
            ok, buf = cv2.imencode('.jpg', small, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if ok:
                tmp = os.path.join(frames_dir, f'.{name}.tmp')
                with open(tmp, 'wb') as f:
                    f.write(buf.tobytes())
                os.replace(tmp, os.path.join(frames_dir, f'{name}.jpg'))
        except Exception:
            pass
        finally:
            _shared_frame_write_queue.task_done()

threading.Thread(target=_shared_frame_writer_loop, daemon=True).start()

def _load_filmstrip_settings():
    """FILMSTRIP_COUNT=0 -> Feature aus. Live aus der Settings-Datei gelesen,
    kein Pipeline-Neustart bei Änderung nötig."""
    try:
        with open(SETTINGS_F) as f:
            d = json.load(f)
        return int(d.get('FILMSTRIP_COUNT', 0)), float(d.get('FILMSTRIP_INTERVAL_SEC', 2.0))
    except Exception:
        return 0, 2.0

def _postprocessing_enabled():
    """Ob überhaupt Grund besteht, postprocess.py zu starten — Vision-Analyse,
    Transkription ODER Gesichtserkennung reicht schon, postprocess.py
    entscheidet dann selbst pro Schritt anhand seines eigenen Enabled-Flags
    weiter."""
    try:
        with open(SETTINGS_F) as f:
            s = json.load(f)
        return (bool(s.get('AI_ANALYSIS_ENABLED', False))
                or bool(s.get('TRANSCRIPTION_ENABLED', False))
                or bool(s.get('FACE_RECOGNITION_ENABLED', False)))
    except Exception:
        return False

def _load_settings_dict():
    """Komplettes Settings-Dict, roh — für Features wie AudioTrigger, die
    mehrere Werte auf einmal live abfragen wollen."""
    try:
        with open(SETTINGS_F) as f:
            return json.load(f)
    except Exception:
        return {}

def _pose_estimation_settings():
    """Ob Pose-Estimation/Sturzerkennung aktiviert ist, plus die
    einstellbaren Schwellwerte für alle Verhaltens-Auswertungen. Wird nur
    beim Kamera-Prozessstart gelesen (Modell-Ladung passiert einmalig),
    nicht pro Frame -- Änderung braucht wie bei YOLO_VERSION einen
    Neustart der Kamera."""
    s = _load_settings_dict()
    return {
        "fall_enabled": bool(s.get('POSE_ESTIMATION_ENABLED', False)),
        "fall_angle_threshold": float(s.get('POSE_FALL_ANGLE_THRESHOLD', 55.0)),
        "raised_hands_enabled": bool(s.get('POSE_RAISED_HANDS_ENABLED', False)),
        "loitering_enabled": bool(s.get('POSE_LOITERING_ENABLED', False)),
        "loitering_seconds": float(s.get('POSE_LOITERING_SECONDS', 30.0)),
        "gaze_enabled": bool(s.get('POSE_GAZE_ENABLED', False)),
        "pointing_enabled": bool(s.get('POSE_POINTING_ENABLED', False)),
        "movement_enabled": bool(s.get('POSE_MOVEMENT_ENABLED', False)),
        "proximity_enabled": bool(s.get('POSE_PROXIMITY_ENABLED', False)),
    }

def _write_state(name, state):
    try:
        with open(os.path.join(STATUS_DIR, f'{name}.json'), 'w') as f:
            json.dump({'state': state}, f)
    except Exception:
        pass


def _publish_mqtt_recording(name, is_recording):
    """RECORDING und POST_ROLL zählen für Home Assistant beide als
    "Aufnahme aktiv" (die Datei wird in beiden Zuständen noch beschrieben)
    -- nur der echte Übergang zu IDLE bedeutet "aus". mqtt_client.publish()
    ist selbst schon nicht-blockierend und fehlertolerant; das try/except
    hier ist nur zusätzliche Absicherung für dieses sensible File."""
    if mqtt_client is None:
        return
    try:
        mqtt_client.publish_recording_state(name, is_recording)
    except Exception:
        pass


def _notify_pose_event(event_type, camera_name, description, extra_score=None):
    """Gemeinsame Benachrichtigung für alle Pose-/Verhaltens-Ereignisse
    (Sturz, Notsignal, Loitering) -- MQTT und Agent-Webhook, beide bereits
    selbst fire-and-forget/fehlertolerant; das try/except hier ist nur
    zusätzliche Absicherung für dieses sensible File, dasselbe Muster wie
    _publish_mqtt_recording."""
    if mqtt_client is not None:
        try:
            mqtt_client.publish(f"{camera_name}/{event_type}", {"description": description}, retain=False)
        except Exception:
            pass
    try:
        import agent_webhook
        agent_webhook.notify_event(camera_name, None, description, {}, anomaly=True, anomaly_score=extra_score)
    except Exception:
        pass


TRIGGER_DIR = os.path.join(ALERTS_DIR, '.triggers')
STOP_DIR = os.path.join(ALERTS_DIR, '.stops')
DETECTION_DIR = os.path.join(ALERTS_DIR, '.detections')
os.makedirs(TRIGGER_DIR, exist_ok=True)
os.makedirs(STOP_DIR, exist_ok=True)
os.makedirs(DETECTION_DIR, exist_ok=True)


def _check_and_clear_manual_trigger(name):
    """Prüft, ob ein externer Trigger (Agent/API) für diese Kamera angefordert
    wurde -- reine Datei-Existenzprüfung, kein Locking nötig, da nur dieser
    eine Prozess die Datei je liest/löscht, und die schreibende Seite
    (mam_api.py) sie nur einmalig anlegt."""
    path = os.path.join(TRIGGER_DIR, f'{name}.flag')
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass
        return True
    return False


def _check_and_clear_manual_stop(name):
    """Gegenstück zum Trigger: beendet eine LAUFENDE Aufnahme sofort (statt
    auf das normale Post-Roll-Ende zu warten). Wichtig, weil 'enabled: false'
    (cameras_toggle/disable) das NICHT tut -- das wird nur beim
    Prozessstart gelesen, ein schon laufender Worker merkt eine spätere
    Änderung dort nie. Das hier ist der tatsächlich richtige Weg, eine
    laufende Aufnahme von außen zu beenden."""
    path = os.path.join(STOP_DIR, f'{name}.flag')
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass
        return True
    return False


_last_detection_notify = {}  # Kameraname -> Zeitstempel der letzten Meldung
DETECTION_NOTIFY_MIN_INTERVAL = 5  # Sekunden -- verhindert Spam bei durchgehender Erkennung


def _write_detection_notification(name, detected_names):
    """Im notify_only-Modus: statt automatisch aufzunehmen, wird hier nur
    gemeldet, was gerade erkannt wurde -- als Datei (für den Agenten-
    Abfrage-Endpunkt) und per MQTT. Gedrosselt, damit eine durchgehende
    Erkennung nicht bei jedem Frame neu schreibt/published."""
    now = time.time()
    last = _last_detection_notify.get(name, 0)
    if now - last < DETECTION_NOTIFY_MIN_INTERVAL:
        return
    _last_detection_notify[name] = now
    payload = {"camera": name, "detected_classes": sorted(set(detected_names)), "timestamp": now}
    try:
        with open(os.path.join(DETECTION_DIR, f'{name}.json'), 'w') as f:
            json.dump(payload, f)
    except Exception:
        pass
    if mqtt_client is not None:
        try:
            mqtt_client.publish(f"{name}/detection", payload)
        except Exception:
            pass


class GracefulShutdown(BaseException):
    """Eigene Exception statt Exception-Basisklasse, damit sie nicht versehentlich
    vom generischen 'except Exception' als Crash geloggt wird (siehe Fix 2)."""
    pass


# 2. Class Definition for the Camera Agent
class CameraAgent(multiprocessing.Process):
    def __init__(self, stream_info, half_precision=True):
        super().__init__()
        self.name = stream_info["name"]
        self.url = stream_info.get("url", "")
        # Plattform-URLs (YouTube/Twitch/Vimeo/...) laufen NICHT direkt über
        # av.open() -- ein persistenter Hintergrund-Prozess (PlatformStreamBridge)
        # kümmert sich robust um yt-dlp-Auflösung/Reconnect und schreibt in eine
        # lokale FIFO; self.url wird für diesen Fall auf die FIFO umgebogen.
        # Das entkoppelt vaelens eigene Verbindungs-Retry-Schleife komplett von
        # plattformspezifischen Eigenheiten (ablaufende signierte URLs,
        # Offline-Kanäle) -- dieselbe FIFO-Technik wie bei Watchfolder-Modus 1.
        self._original_platform_url = self.url if platform_source is not None and platform_source.needs_resolution(self.url) else None
        self._platform_bridge = None
        if self._original_platform_url is not None:
            fifo_path = os.path.join(ALERTS_DIR, ".platform_fifos", f"{self.name}.fifo")
            self.url = fifo_path
        self.enabled = stream_info.get("enabled", False)
        # Default True — bestehende streams.json-Einträge von vor diesem
        # Feature haben das Feld noch nicht, sollen sich aber nicht plötzlich
        # stumm schalten.
        self.audio_enabled = stream_info.get("audio_enabled", True)
        # Notify-only: YOLO erkennt weiterhin normal, löst aber KEINE
        # automatische Aufnahme aus -- nur eine Erkennungs-Meldung (Datei +
        # MQTT). Aufnahme startet dann ausschließlich über einen externen
        # Trigger (Agent/API). Default False -- bestehendes Verhalten
        # bleibt für alle Kameras ohne dieses Feld exakt gleich.
        self.notify_only = stream_info.get("notify_only", False)
        # Vom Master anhand der tatsächlich verbauten GPU bestimmt (siehe
        # detect_gpu_profile) — nicht pro Worker neu geraten.
        self.half_precision_allowed = half_precision

        self.daemon = True
        self._stop_event = multiprocessing.Event()

    def run(self):
        """The primary execution loop for each camera process using PyAV and CUDA GPU."""
        try:
            from config import get_stream_logger as gs, YOLO_VERSION
            self.logger = gs(self.name)
        except Exception:
            import logging
            self.logger = logging.getLogger(self.name)
            YOLO_VERSION = "v10"  # Fallback

        # SIGTERM-Handler wird bewusst SPÄTER registriert (siehe kurz vor dem
        # Eintritt in den Hauptloop) — nicht hier, direkt am Anfang. Details dort.
        def _handle_signal(signum, frame):
            self._stop_event.set()
            raise GracefulShutdown()

        print(f"🚀 [Process Start] Initializing agent: {self.name} (Using YOLO {YOLO_VERSION})")

        try:
            import av
            import torch
            import numpy as np
            import cv2  # bereits Ultralytics-Abhängigkeit, für Trigger-Screenshots genutzt

            torch.set_num_threads(2)
            cv2.setNumThreads(2)  # sonst nutzt cv2 (Resize/JPEG-Encode für Thumbnails,
            # Filmstrip, Shared-Frames) unkontrolliert alle Kerne — pro Kamera-Prozess
            # echte CPU-Konkurrenz mit den bereits gedeckelten Torch/OMP-Threads.

            from ultralytics import YOLO
        except ImportError as e:
            self.logger.error(f"❌ Dependency Error in {self.name}: {e}")
            return

        # 1. Initialize AI Engine (YOLO v10, v12 or v26) auf CUDA GPU
        detector = None
        device_target = "cuda:0" if torch.cuda.is_available() else "cpu"
        half_enabled = False

        def _load_and_selftest(use_cudnn, use_half):
            """Lädt das Modell mit gegebenem cuDNN-/FP16-Zustand und führt einen
            winzigen Dummy-Inferenzlauf aus. Deckt cuDNN- oder FP16-
            Versionskonflikte sofort beim Start auf, statt erst mitten im
            Stream-Loop zu crashen."""
            torch.backends.cudnn.enabled = use_cudnn
            m = YOLO(MODEL_PATH)
            if device_target == "cuda:0":
                m.to("cuda:0")
            dummy = np.zeros((64, 64, 3), dtype=np.uint8)
            # 'half=' ist in aktuellen Ultralytics-Versionen deprecated
            # (Warnung: "Use 'quantize' instead") — quantize=16 ist das
            # Äquivalent für FP16. Nur mitgeben wenn gewünscht, statt
            # quantize=None zu raten.
            if use_half:
                m(dummy, verbose=False, device=device_target, quantize=16)
            else:
                m(dummy, verbose=False, device=device_target)
            return m

        # Fix 1: Reihenfolge war `os.path.exists(MODEL_PATH) and MODEL_PATH` —
        # os.path.exists(None) wirft TypeError, wenn MODEL_PATH mal None/leer
        # ist, BEVOR die and-Kurzschlussauswertung das prüfen kann. Erst auf
        # MODEL_PATH prüfen, dann erst exists() aufrufen.
        def _try_load_model():
            """Versucht das Modell zu laden — von voller Performance (cuDNN +
            FP16) stufenweise abwärts bis zu einer Kombination, die auf DIESER
            Hardware tatsächlich funktioniert. Geht über jede GPU-Generation
            von RTX 2060 bis RTX 5090 sicher, ohne dass man vorher wissen
            muss, welche Kombination auf der jeweiligen Maschine läuft."""
            if device_target != "cuda:0":
                # Reines CPU-Vision: kein cuDNN/FP16 relevant
                try:
                    m = YOLO(MODEL_PATH)
                    dummy = np.zeros((64, 64, 3), dtype=np.uint8)
                    m(dummy, verbose=False, device=device_target)
                    self.logger.warning("⚠️ CUDA not available, falling back to CPU.")
                    return m, False
                except Exception as e:
                    self.logger.error(f"❌ Failed to load model ({YOLO_VERSION}) on CPU: {e}")
                    return None, False

            attempts = []
            if not DISABLE_CUDNN:
                if self.half_precision_allowed:
                    attempts.append((True, True))
                attempts.append((True, False))
            if self.half_precision_allowed:
                attempts.append((False, True))
            attempts.append((False, False))

            last_error = None
            for use_cudnn, use_half in attempts:
                try:
                    m = _load_and_selftest(use_cudnn, use_half)
                    self.logger.info(
                        f"✅ AI Model (YOLO {YOLO_VERSION}) auf CUDA GPU geladen "
                        f"({torch.cuda.get_device_name(0)}), cuDNN={'aktiv' if use_cudnn else 'inaktiv'}, "
                        f"FP16={'aktiv' if use_half else 'inaktiv'}."
                    )
                    return m, use_half
                except Exception as e:
                    last_error = e
                    self.logger.warning(
                        f"⚠️ Selbsttest fehlgeschlagen mit cuDNN={'an' if use_cudnn else 'aus'}/"
                        f"FP16={'an' if use_half else 'aus'} ({e}) — versuche schwächere Kombination..."
                    )
                    if device_target == "cuda:0":
                        try:
                            torch.cuda.empty_cache()
                        except Exception:
                            pass

            self.logger.error(f"❌ Failed to load model ({YOLO_VERSION}) in jeder getesteten Kombination: {last_error}")
            return None, False

        if MODEL_PATH and os.path.exists(MODEL_PATH):
            detector, half_enabled = _try_load_model()
        else:
            self.logger.warning("⚠️ No valid YOLO path found; running in VISION-ONLY mode.")

        # Pose-Estimation (Sturz, Notsignal) und Loitering (Positions-
        # Tracking, kein Pose-Modell nötig) -- alle drei unabhängig
        # voneinander schaltbar. Pose-Modell läuft nur an, wenn mindestens
        # eine der beiden Pose-basierten Auswertungen aktiviert ist, UND
        # nur für Frames, in denen der Hauptdetektor bereits eine Person
        # gesehen hat (kein zusätzlicher GPU-Aufwand für leere Szenen).
        # Kein aufwändiger Selbsttest wie beim Hauptmodell -- schlägt das
        # Laden fehl, werden die Pose-Features einfach übersprungen, der
        # Rest der Pipeline läuft unverändert weiter.
        pose_cfg = _pose_estimation_settings()
        pose_detector = None
        fall_tracker = None
        raised_hands_tracker = None
        loitering_tracker = None
        gaze_tracker = None
        pointing_tracker = None
        movement_tracker = None
        proximity_tracker = None

        needs_pose_model = pose_cfg["fall_enabled"] or pose_cfg["raised_hands_enabled"] or pose_cfg["gaze_enabled"] or pose_cfg["pointing_enabled"]
        if needs_pose_model and pose_fall_detection is not None:
            try:
                from ultralytics import YOLO as _YOLO
                pose_detector = _YOLO("yolo11n-pose.pt")
                if pose_cfg["fall_enabled"]:
                    fall_tracker = pose_fall_detection.FallTracker(required_consecutive=5)
                if pose_cfg["raised_hands_enabled"]:
                    raised_hands_tracker = pose_fall_detection.RaisedHandsTracker(required_consecutive=3)
                if pose_cfg["gaze_enabled"]:
                    gaze_tracker = pose_fall_detection.GenericEventTracker(required_consecutive=3)
                if pose_cfg["pointing_enabled"]:
                    pointing_tracker = pose_fall_detection.GenericEventTracker(required_consecutive=3)
                self.logger.info(
                    f"🧍 [{self.name}] Pose estimation aktiviert "
                    f"(Sturz: {'an' if pose_cfg['fall_enabled'] else 'aus'}, "
                    f"Notsignal: {'an' if pose_cfg['raised_hands_enabled'] else 'aus'}, "
                    f"Blickrichtung: {'an' if pose_cfg['gaze_enabled'] else 'aus'}, "
                    f"Zeigen: {'an' if pose_cfg['pointing_enabled'] else 'aus'})."
                )
            except Exception as e:
                self.logger.warning(f"⚠️ [{self.name}] Pose-Modell konnte nicht geladen werden, alle Pose-basierten Auswertungen bleiben aus: {e}")

        if pose_cfg["loitering_enabled"] and loitering_detection is not None:
            loitering_tracker = loitering_detection.LoiteringTracker(min_duration_sec=pose_cfg["loitering_seconds"])
            self.logger.info(f"🚶 [{self.name}] Loitering-Erkennung aktiviert ({pose_cfg['loitering_seconds']}s Schwellwert).")
        if pose_cfg["movement_enabled"] and loitering_detection is not None:
            movement_tracker = loitering_detection.MovementTracker()
            self.logger.info(f"🏃 [{self.name}] Bewegungsgeschwindigkeits-Erkennung aktiviert.")
        if pose_cfg["proximity_enabled"] and loitering_detection is not None:
            proximity_tracker = loitering_detection.ProximityTracker(required_consecutive=5)
            self.logger.info(f"👥 [{self.name}] Personen-Nähe-Erkennung aktiviert.")

        # YOLO läuft in eigenem Thread statt synchron im Encode-Loop -- verzögerte sonst das Encoding bei langsamer Inferenz. Nur boxes/names geteilt, nie das rohe Ultralytics-Objekt.
        _detection_lock = threading.Lock()
        _latest_detection = {
            'boxes': None, 'names': None,
            'fall_confirmed': False, 'fall_info': None,
            'raised_hands_confirmed': False, 'raised_hands_info': None,
            'gaze_changed': None, 'pointing_changed': None,
            'running_detected': False, 'proximity_confirmed': False,
        }
        _pending_frame_lock = threading.Lock()
        _pending_frame = {'img': None}
        _frame_ready_event = threading.Event()
        _detector_stop_event = threading.Event()

        def _run_pose_check(img, boxes, names):
            """Läuft nur, wenn mind. eine 'person'-Box im Hauptergebnis war --
            spart die Pose-Inferenz komplett für leere/objektlose Frames.
            Nimmt die Person mit der höchsten Detection-Confidence, falls
            mehrere im Bild sind. Berechnet ALLE aktivierten Pose-Auswertungen
            aus DENSELBEN Keypoints -- keine zusätzliche Inferenz pro
            Auswertung, nur unterschiedliche Interpretationen derselben
            Daten. Gibt (fall_info, raised_hands_info, gaze_info,
            pointing_info) zurück, None an der jeweiligen Stelle, wo die
            Auswertung nicht aktiviert ist."""
            person_class_ids = {cid for cid, n in names.items() if n == "person"}
            person_boxes = [b for b in boxes if int(b[5]) in person_class_ids]
            if not person_boxes:
                for tracker in (fall_tracker, raised_hands_tracker, gaze_tracker, pointing_tracker):
                    if tracker is not None:
                        tracker.reset()  # keine Person mehr im Bild -- Zustand darf nicht "hängen bleiben"
                return None, None, None, None
            try:
                pose_results = pose_detector(img, verbose=False, device=device_target)
                if pose_results[0].keypoints is None or len(pose_results[0].keypoints.data) == 0:
                    return None, None, None, None
                # Beste Person nach Detection-Confidence auswählen. Keypoints-
                # Reihenfolge entspricht der Reihenfolge der erkannten Personen
                # im selben results-Objekt -- nutzt hier einfach die erste
                # erkannte Person-Pose als Näherung, da eine exakte Box-zu-
                # Keypoints-Zuordnung über zwei getrennte Modell-Aufrufe
                # (Haupt- vs. Pose-Modell) ohnehin nicht exakt ist -- für
                # Ein-Personen-Szenen (der typische Zuhause-Fall) macht das
                # keinen Unterschied.
                best_person = max(person_boxes, key=lambda b: b[4])
                keypoints = pose_results[0].keypoints.data[0].cpu().numpy()
                bbox = tuple(best_person[:4])
                fall_info = pose_fall_detection.detect_fall(keypoints, bbox=bbox, angle_threshold=pose_cfg["fall_angle_threshold"]) \
                    if fall_tracker is not None else None
                raised_hands_info = pose_fall_detection.detect_raised_hands(keypoints) \
                    if raised_hands_tracker is not None else None
                gaze_info = pose_fall_detection.detect_head_orientation(keypoints) \
                    if gaze_tracker is not None else None
                pointing_info = pose_fall_detection.detect_pointing(keypoints) \
                    if pointing_tracker is not None else None
                return fall_info, raised_hands_info, gaze_info, pointing_info
            except Exception as e:
                self.logger.error(f"❌ [{self.name}] Fehler in der Pose-Inferenz: {e}")
                return None, None, None, None

        def _run_position_checks(boxes, names, frame_width, frame_height):
            """Loitering, Bewegungsgeschwindigkeit und Personen-Nähe --
            alle drei unabhängig vom Pose-Modell, nutzen nur die normalen
            Detection-Boxen. Bei mehreren Personen wird für Loitering/
            Bewegung einfach die erste verfolgt (Ein-Personen-Fokus, siehe
            LoiteringTracker-Docstring); Nähe prüft dagegen bewusst ALLE
            Paare, dafür braucht es ja mindestens zwei Personen."""
            person_class_ids = {cid for cid, n in names.items() if n == "person"}
            person_boxes = [b for b in boxes if int(b[5]) in person_class_ids]
            now = time.time()

            loitering_confirmed = False
            movement_state = None
            if not person_boxes:
                if loitering_tracker is not None:
                    loitering_tracker.update(None, frame_width, frame_height, now)
                if movement_tracker is not None:
                    movement_tracker.reset()
            else:
                chosen = person_boxes[0]
                center = loitering_detection.box_center(tuple(chosen[:4]))
                if loitering_tracker is not None:
                    loitering_confirmed = bool(loitering_tracker.update(center, frame_width, frame_height, now))
                if movement_tracker is not None:
                    bbox_height = chosen[3] - chosen[1]
                    movement_state = movement_tracker.update(center, bbox_height, now)

            proximity_confirmed = False
            if proximity_tracker is not None:
                is_close = loitering_detection.detect_close_proximity(person_boxes)
                proximity_confirmed = bool(proximity_tracker.update(is_close))

            return loitering_confirmed, movement_state, proximity_confirmed

        def _detection_worker():
            while not _detector_stop_event.is_set():
                if not _frame_ready_event.wait(timeout=1.0):
                    continue
                _frame_ready_event.clear()
                with _pending_frame_lock:
                    img = _pending_frame['img']
                    _pending_frame['img'] = None
                if img is None or detector is None:
                    continue
                try:
                    if half_enabled:
                        results = detector(img, verbose=False, classes=DETECTION_CLASSES, conf=CONFIDENCE_THRESHOLD, device=device_target, quantize=16)
                    else:
                        results = detector(img, verbose=False, classes=DETECTION_CLASSES, conf=CONFIDENCE_THRESHOLD, device=device_target)
                    boxes = results[0].boxes.data.cpu().numpy().copy()
                    names = dict(results[0].names)
                except Exception as det_exc:
                    self.logger.error(f"❌ [{self.name}] Fehler in der Erkennungs-Inferenz: {det_exc}")
                    continue

                fall_confirmed = False
                fall_info = None
                raised_hands_confirmed = False
                raised_hands_info = None
                gaze_changed = None
                pointing_changed = None
                if pose_detector is not None:
                    fall_info, raised_hands_info, gaze_info, pointing_info = _run_pose_check(img, boxes, names)
                    if fall_info is not None and fall_tracker is not None:
                        fall_confirmed = fall_tracker.update(fall_info)
                        if fall_confirmed:
                            self.logger.warning(f"🚨 [{self.name}] Möglicher Sturz erkannt ({fall_info['reason']})")
                    if raised_hands_info is not None and raised_hands_tracker is not None:
                        raised_hands_confirmed = raised_hands_tracker.update(raised_hands_info)
                        if raised_hands_confirmed:
                            self.logger.warning(f"🙌 [{self.name}] Mögliches Notsignal erkannt ({raised_hands_info['reason']})")
                    if gaze_info is not None and gaze_tracker is not None:
                        gaze_changed = gaze_tracker.update(gaze_info["orientation"])
                        if gaze_changed:
                            self.logger.info(f"👀 [{self.name}] Blickrichtung geändert: {gaze_changed}")
                    if pointing_info is not None and pointing_tracker is not None:
                        pointing_changed = pointing_tracker.update(pointing_info["pointing"])
                        if pointing_changed:
                            self.logger.info(f"👉 [{self.name}] Zeige-Geste erkannt ({pointing_info.get('arm')})")

                loitering_confirmed, movement_state, proximity_confirmed = False, None, False
                if loitering_tracker is not None or movement_tracker is not None or proximity_tracker is not None:
                    frame_height, frame_width = img.shape[:2]
                    loitering_confirmed, movement_state, proximity_confirmed = _run_position_checks(boxes, names, frame_width, frame_height)
                    if loitering_confirmed:
                        self.logger.warning(f"🚶 [{self.name}] Herumlungern erkannt (Person bleibt seit {pose_cfg['loitering_seconds']}s+ an derselben Stelle).")
                        _notify_pose_event(
                            "loitering_detected", self.name,
                            f"A person has stayed in roughly the same spot for over {int(pose_cfg['loitering_seconds'])}s."
                        )
                    if movement_state == "running":
                        self.logger.warning(f"🏃 [{self.name}] Schnelle Bewegung erkannt (möglicherweise rennend).")
                        _notify_pose_event("running_detected", self.name, "A person appears to be moving quickly/running.")
                    if proximity_confirmed:
                        self.logger.warning(f"👥 [{self.name}] Anhaltende Personen-Nähe erkannt.")
                        _notify_pose_event("proximity_detected", self.name, "Two or more people have been in close proximity for a sustained period.")

                with _detection_lock:
                    _latest_detection['boxes'] = boxes
                    _latest_detection['names'] = names
                    _latest_detection['fall_confirmed'] = fall_confirmed
                    _latest_detection['fall_info'] = fall_info
                    _latest_detection['raised_hands_confirmed'] = raised_hands_confirmed
                    _latest_detection['raised_hands_info'] = raised_hands_info
                    _latest_detection['gaze_changed'] = gaze_changed
                    _latest_detection['pointing_changed'] = pointing_changed
                    _latest_detection['running_detected'] = (movement_state == "running")
                    _latest_detection['proximity_confirmed'] = proximity_confirmed

        _detection_thread = threading.Thread(target=_detection_worker, daemon=True)
        _detection_thread.start()

        # Echter NVENC-Test beim Start -- add_stream() allein prüft nicht zuverlässig, echte Fehler tauchen sonst erst mitten in der Aufnahme auf.
        def _probe_nvenc():
            try:
                import tempfile
                # 320x240, nicht 64x64: manche NVENC-Generationen/Treiber
                # lehnen zu kleine Auflösungen ab (unterhalb einer nicht
                # überall gleich dokumentierten Mindestgröße), was den Test
                # fälschlich als "NVENC kaputt" melden würde, obwohl der
                # Encoder bei den tatsächlichen Aufnahme-Auflösungen (720p/
                # 1080p) einwandfrei funktionieren könnte.
                with tempfile.NamedTemporaryFile(suffix='.mp4', delete=True) as tmp:
                    probe_container = av.open(tmp.name, mode='w')
                    probe_stream = probe_container.add_stream('h264_nvenc', rate=30)
                    probe_stream.width = 320
                    probe_stream.height = 240
                    probe_stream.pix_fmt = 'yuv420p'
                    probe_frame = av.VideoFrame(width=320, height=240, format='yuv420p')
                    list(probe_stream.encode(probe_frame))
                    list(probe_stream.encode(None))  # flush
                    probe_container.close()
                return True
            except Exception as e:
                self.logger.warning(f"⚠️ [{self.name}] NVENC-Test fehlgeschlagen, nutze libx264 (CPU-Encoding) für diese Kamera: {e}")
                return False

        nvenc_available = _probe_nvenc()
        if nvenc_available:
            self.logger.info(f"🎮 [{self.name}] NVENC-Test erfolgreich — Aufnahmen nutzen GPU-Encoding.")

        # Optionaler Audio-Trigger (CLAP, mehrere frei wählbare Kategorien
        # gleichzeitig). Läuft immer als Hintergrund-Thread, prüft aber selbst
        # laufend AUDIO_TRIGGER_ENABLED aus den Settings — so greift Ein/Aus
        # und Kategorien-Änderung live, ohne Pipeline-Neustart. Lädt das
        # eigentliche Modell erst lazy beim ersten Aktivieren.
        audio_trigger = None
        if AudioTrigger is not None and self.audio_enabled:
            try:
                audio_trigger = AudioTrigger(self.logger, self.name)
                audio_trigger.start(lambda: _load_settings_dict())
            except Exception as e:
                self.logger.warning(f"⚠️ [{self.name}] Audio-Trigger konnte nicht gestartet werden: {e}")
                audio_trigger = None
        elif AudioTrigger is not None and not self.audio_enabled:
            self.logger.info(f"🔇 [{self.name}] Audio-Erkennung für diese Kamera deaktiviert.")

        # Fix 5: Gemeinsamer Pre-Roll Puffer für Video und Audio, jetzt primär
        # nach Timestamp statt nach geschätzter Item-Anzahl getrimmt. Die alte
        # Formel `(TARGET_FPS + 50) * PRE_ROLL_SEC` war nur eine Annahme über
        # die Audio-Frame-Rate — kam mehr Audio rein als angenommen, flogen
        # Video-Frames früher raus als der eingestellte PRE_ROLL_SEC vorsah,
        # ohne dass das irgendwo sichtbar wurde. maxlen bleibt als grobzügiges
        # Sicherheitsnetz gegen Speicher-Runaway, trimmt aber nicht mehr aktiv.
        safety_cap = int((TARGET_FPS + 60) * PRE_ROLL_SEC) * 3
        av_buffer = deque(maxlen=safety_cap)
        # Encoding läuft über eine Warteschlange, verteilt über mehrere Loop-Durchläufe abgearbeitet -- vermeidet einen blockierenden Pre-Roll-Burst. Ein Thread bewusst, PyAV-Encoder sind nicht thread-sicher.
        pending_encode_queue = deque()

        def trim_buffer():
            cutoff = time.time() - PRE_ROLL_SEC
            while av_buffer and av_buffer[0][2] < cutoff:
                av_buffer.popleft()

        # Sicherheitsnetz für das wichtigste Kriterium: die Pipeline MUSS
        # aufzeichnen, sobald ein Trigger erkannt wird. Konnte das Modell beim
        # Start nicht geladen werden (z.B. kurzzeitiger GPU/Treiber-Hänger),
        # würde ohne diesen Retry NIE wieder etwas erkannt — der Stream liefe
        # bis zum manuellen Neustart nur im "VISION-ONLY"-Blindflug weiter.
        MODEL_RETRY_INTERVAL = 60  # Sekunden zwischen Nachlade-Versuchen
        last_model_retry = time.time()

        # Frame-Drosselung: überzählige Quell-Frames werden vor der teuren BGR-Konvertierung übersprungen, damit YOLO nicht öfter läuft als TARGET_FPS nötig.
        frame_interval = 1.0 / TARGET_FPS if TARGET_FPS and TARGET_FPS > 0 else 0.0
        last_processed_time = 0.0

        # Geteilter Live-Frame für web_ui.py/helpers.py: schreibt periodisch (Rate
        # aus THUMBNAIL_FPS) ein JPEG, damit die Web-UI NICHT mehr selbst eine
        # zweite RTMP-Verbindung pro Kamera aufmachen und decodieren muss.
        FRAMES_DIR = os.path.join(ALERTS_DIR, '.frames')
        os.makedirs(FRAMES_DIR, exist_ok=True)
        shared_frame_next_time = 0
        shared_frame_interval = 1.0
        show_boxes_live = True

        def _shared_frame_settings_watcher():
            """Liest die Settings-Datei alle 5s in einem eigenen Thread —
            der Hauptloop selbst macht dafür KEINE Datei-I/O mehr, auch
            keine seltene. Einfache Zuweisungen an nonlocal-Variablen sind
            unter der GIL atomar genug für diesen Fall (kein mehrstufiger
            Zustand, der zwischen Lese- und Schreibzugriff inkonsistent
            werden könnte)."""
            nonlocal shared_frame_interval, show_boxes_live
            while not _detector_stop_event.is_set():
                try:
                    with open(SETTINGS_F) as f:
                        d = json.load(f)
                    fps = float(d.get('THUMBNAIL_FPS', 1.0))
                    shared_frame_interval = 1.0 / fps if fps > 0 else 1.0
                    show_boxes_live = bool(d.get('SHOW_DETECTION_BOXES', True))
                except Exception:
                    pass
                _detector_stop_event.wait(timeout=5.0)

        threading.Thread(target=_shared_frame_settings_watcher, daemon=True).start()

        def write_shared_frame(img_bgr, boxes=None, names=None):
            nonlocal shared_frame_next_time
            now2 = time.time()
            if now2 < shared_frame_next_time:
                return
            try:
                if not show_boxes_live:
                    boxes = None
                    names = None
                # NUR ein günstiger Array-Copy hier im Hauptloop — Annotieren,
                # Resize, JPEG-Encoding und der eigentliche Schreibvorgang
                # laufen jetzt komplett im Hintergrund-Thread.
                _shared_frame_write_queue.put((self.name, img_bgr.copy(), boxes, names))
                shared_frame_next_time = now2 + shared_frame_interval
            except Exception:
                pass

        state = "IDLE"
        post_roll_end_time = 0
        container = None
        out_container = None
        out_video = None
        out_audio = None
        recording_start_time = 0
        pts_offset = None  # None = "noch nicht gesetzt", wird lazy beim ersten Paket der Aufnahme berechnet
        video_frame_count = 0  # nur im Encode-Modus genutzt (MJPEG/USB/HEVC-Kameras)
        last_pts = -1  # nur im Encode-Modus genutzt

        # Filmstrip (Hover-Scrub-Vorschau + AI-taugliche Großbilder): pro
        # Recording neu gesetzt, siehe RECORDING-Start weiter unten.
        fs_small_dir = None
        fs_large_dir = None
        filmstrip_count_target = 0
        filmstrip_interval = 2.0
        filmstrip_taken_total = 0   # ALLE seit Recording-Start gesehenen Kandidaten (fürs Reservoir Sampling)
        filmstrip_timestamps = {}  # slot_idx (str) -> Sekunden seit Recording-Start, für korrekte Zeitreihenfolge trotz Slot-Überschreibung
        filmstrip_next_time = 0
        # slot_idx (int) -> (roher Frame-Copy, Box-Koordinaten oder None, rel_ts).
        # Während der Aufnahme wird hier NUR reingeschrieben (reiner Array-Copy,
        # kein Resize/Annotieren/Schreiben) — die eigentliche teurere Arbeit
        # läuft erst in flush_filmstrip() bei Aufnahmeende, wenn Zeit keine
        # Rolle mehr spielt. Durch FILMSTRIP_COUNT natürlich nach oben
        # begrenzt, kein unbegrenztes Wachstum bei langen Aufnahmen.
        filmstrip_pending = {}

        def close_writer():
            nonlocal out_container, out_video, out_audio, pts_offset, video_frame_count, last_pts
            if out_container:
                try:
                    # Restliche Warteschlange komplett abarbeiten — sonst
                    # gingen evtl. noch nicht gemuxte/encodierte Pakete
                    # (Pre-Roll-Rest, oder normale Pakete kurz vor
                    # Aufnahmeende) beim Schließen verloren.
                    _drain_encode_queue_fully()

                    # Encoder-Flush nur im Encode-Modus nötig -- NVENC/libx264
                    # haben internes Lookahead-Buffering, das explizit mit
                    # encode(None) geleert werden muss, sonst gehen die letzten
                    # paar Frames verloren. Packet-Copy hat kein solches
                    # Buffering (jedes Paket wird sofort gemuxt).
                    if recording_mode == "encode" and out_video:
                        try:
                            for packet in out_video.encode(None):
                                out_container.mux(packet)
                        except Exception as e:
                            self.logger.warning(f"⚠️ [{self.name}] Encoder-Flush-Fehler: {e}")

                    out_container.close()
                except Exception as e:
                    self.logger.error(f"❌ Error closing output file: {e}")
                finally:
                    flush_filmstrip()
                    try:
                        marker = os.path.splitext(video_file_path)[0] + '.recording'
                        if os.path.exists(marker):
                            os.remove(marker)
                    except Exception:
                        pass
                    out_container = None
                    out_video = None
                    out_audio = None
                    pts_offset = None
                    video_frame_count = 0
                    last_pts = -1
                    av_buffer.clear()
                    pending_encode_queue.clear()

        def remux_packet(packet, ts=None):
            """Ersetzt encode_video_frame/encode_audio_frame komplett — kein
            Neu-Encodieren mehr, das Paket ist ja schon komprimiert. Nur PTS/
            DTS um pts_offset verschieben und direkt muxen. ts wird nicht mehr
            für die PTS-Berechnung gebraucht (die Quelle liefert ihre eigenen,
            echten Zeitstempel) — Parameter bleibt nur der Kompatibilität mit
            write_buffered_item()/der Warteschlange wegen erhalten.

            pts_offset wird LAZY beim ersten tatsächlich verarbeiteten Paket
            dieser Aufnahme gesetzt (nicht vorab am Trigger-Zeitpunkt) — war
            der Pre-Roll-Puffer beim Trigger leer (PRE_ROLL_SEC=0, oder ein
            Trigger direkt nach Verbindungsaufbau, bevor sich der Puffer
            füllen konnte), hätte eine vorab-berechnete Offset sonst bei 0
            hängenbleiben, während das erste ECHTE Paket einen riesigen PTS-
            Wert trägt (die Quelle zählt seit Verbindungsaufbau, nicht seit
            Aufnahmestart) — die Datei hätte dann falsch riesig begonnen."""
            nonlocal pts_offset
            if not out_container:
                return
            target_stream = out_video if packet.stream.type == 'video' else out_audio
            if not target_stream:
                return
            try:
                if packet.dts is None:
                    return
                if pts_offset is None:
                    pts_offset = packet.dts
                packet.stream = target_stream
                packet.pts -= pts_offset
                packet.dts -= pts_offset
                out_container.mux(packet)
            except Exception as e:
                self.logger.error(f"❌ Remux error ({packet.stream.type if packet.stream else '?'}): {e}")

        def _finish_recording_now(reason):
            """Beendet die aktuell laufende Aufnahme sofort und stößt die
            Nachbearbeitung an -- gemeinsam genutzt vom normalen Post-Roll-
            Timeout UND vom manuellen Stop (Agent/API), damit beide Wege
            exakt denselben, bereits bewährten Abschluss-Code durchlaufen
            statt ihn zu duplizieren."""
            nonlocal state
            self.logger.info(f"✅ Session ended for {self.name} ({reason}). Closing file.")
            close_writer()
            state = "IDLE"
            _write_state(self.name, "IDLE")
            _publish_mqtt_recording(self.name, False)
            if _postprocessing_enabled():
                try:
                    vb = os.path.splitext(os.path.basename(video_file_path))[0]
                    subprocess.Popen(
                        [sys.executable, os.path.join(DIR, 'postprocess.py'), vb, ALERTS_DIR]
                    )
                except Exception as e:
                    self.logger.warning(f"⚠️ [{self.name}] Konnte Nachbearbeitung nicht starten: {e}")

        def capture_filmstrip(img_bgr, boxes=None, names=None):
            """Wählt im Intervall einen Filmstrip-Slot aus und legt NUR einen
            günstigen Roh-Frame-Copy + Box-Koordinaten dafür beiseite —
            Resize, Box-Annotation und das eigentliche Schreiben passieren
            NICHT hier, sondern erst nachträglich in flush_filmstrip() bei
            Aufnahmeende (siehe close_writer()). Ein reiner Array-Copy kostet
            unter einer Millisekunde; Resize+Annotieren+JPEG-Encoding+Disk-I/O
            zusammen können ein Vielfaches davon sein — und genau das sollte
            nie im zeitkritischen Aufnahme-Loop passieren, selbst nicht in
            einem Hintergrund-Thread (der nimmt nur die Disk-I/O ab, nicht
            die CPU-Arbeit fürs Resize/Annotieren).

            Hybrid aus Reservoir Sampling + garantiertem Ende-Slot:
            - Der LETZTE Slot wird bei JEDEM Aufruf überschrieben — zeigt also
              immer den zuletzt aufgenommenen Frame. Garantiert, dass das Ende
              einer Aktion nie fehlt, egal wie lange sie dauert.
            - Die übrigen Slots nutzen Reservoir Sampling (Algorithm R): jeder
              Kandidat hat eine mit der Zeit abnehmende Chance, einen davon zu
              ersetzen — Ergebnis: gleichmäßige Verteilung über die gesamte
              bisherige Dauer, egal ob 10 Sekunden oder 30 Minuten.
            Reines Reservoir Sampling allein GARANTIERT die Ende-Abdeckung nicht
            (nur im statistischen Mittel) — deshalb der feste Ende-Slot zusätzlich.
            """
            nonlocal filmstrip_taken_total, filmstrip_next_time
            if not fs_small_dir or filmstrip_count_target <= 0:
                return
            now = time.time()
            if now < filmstrip_next_time:
                return
            try:
                reservoir_size = filmstrip_count_target - 1 if filmstrip_count_target >= 2 else filmstrip_count_target
                end_slot = filmstrip_count_target - 1 if filmstrip_count_target >= 2 else None

                filmstrip_taken_total += 1
                slot = None
                if reservoir_size > 0:
                    if filmstrip_taken_total <= reservoir_size:
                        slot = filmstrip_taken_total - 1
                    else:
                        j = random.randint(0, filmstrip_taken_total - 1)
                        if j < reservoir_size:
                            slot = j

                slots_to_fill = set()
                if slot is not None:
                    slots_to_fill.add(slot)
                if end_slot is not None:
                    slots_to_fill.add(end_slot)

                if slots_to_fill:
                    frame_copy = img_bgr.copy()
                    rel_ts = round(now - recording_start_time, 2)
                    for s in slots_to_fill:
                        filmstrip_pending[s] = (frame_copy, boxes, names, rel_ts)

                filmstrip_next_time = now + filmstrip_interval
            except Exception:
                pass

        def flush_filmstrip():
            """Läuft einmalig bei Aufnahmeende (siehe close_writer()) — hier
            passiert die eigentliche, vorher im Hauptloop laufende
            Resize/Annotations-Arbeit, plus das Einreihen der tatsächlichen
            Schreibvorgänge in die Hintergrund-Queue. Zeit spielt hier keine
            Rolle mehr: die Aufnahme ist zu diesem Zeitpunkt schon fertig
            encodiert, ein paar zusätzliche Millisekunden hier beeinflussen
            die Video-Glätte in keiner Weise."""
            if not fs_small_dir or not filmstrip_pending:
                return
            try:
                for s, (frame, boxes, names, rel_ts) in filmstrip_pending.items():
                    h, w = frame.shape[:2]
                    annotated = frame
                    if boxes is not None and len(boxes) > 0:
                        try:
                            annotated = frame.copy()
                            _draw_boxes_with_labels(cv2, annotated, boxes, names)
                        except Exception:
                            annotated = frame
                    small_full = cv2.resize(annotated, (560, max(1, int(h * 560 / w))))
                    large_full = frame if w <= 1280 else cv2.resize(frame, (1280, max(1, int(h * 1280 / w))))

                    _filmstrip_write_queue.put(('jpg', os.path.join(fs_small_dir, f'{s:04d}.jpg'), small_full))
                    _filmstrip_write_queue.put(('jpg', os.path.join(fs_large_dir, f'{s:04d}.jpg'), large_full))
                    filmstrip_timestamps[str(s)] = rel_ts

                ts_path = os.path.join(os.path.dirname(fs_small_dir), 'timestamps.json')
                _filmstrip_write_queue.put(('json', ts_path, filmstrip_timestamps.copy()))
                filmstrip_pending.clear()
            except Exception:
                pass


        def encode_video_frame(img_bgr, ts=None):
            """Nur im Encode-Modus genutzt (Kamera liefert keinen browser-
            kompatiblen Codec) — echtes Encoding statt Packet-Copy, bringt das
            bewährte Wall-Clock-PTS-Verhalten von vor dem Packet-Copy-Umbau
            zurück, aber isoliert auf genau die Kameras beschränkt, die es
            wirklich brauchen."""
            nonlocal video_frame_count, last_pts
            if not out_container or not out_video:
                return
            try:
                t = ts if ts is not None else time.time()
                elapsed = max(0.0, t - recording_start_time)
                pts = int(elapsed * TARGET_FPS)
                if pts <= last_pts:
                    pts = last_pts + 1
                last_pts = pts
                av_frame = av.VideoFrame.from_ndarray(img_bgr, format="bgr24")
                av_frame.pts = pts
                video_frame_count += 1
                for packet in out_video.encode(av_frame):
                    out_container.mux(packet)
            except Exception as e:
                self.logger.error(f"❌ [{self.name}] Video-Encoding-Fehler: {e}")

        def write_buffered_item(item_type, data, ts=None):
            if item_type == "video" and recording_mode == "encode":
                encode_video_frame(data, ts)
            else:
                remux_packet(data, ts)

        def _drain_encode_queue(max_items=8):
            """Arbeitet höchstens max_items aus der Warteschlange ab, statt
            alles auf einmal — verteilt einen Pre-Roll-Burst über mehrere
            Loop-Durchläufe, damit der Decode-Loop dazwischen immer wieder
            neue Quell-Pakete lesen kann, statt für den kompletten Burst zu
            pausieren."""
            n = 0
            while pending_encode_queue and n < max_items:
                item_type, data, ts = pending_encode_queue.popleft()
                write_buffered_item(item_type, data, ts)
                n += 1

        def _drain_encode_queue_fully():
            """Restlos abarbeiten, ohne Obergrenze — für close_writer(), damit
            beim Beenden einer Aufnahme garantiert nichts verloren geht, egal
            wie viel noch in der Warteschlange steht."""
            while pending_encode_queue:
                item_type, data, ts = pending_encode_queue.popleft()
                write_buffered_item(item_type, data, ts)

        # NVDEC-HWAccel-Objekt einmal pro Prozess konstruiert; schlägt nur ein Verbindungsversuch fehl, bleibt hw_device erhalten und wird beim nächsten Reconnect erneut probiert.
        hw_device = None
        try:
            from av.codec.hwaccel import HWAccel
            hw_device = HWAccel(device_type='cuda', device='0', allow_software_fallback=True)
            self.logger.info(f"🎮 [{self.name}] NVDEC-Hardware-Decode wird versucht.")
        except Exception as e:
            self.logger.info(f"ℹ️ [{self.name}] NVDEC nicht verfügbar ({e}) — nutze Software-Decoding (PyAV-Version prüfen für Hardware-Decode).")
            hw_device = None

        # Zählt NVDEC-Fehlversuche IN FOLGE (ohne zwischenzeitlichen Erfolg).
        # Kamera kurz weg -> ein paar Fehlversuche, dann klappt's wieder ->
        # Zähler wird zurückgesetzt, NVDEC bleibt aktiv. Erst wenn NVDEC
        # mehrfach hintereinander NIE erfolgreich verbindet, deutet das auf
        # ein grundsätzliches Problem hin (nicht auf eine flackernde Cam) —
        # dann erst dauerhaft abschalten, um nicht endlos sinnlos zu retryen.
        nvdec_fail_streak = 0
        NVDEC_FAIL_THRESHOLD = 5
        using_nvdec = False
        recording_mode = "copy"  # Standardannahme, wird nach jedem (Re-)Connect neu bestimmt

        def _build_open_options(url):
            """Baut protokoll-passende ffmpeg-Optionen. rtmp_live war bisher
            unconditional gesetzt, unabhängig vom tatsächlichen Protokoll —
            ffmpeg ignoriert protokollfremde Optionen zwar meist
            stillschweigend, aber sauber ist anders, und RTSP braucht eigene,
            sinnvolle Optionen statt gar keine."""
            scheme = url.split("://", 1)[0].lower() if "://" in url else ""
            if scheme == "rtsp":
                return {
                    # TCP statt des ffmpeg-Standards UDP -- robuster gegen
                    # Paketverlust, der bei UDP zu sichtbaren Bildfehlern
                    # führen würde, kostet dafür etwas Latenz. Für ein
                    # Aufnahme-/Erkennungssystem (kein Live-Gaming) klar die
                    # richtige Abwägung.
                    "rtsp_transport": "tcp",
                    "rw_timeout": "5000000",
                }
            elif scheme == "rtmp":
                return {"rtmp_live": "live", "rw_timeout": "5000000"}
            elif url.startswith("/dev/video"):
                # USB-Webcam über V4L2 -- framerate/Auflösung optional
                # anforderbar, ffmpeg fällt sonst auf das zurück, was die
                # Kamera als Standard liefert.
                return {"framerate": str(TARGET_FPS)}
            else:
                # http(s) (z.B. MJPEG) und alles andere -- generische,
                # protokoll-neutrale Option.
                return {"rw_timeout": "5000000"}

        def _build_input_format(url):
            """Ein reiner Gerätepfad wie /dev/video0 (USB-Webcam über V4L2)
            hat kein Protokoll-Präfix, aus dem ffmpeg das Format selbst
            erkennen könnte (anders als bei rtmp://, rtsp://, http://) --
            muss hier explizit angegeben werden. Eine Plattform-Brücken-FIFO
            (siehe platform_bridge.py) ebenfalls: eine Named Pipe ohne
            Dateiendungs-Erkennung braucht die explizite Format-Angabe,
            sonst kann ffmpeg nicht wissen, dass dort MPEG-TS ankommt.
            None = ffmpeg soll selbst erkennen (alle anderen Quellen,
            unverändertes Verhalten)."""
            if url.startswith("/dev/video"):
                return "v4l2"
            if url.endswith(".fifo"):
                return "mpegts"
            return None

        input_format = _build_input_format(self.url)

        try:
            while not self._stop_event.is_set():
                if container is None:
                    if self._original_platform_url is not None:
                        # Brücken-Prozess läuft dauerhaft im Hintergrund und
                        # kümmert sich SELBST um Reconnect/Offline-Kanäle --
                        # hier nur sicherstellen, dass er läuft, nicht bei
                        # jedem Verbindungsversuch neu starten.
                        if self._platform_bridge is None or not self._platform_bridge.is_alive:
                            self.logger.info(f"🌉 [{self.name}] Starte Plattform-Brücke für {self._original_platform_url}")
                            os.makedirs(os.path.dirname(self.url), exist_ok=True)
                            self._platform_bridge = platform_bridge.PlatformStreamBridge(self._original_platform_url, self.url)
                            self._platform_bridge.start()
                            time.sleep(2)  # der Brücke kurz Zeit geben, die FIFO tatsächlich anzulegen, bevor av.open() sie zu öffnen versucht
                    self.logger.info(f"🔗 Attempting connection to: {self.url}")
                    open_options = _build_open_options(self.url)
                    using_nvdec = False
                    try:
                        if hw_device is not None:
                            container = av.open(self.url, options=open_options, hwaccel=hw_device, format=input_format)
                            using_nvdec = True
                            nvdec_fail_streak = 0
                            self.logger.info(f"✅ [CONNECTED] '{self.name}' via NVDEC established stream at {self.url}")
                        else:
                            container = av.open(self.url, options=open_options, format=input_format)
                            self.logger.info(f"✅ [CONNECTED] '{self.name}' established stream at {self.url} (Software-Decode)")
                    except Exception as e:
                        if hw_device is not None:
                            nvdec_fail_streak += 1
                            if nvdec_fail_streak >= NVDEC_FAIL_THRESHOLD:
                                self.logger.warning(
                                    f"⚠️ [{self.name}] NVDEC ist {nvdec_fail_streak}x in Folge ohne jeden Erfolg "
                                    f"fehlgeschlagen ({e}) — deaktiviere Hardware-Decode dauerhaft für diesen Prozess."
                                )
                                hw_device = None
                            else:
                                self.logger.warning(
                                    f"⚠️ [{self.name}] NVDEC-Verbindung fehlgeschlagen ({e}, {nvdec_fail_streak}/{NVDEC_FAIL_THRESHOLD}) — "
                                    f"versuche diesen Versuch mit Software-Decode, NVDEC wird beim nächsten Reconnect erneut probiert."
                                )
                            try:
                                container = av.open(self.url, options=open_options, format=input_format)
                                self.logger.info(f"✅ [CONNECTED] '{self.name}' established stream at {self.url} (Software-Decode, NVDEC-Fallback)")
                            except Exception as e2:
                                self.logger.error(f"❌ [CONNECTION FAILED] '{self.name}': {e2}. Retrying in 5s...")
                                container = None
                                time.sleep(5)
                                continue
                        else:
                            self.logger.error(f"❌ [CONNECTION FAILED] '{self.name}': {e}. Retrying in 5s...")
                            container = None
                            time.sleep(5)
                            continue

                    # Quell-Codec bestimmt den Aufnahme-Modus (Packet-Copy bei H.264/VP9/AV1, sonst echtes Encoding) -- neu bestimmt bei jedem Reconnect.
                    try:
                        in_video_for_codec = next(s for s in container.streams if s.type == 'video')
                        source_codec = in_video_for_codec.codec_context.name
                        recording_mode = "copy" if source_codec in BROWSER_COMPATIBLE_VIDEO_CODECS else "encode"
                        if recording_mode == "encode":
                            self.logger.warning(
                                f"🎞️ [{self.name}] Quell-Codec '{source_codec}' ist im Dashboard-Player "
                                f"unzuverlässig abspielbar — Aufnahme läuft für diese Kamera per echtem "
                                f"Encoding statt Packet-Copy (kostet GPU/CPU, ist aber die einzig sichere Option)."
                            )
                    except Exception as e:
                        self.logger.warning(f"⚠️ [{self.name}] Video-Codec konnte nicht bestimmt werden ({e}), nehme sicherheitshalber Encode-Modus an.")
                        recording_mode = "encode"

                # SIGTERM-Handler erst hier aktivieren, nicht am Anfang -- vorher läuft fragiler nativer Code (NVENC-Probe, Verbindungsaufbau), ein früher Handler kann dort 'terminate called without an active exception' auslösen.
                signal.signal(signal.SIGTERM, _handle_signal)
                signal.signal(signal.SIGINT, _handle_signal)

                try:
                    for packet in container.demux():
                        if self._stop_event.is_set():
                            break

                        # VIDEO FRAME PROCESSING
                        if packet.stream.type == 'video':
                            # Verhindert, dass DASSELBE Paket mehrfach gepuffert/
                            # eingereiht wird, falls es (selten) zu mehreren Frames
                            # dekodiert — das Paket wird ja nur EINMAL gemuxt,
                            # unabhängig davon wie viele Frames es liefert.
                            packet_video_queued = False
                            for frame in packet.decode():
                                now = time.time()

                                # KRITISCH: Paket-Einreihung muss vor jeder Drosselung passieren -- bei Packet-Copy referenzieren P-/B-Frames vorherige Frames, ein übersprungenes Paket erzeugt Artefakte bis zum nächsten Keyframe.
                                if recording_mode == "copy":
                                    if state == "IDLE" and not packet_video_queued:
                                        av_buffer.append(("video", packet, now))
                                        packet_video_queued = True
                                        trim_buffer()
                                    elif state in ("RECORDING", "POST_ROLL") and not packet_video_queued:
                                        pending_encode_queue.append(('video', packet, now))
                                        packet_video_queued = True

                                # Drosselung jetzt NUR noch für Erkennung/Vorschau/
                                # Filmstrip — beeinflusst nicht mehr, ob das Paket
                                # in der Aufnahme landet (siehe oben, immer).
                                if frame_interval > 0 and (now - last_processed_time) < frame_interval:
                                    continue
                                last_processed_time = now

                                img_bgr = frame.to_ndarray(format='bgr24')

                                # Encode-Modus reiht NACH der Drosselung ein -- unproblematisch, da jedes Bild unabhängig encodiert wird (keine Referenzkette wie bei Packet-Copy).
                                if recording_mode == "encode":
                                    if state == "IDLE":
                                        av_buffer.append(("video", img_bgr.copy(), now))
                                        trim_buffer()
                                    elif state in ("RECORDING", "POST_ROLL"):
                                        pending_encode_queue.append(('video', img_bgr, now))

                                # Modell nachladen, falls es beim Start (oder nach einem
                                # vorherigen Fehlversuch) noch nicht verfügbar war —
                                # siehe Kommentar bei MODEL_RETRY_INTERVAL weiter oben.
                                if detector is None and time.time() - last_model_retry > MODEL_RETRY_INTERVAL:
                                    last_model_retry = time.time()
                                    self.logger.info(f"🔄 [{self.name}] Erneuter Versuch, das KI-Modell zu laden...")
                                    if MODEL_PATH and os.path.exists(MODEL_PATH):
                                        detector, half_enabled = _try_load_model()
                                        if detector is not None:
                                            self.logger.info(f"✅ [{self.name}] KI-Modell nachträglich geladen — Erkennung ist jetzt wieder aktiv.")

                                # Keine synchrone Inferenz mehr -- nur aktuelles Frame an den Erkennungs-Thread übergeben, zuletzt verfügbaren Stand auslesen.
                                if detector is not None:
                                    with _pending_frame_lock:
                                        _pending_frame['img'] = img_bgr
                                    _frame_ready_event.set()

                                with _detection_lock:
                                    boxes = _latest_detection['boxes']
                                    names = _latest_detection['names']
                                    fall_confirmed = _latest_detection.get('fall_confirmed', False)
                                    fall_info = _latest_detection.get('fall_info')
                                    raised_hands_confirmed = _latest_detection.get('raised_hands_confirmed', False)
                                    raised_hands_info = _latest_detection.get('raised_hands_info')
                                    running_detected = _latest_detection.get('running_detected', False)
                                    proximity_confirmed = _latest_detection.get('proximity_confirmed', False)
                                target_detected = boxes is not None and len(boxes) > 0
                                manual_trigger = _check_and_clear_manual_trigger(self.name)
                                if manual_trigger:
                                    target_detected = True
                                if fall_confirmed:
                                    # Ein bestätigter Sturz ist immer aufnahmewürdig,
                                    # unabhängig davon, ob sich die Person noch bewegt.
                                    target_detected = True
                                    _notify_pose_event(
                                        "fall_detected", self.name,
                                        f"Possible fall detected ({(fall_info or {}).get('reason', '')})",
                                        extra_score=(fall_info or {}).get('angle')
                                    )
                                if raised_hands_confirmed:
                                    target_detected = True
                                    _notify_pose_event(
                                        "raised_hands_detected", self.name,
                                        f"Possible distress signal detected ({(raised_hands_info or {}).get('reason', '')})"
                                    )
                                if running_detected or proximity_confirmed:
                                    # Benachrichtigung ist bereits im Erkennungs-Thread
                                    # selbst passiert (siehe _notify_pose_event dort) --
                                    # hier nur zusätzlich als Aufnahme-würdig werten,
                                    # nicht nochmal melden.
                                    target_detected = True

                                # Erst NACH der Detection: write_shared_frame bekommt die
                                # Ergebnisse mit, damit die Live-Vorschau (Grid + Lightbox)
                                # optional Erkennungs-Boxen zeigen kann — kostet keine
                                # zusätzliche Inferenz, nutzt nur das bereits berechnete Ergebnis.
                                write_shared_frame(img_bgr, boxes, names)

                                # Audio-Trigger klinkt sich hier NUR an das Ergebnis an —
                                # keine eigene State-Machine, keine eigene Aufnahme-Logik.
                                # is_triggered() liest nur ein Flag, das der Hintergrund-
                                # Thread setzt — kein blockierender Aufruf.
                                audio_triggered_now, audio_label, audio_score = (False, None, None)
                                if audio_trigger is not None:
                                    audio_triggered_now, audio_label, audio_score = audio_trigger.is_triggered()
                                    if audio_triggered_now and not target_detected:
                                        self.logger.warning(f"🔊 [{self.name}] Aufnahme durch Audio-Trigger ausgelöst: '{audio_label}'")
                                target_detected = target_detected or audio_triggered_now

                                if state == "IDLE":
                                    if target_detected and self.notify_only and not manual_trigger:
                                        # Melden statt aufnehmen -- Zustand bleibt IDLE, die
                                        # eigentliche Aufnahme muss extern (Agent/API) über
                                        # einen manuellen Trigger ausgelöst werden.
                                        # boxes: [x1,y1,x2,y2,conf,class_id] pro Zeile --
                                        # tatsächlich erkannte Klassen, nicht die komplette
                                        # COCO-Zuordnung, die in 'names' steckt.
                                        detected_class_names = [names.get(int(b[5]), str(int(b[5]))) for b in boxes] if names else []
                                        _write_detection_notification(self.name, detected_class_names)
                                    elif target_detected:
                                        state = "RECORDING"
                                        _write_state(self.name, "RECORDING")
                                        _publish_mqtt_recording(self.name, True)
                                        ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                                        os.makedirs(ALERTS_DIR, exist_ok=True)
                                        video_file_path = os.path.join(ALERTS_DIR, f"{self.name}_EVENT_{ts_str}.mp4")
                                        # Markerdatei, solange die Aufnahme läuft — dasselbe Muster
                                        # wie .ai.pending. web_ui.py zeigt anhand dieser Datei ein
                                        # "REC"-Abzeichen an, close_writer() räumt sie wieder auf.
                                        try:
                                            open(os.path.splitext(video_file_path)[0] + '.recording', 'w').close()
                                        except Exception:
                                            pass
                                        self.logger.warning(f"🚨 [DETECTED] Target object found! Starting recording (YOLO {YOLO_VERSION}, Packet-Copy).")

                                        # Zeit-Nullpunkt: ältester Pre-Roll-Frame, damit
                                        # relative Zeitstempel (Filmstrip etc.) korrekt einsortiert werden.
                                        recording_start_time = av_buffer[0][2] if av_buffer else time.time()

                                        # Trigger-Screenshot mit eingezeichneter Erkennungs-Box
                                        # speichern (gleicher Basisname, .jpg) — wird von
                                        # web_ui.py automatisch als Vorschaubild im Dashboard
                                        # (Recent Recordings + Archiv) angezeigt.
                                        try:
                                            thumb_path = os.path.splitext(video_file_path)[0] + '.jpg'
                                            annotated = img_bgr
                                            if boxes is not None and len(boxes) > 0:
                                                annotated = img_bgr.copy()
                                                _draw_boxes_with_labels(cv2, annotated, boxes, names)
                                            cv2.imwrite(thumb_path, annotated)

                                            # Konfidenz + Klasse der stärksten Erkennung als kleines
                                            # Sidecar — fürs Badge auf dem Thumbnail im Dashboard.
                                            trigger_meta = {}
                                            if boxes is not None and len(boxes) > 0:
                                                confs = boxes[:, 4]
                                                clss = boxes[:, 5]
                                                top_idx = int(confs.argmax())
                                                trigger_meta['confidence'] = round(float(confs[top_idx]), 3)
                                                trigger_meta['class'] = str(names.get(int(clss[top_idx]), int(clss[top_idx]))) if names else str(int(clss[top_idx]))
                                            if audio_triggered_now and audio_label:
                                                trigger_meta['audio_trigger'] = audio_label
                                                if audio_score is not None:
                                                    trigger_meta['audio_confidence'] = round(float(audio_score), 3)
                                            if trigger_meta:
                                                meta_path = os.path.splitext(video_file_path)[0] + '.trigger.json'
                                                with open(meta_path, 'w') as mf:
                                                    json.dump(trigger_meta, mf)
                                        except Exception as e:
                                            self.logger.warning(f"⚠️ [{self.name}] Trigger-Screenshot konnte nicht gespeichert werden: {e}")

                                        filmstrip_count_target, filmstrip_interval = _load_filmstrip_settings()
                                        if filmstrip_count_target > 0:
                                            fs_name = os.path.splitext(os.path.basename(video_file_path))[0]
                                            fs_small_dir = os.path.join(ALERTS_DIR, '.thumbs', fs_name, 'small')
                                            fs_large_dir = os.path.join(ALERTS_DIR, '.thumbs', fs_name, 'large')
                                            os.makedirs(fs_small_dir, exist_ok=True)
                                            os.makedirs(fs_large_dir, exist_ok=True)
                                        else:
                                            fs_small_dir = fs_large_dir = None
                                        filmstrip_taken_total = 0
                                        filmstrip_timestamps = {}
                                        filmstrip_next_time = time.time()
                                        filmstrip_pending = {}

                                        try:
                                            out_container = av.open(video_file_path, mode='w')

                                            if recording_mode == "encode":
                                                # Encode-Modus: Kamera liefert keinen browser-
                                                # kompatiblen Codec (MJPEG, rohes USB-Material,
                                                # HEVC, ...) -- echtes Encoding nötig, Packet-Copy
                                                # würde eine im Dashboard nicht abspielbare Datei
                                                # erzeugen (mit Chromium konkret verifiziert).
                                                h, w = img_bgr.shape[:2]
                                                gop_size = str(max(1, TARGET_FPS * 2))
                                                if nvenc_available:
                                                    out_video = out_container.add_stream('h264_nvenc', rate=TARGET_FPS)
                                                    try:
                                                        out_video.options = {'rc': 'vbr', 'cq': '23', 'gpu': '0', 'g': gop_size}
                                                    except Exception as opt_err:
                                                        self.logger.warning(f"⚠️ NVENC-Optionen konnten nicht gesetzt werden ({opt_err}), nutze Encoder-Defaults.")
                                                else:
                                                    out_video = out_container.add_stream('libx264', rate=TARGET_FPS)
                                                    try:
                                                        out_video.options = {'preset': 'veryfast', 'crf': '23', 'g': gop_size}
                                                    except Exception:
                                                        pass
                                                out_video.width = w
                                                out_video.height = h
                                                out_video.pix_fmt = 'yuv420p'
                                                out_video.time_base = Fraction(1, TARGET_FPS)

                                                audio_in_stream = next((s for s in container.streams if s.type == 'audio'), None)
                                                if audio_in_stream:
                                                    # Audio bleibt IMMER Packet-Copy, unabhängig vom
                                                    # Video-Modus -- AAC ist ohnehin universell
                                                    # abspielbar, kein Grund das neu zu encodieren.
                                                    out_audio = out_container.add_stream_from_template(audio_in_stream)

                                                # Keine Keyframe-Suche nötig -- jedes gepufferte
                                                # Bild ist unabhängig (kein P-/B-Frame-Referenz-
                                                # Problem wie bei Packet-Copy), also der komplette
                                                # Pre-Roll-Puffer auf einmal.
                                                pending_encode_queue.extend(av_buffer)

                                            else:
                                                # Packet-Copy statt Neu-Encodieren: ~1000x schneller (0.7ms vs 786ms/150 Frames), Qualität exakt wie die Kamera-Quelle.
                                                in_video_stream = next(s for s in container.streams if s.type == 'video')
                                                out_video = out_container.add_stream_from_template(in_video_stream)

                                                audio_in_stream = next((s for s in container.streams if s.type == 'audio'), None)
                                                if audio_in_stream:
                                                    out_audio = out_container.add_stream_from_template(audio_in_stream)

                                                # Keyframe-Suche: ein Video kann nur an einem Keyframe (I-Frame)
                                                # sauber beginnen — vom Ende des Puffers rückwärts zum letzten
                                                # Video-Keyframe suchen, alles davor verwerfen. Ohne das wäre
                                                # die Datei am Anfang nicht dekodierbar.
                                                keyframe_idx = 0
                                                found_keyframe = False
                                                for i in range(len(av_buffer) - 1, -1, -1):
                                                    item_type, pkt, _ts = av_buffer[i]
                                                    if item_type == "video" and pkt.is_keyframe:
                                                        keyframe_idx = i
                                                        found_keyframe = True
                                                        break
                                                if not found_keyframe and av_buffer:
                                                    # Seltener Randfall: Trigger direkt nach Verbindungsaufbau,
                                                    # bevor der erste Keyframe überhaupt ankam. Puffer beginnt
                                                    # dann zwangsläufig NICHT an einem Keyframe — kann zu einem
                                                    # kurz unsauberen/nicht dekodierbaren Anfang führen. Selten
                                                    # genug, um es nur sichtbar zu machen statt komplex
                                                    # abzufangen (z.B. Pre-Roll für diesen einen Trigger verwerfen).
                                                    self.logger.warning(
                                                        f"⚠️ [{self.name}] Kein Keyframe im Pre-Roll-Puffer gefunden "
                                                        f"(vermutlich Trigger kurz nach Verbindungsaufbau) — Aufnahme-"
                                                        f"Anfang könnte kurz unsauber sein."
                                                    )
                                                aligned_buffer = list(av_buffer)[keyframe_idx:]

                                                # Nicht mehr sofort schreiben — nur einreihen. Der Drain-Schritt
                                                # (siehe _drain_encode_queue) arbeitet das über die nächsten
                                                # Loop-Durchläufe verteilt ab. Bei Packet-Copy ist das ohnehin
                                                # kaum noch nötig (so schnell), bleibt aber als Sicherheitsnetz.
                                                pending_encode_queue.extend(aligned_buffer)

                                        except Exception as e:
                                            self.logger.error(f"❌ Failed to initialize video writer: {e}")
                                            close_writer()
                                            state = "IDLE"
                                            _write_state(self.name, "IDLE")
                                            _publish_mqtt_recording(self.name, False)

                                if state in ("RECORDING", "POST_ROLL") and _check_and_clear_manual_stop(self.name):
                                    # Externe Stop-Anforderung (Agent/API) -- beendet die
                                    # Aufnahme SOFORT, unabhängig davon ob YOLO gerade noch
                                    # etwas sieht. Das ist der einzige Weg, eine laufende
                                    # Aufnahme von außen zu beenden -- cameras_toggle/disable
                                    # setzt nur 'enabled' in streams.json, das ein bereits
                                    # laufender Worker-Prozess nie erneut liest.
                                    _finish_recording_now("manual stop")
                                elif state == "RECORDING":
                                    if target_detected:
                                        capture_filmstrip(img_bgr, boxes, names)
                                    else:
                                        state = "POST_ROLL"
                                        _write_state(self.name, "POST_ROLL")
                                        post_roll_end_time = time.time() + POST_ROLL_SEC
                                        self.logger.info(f"🏠 [GONE] Target object left frame. Monitoring for {POST_ROLL_SEC}s extra.")
                                        capture_filmstrip(img_bgr, boxes, names)

                                elif state == "POST_ROLL":
                                    if target_detected:
                                        state = "RECORDING"
                                        _write_state(self.name, "RECORDING")
                                        vision_hit = bool(boxes is not None and len(boxes) > 0)
                                        sources = []
                                        if vision_hit:
                                            sources.append("visual detection")
                                        if audio_triggered_now:
                                            sources.append(f"audio ('{audio_label}')" if audio_label else "audio")
                                        source_desc = " + ".join(sources) if sources else "detection"
                                        self.logger.info(f"🚨 [DETECTED] Target returned ({source_desc})! Resuming recording.")
                                        capture_filmstrip(img_bgr, boxes, names)
                                    else:
                                        capture_filmstrip(img_bgr, boxes, names)
                                        if time.time() > post_roll_end_time:
                                            _finish_recording_now("post-roll timeout")

                                # Begrenzte Menge aus der Encoding-Warteschlange abarbeiten —
                                # nach JEDEM verarbeiteten Video-Frame, unabhängig vom State-
                                # Zweig, damit ein Pre-Roll-Burst gleichmäßig über die
                                # nächsten Loop-Durchläufe verteilt wird.
                                _drain_encode_queue()

                        # AUDIO FRAME PROCESSING
                        elif packet.stream.type == 'audio':
                            packet_audio_queued = False
                            for a_frame in packet.decode():
                                now = time.time()
                                if state == "IDLE" and not packet_audio_queued:
                                    av_buffer.append(("audio", packet, now))
                                    packet_audio_queued = True
                                    trim_buffer()
                                if state in ["RECORDING", "POST_ROLL"] and not packet_audio_queued:
                                    pending_encode_queue.append(('audio', packet, now))
                                    packet_audio_queued = True
                                _drain_encode_queue()

                                # Audio-Trigger füttern: NUR ein billiger Buffer-Append,
                                # die eigentliche (langsame) Klassifikation läuft komplett
                                # in AudioTrigger's eigenem Hintergrund-Thread — blockiert
                                # hier nichts.
                                if audio_trigger is not None:
                                    try:
                                        samples = a_frame.to_ndarray()
                                        if samples.ndim > 1:
                                            samples = samples.mean(axis=0)
                                        samples = samples.astype(np.float32)
                                        if np.issubdtype(samples.dtype, np.integer):
                                            samples = samples / 32768.0
                                        max_abs = np.abs(samples).max() if samples.size else 0
                                        if max_abs > 4.0:  # vermutlich noch Integer-PCM (z.B. int16-Range)
                                            samples = samples / 32768.0
                                        audio_trigger.feed(samples, a_frame.sample_rate)
                                    except Exception:
                                        pass

                except GracefulShutdown:
                    raise
                except Exception as e:
                    self.logger.error(f"⚠️ [STREAM LOST] '{self.name}': {e}. Retrying in 5s...")
                    if using_nvdec:
                        # Zählt als Fehlversuch für den NVDEC-Streak — die Cam war
                        # evtl. nur kurz weg, dann wird beim nächsten Reconnect (oben)
                        # NVDEC ganz normal wieder probiert. Erst nach mehreren
                        # Fehlversuchen IN FOLGE ohne jeden Erfolg schaltet der
                        # Verbindungs-Block hw_device dauerhaft ab.
                        nvdec_fail_streak += 1
                        using_nvdec = False
                    close_writer()
                    state = "IDLE"
                    _write_state(self.name, "IDLE")
                    _publish_mqtt_recording(self.name, False)
                    # av_buffer explizit leeren vor dem Schließen des Quell-Containers -- sonst können alte Pakete noch darauf zeigen und beim Muxen segfaulten (reproduziert).
                    av_buffer.clear()
                    if container:
                        try:
                            container.close()
                        except Exception:
                            pass
                    container = None
                    time.sleep(5)

        except GracefulShutdown:
            self.logger.info(f"🛑 [{self.name}] Shutdown-Signal empfangen, schließe sauber ab...")
        except Exception as e:
            self.logger.error(f"💥 Process Crash [{self.name}]: {e}")
        finally:
            close_writer()
            # queue.join() wartet, bis alle Filmstrip-Bilder wirklich geschrieben sind -- der Daemon-Thread stirbt sonst mit dem Prozess und verliert die letzten Bilder.
            try:
                _filmstrip_write_queue.join()
            except Exception:
                pass
            av_buffer.clear()  # dieselbe Absicherung wie beim Reconnect-Pfad
            _detector_stop_event.set()
            if audio_trigger is not None:
                audio_trigger.stop()
            if container:
                try:
                    container.close()
                except Exception:
                    pass
            if self._platform_bridge is not None:
                self._platform_bridge.stop()
                self._platform_bridge.cleanup()
            self.logger.info("🛑 Agent process shutting down.")

    def stop_agent(self):
        self._stop_event.set()


def detect_gpu_profile(logger):
    """Liest die verbaute GPU einmalig VOR dem Start der Pipeline aus und
    bestimmt sichere Defaults — von RTX 2060 (Turing) bis RTX 5090 (Blackwell).
    Läuft im Master, das Ergebnis wird an jeden CameraAgent durchgereicht,
    statt dass jeder Worker-Prozess einzeln (und potenziell widersprüchlich)
    dieselbe Erkennung nochmal macht."""
    profile = {"cuda_available": False, "half_precision": False, "name": "CPU"}
    try:
        import torch
        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            major, minor = torch.cuda.get_device_capability(0)
            vram_gb = round(torch.cuda.get_device_properties(0).total_memory / (1024 ** 3), 1)
            cuda_version = torch.version.cuda
            profile.update({
                "cuda_available": True,
                "name": name,
                "compute_capability": f"{major}.{minor}",
                "vram_gb": vram_gb,
                "cuda_version": cuda_version,
                # Ab Volta (7.0) sind Tensor Cores für schnelles FP16 vorhanden —
                # von Turing (RTX 2060, 7.5) bis Blackwell (RTX 5090, 12.0)
                # durchgängig gegeben. Darunter lohnt sich FP16 nicht.
                "half_precision": major >= 7,
            })
            logger.info(
                f"🎮 [MASTER] GPU erkannt: {name} | Compute Capability {major}.{minor} | "
                f"{vram_gb} GB VRAM | PyTorch-CUDA {cuda_version} | FP16-Inferenz: "
                f"{'aktiv' if profile['half_precision'] else 'inaktiv (Architektur zu alt)'}"
            )
        else:
            logger.warning("⚠️ [MASTER] Keine CUDA-GPU gefunden — Pipeline läuft komplett auf CPU (deutlich langsamer).")
    except Exception as e:
        logger.warning(f"⚠️ [MASTER] GPU-Erkennung fehlgeschlagen ({e}) — nehme sicheren CPU-Fallback an.")
    return profile


# ---------------------------------------------------------
# MAIN ORCHESTRATOR
# ---------------------------------------------------------
if __name__ == "__main__":
    # KRITISCH: muss vor jedem CUDA-Zugriff im Master passieren -- 'spawn' statt 'fork', da sich ein CUDA-Kontext nach fork() nicht sauber re-initialisieren lässt (PyTorch-Empfehlung).
    multiprocessing.set_start_method('spawn', force=True)

    system_logger = get_stream_logger("SYSTEM")
    system_logger.info("🚀 [MASTER] Initializing Multi-Agent Pipeline...")

    # Fix 2 (Master-Seite): stop.sh killt per `pkill -f "recorder_pipeline.py"`,
    # was auch den Master-Prozess selbst trifft (SIGTERM, nicht SIGINT) — ohne
    # eigenen Handler wäre `except KeyboardInterrupt` unten wirkungslos und die
    # Cleanup-Schleife für alle Worker würde nie laufen.
    shutdown_requested = threading.Event()

    def _handle_master_signal(signum, frame):
        system_logger.info(f"[MASTER] Signal {signum} empfangen — fahre Pipeline sauber herunter...")
        shutdown_requested.set()

    signal.signal(signal.SIGTERM, _handle_master_signal)
    signal.signal(signal.SIGINT, _handle_master_signal)

    # Hardware VOR dem Start der Pipeline auslesen (RTX 2060 bis RTX 5090) —
    # bestimmt einmalig zentral, ob FP16-Inferenz probiert werden soll.
    gpu_profile = detect_gpu_profile(system_logger)

    # Vollständiges Startup-Log: welches Modell, welche Settings, welche
    # Kameras — alles, was die Pipeline für diesen Lauf tatsächlich nutzt.
    enabled_names = [s['name'] for s in STREAMS if s.get('enabled', False)]
    disabled_names = [s['name'] for s in STREAMS if not s.get('enabled', False)]
    system_logger.info("=" * 70)
    system_logger.info("🚀 [MASTER] vaelen Pipeline Startup")
    system_logger.info(f"   KI-Modell     : YOLO {YOLO_VERSION} | Pfad: {MODEL_PATH}")
    system_logger.info(f"   Detection     : Klassen={DETECTION_CLASSES} | Confidence={CONFIDENCE_THRESHOLD}")
    system_logger.info(f"   Aufnahme      : Ziel-FPS={TARGET_FPS} | Pre-Roll={PRE_ROLL_SEC}s | Post-Roll={POST_ROLL_SEC}s")
    system_logger.info(f"   Alerts-Pfad   : {ALERTS_DIR}")
    system_logger.info(f"   cuDNN         : {'per DISABLE_CUDNN erzwungen aus' if DISABLE_CUDNN else 'wird versucht (mit Selbsttest-Fallback)'}")
    system_logger.info(f"   GPU           : {gpu_profile['name']}" + (f" ({gpu_profile.get('compute_capability')}, {gpu_profile.get('vram_gb')} GB)" if gpu_profile['cuda_available'] else ""))
    system_logger.info(f"   Aktive Kameras ({len(enabled_names)}): {', '.join(enabled_names) if enabled_names else '—'}")
    system_logger.info(f"   Inaktive Kameras ({len(disabled_names)}): {', '.join(disabled_names) if disabled_names else '—'}")
    system_logger.info("=" * 70)

    # Fix 3: statt nur (proc) merken wir uns auch die Stream-Config, damit ein
    # abgestürzter Worker mit denselben Settings automatisch neu gestartet
    # werden kann, statt nur eine Warnung zu loggen.
    agents = []
    for stream in STREAMS:
        if stream.get("enabled", False):
            agent_proc = CameraAgent(stream, half_precision=gpu_profile["half_precision"])
            agent_proc.start()
            agents.append({'process': agent_proc, 'type': 'camera', 'stream': stream, 'name': stream['name']})
            system_logger.info(f"📡 [MASTER] Launched Process Worker for: {stream['name']}")
        else:
            system_logger.info(f"⏭️ [MASTER] Skipping Disabled Stream: {stream['name']}")

    # Watchfolder-Import: eigener Prozess, unabhängig von den Kamera-Streams,
    # nur gestartet wenn in den Settings aktiviert. Andere Konstruktor-
    # Signatur als CameraAgent -- deshalb 'type' pro Eintrag, damit die
    # Monitoring-Schleife unten beim Neustart den richtigen Prozess-Typ baut.
    try:
        with open(SETTINGS_F) as f:
            _settings_for_watchfolder = json.load(f)
    except Exception:
        _settings_for_watchfolder = {}
    if _settings_for_watchfolder.get("WATCH_FOLDER_ENABLED", False):
        from watch_folder import WatchFolderAgent
        wf_proc = WatchFolderAgent()
        wf_proc.start()
        agents.append({'process': wf_proc, 'type': 'watchfolder', 'stream': None, 'name': 'Watchfolder'})
        system_logger.info("📥 [MASTER] Launched Watchfolder import process.")

    if not agents:
        system_logger.error("❌ No active streams found! Exiting.")
        sys.exit(1)

    system_logger.info("[MASTER] All processes running in parallel. Monitoring ACTIVE.")

    while not shutdown_requested.is_set():
        # Live-Abgleich mit streams.json -- vorher wurde diese Datei NUR beim
        # Start gelesen. Eine Kamera über die Agenten-/Dashboard-API zu
        # aktivieren/deaktivieren änderte zwar die Datei, hatte aber KEINE
        # Wirkung auf einen bereits laufenden Master: eine neu aktivierte
        # Kamera bekam nie einen Prozess, eine deaktivierte lief einfach
        # unbeeindruckt weiter -- bis zum nächsten vollständigen
        # Pipeline-Neustart. Das führte dazu, dass ein manueller Trigger für
        # eine gerade erst aktivierte Kamera ins Leere lief: das Flag wurde
        # geschrieben, aber kein Prozess existierte, der es je gelesen hätte.
        try:
            with open(STREAMS_F) as f:
                current_streams = json.load(f)
        except Exception:
            current_streams = STREAMS  # Datei kurzzeitig nicht lesbar -- alten Stand behalten statt abzustürzen
        current_by_name = {s['name']: s for s in current_streams if 'name' in s}

        for entry in agents:
            if entry['type'] != 'camera':
                continue
            proc = entry['process']
            still_enabled = current_by_name.get(entry['name'], {}).get('enabled', False)
            if not proc.is_alive():
                exitcode = proc.exitcode
                if still_enabled:
                    system_logger.warning(
                        f"⚠️ [MASTER] Worker '{entry['name']}' ist beendet (exitcode={exitcode}) — starte automatisch neu..."
                    )
                    new_proc = CameraAgent(current_by_name[entry['name']], half_precision=gpu_profile["half_precision"])
                    new_proc.start()
                    entry['process'] = new_proc
                    entry['stream'] = current_by_name[entry['name']]
                else:
                    system_logger.info(f"⏹️ [MASTER] '{entry['name']}' ist beendet und wurde zwischenzeitlich deaktiviert — kein Neustart.")
            elif not still_enabled:
                system_logger.info(f"⏹️ [MASTER] '{entry['name']}' wurde deaktiviert — stoppe laufenden Prozess.")
                proc.stop_agent()
                proc.join(timeout=5)
                if proc.is_alive():
                    proc.terminate()

        # Nicht-Kamera-Einträge (Watchfolder) unverändert nach dem alten Muster prüfen
        for entry in agents:
            if entry['type'] == 'camera':
                continue
            proc = entry['process']
            if not proc.is_alive():
                exitcode = proc.exitcode
                system_logger.warning(
                    f"⚠️ [MASTER] Worker '{entry['name']}' ist beendet (exitcode={exitcode}) — starte automatisch neu..."
                )
                from watch_folder import WatchFolderAgent
                new_proc = WatchFolderAgent()
                new_proc.start()
                entry['process'] = new_proc

        # Tote/gestoppte Einträge aus der Liste entfernen (sonst wächst sie
        # bei wiederholtem Ein-/Ausschalten immer weiter mit Leichen an)
        agents = [e for e in agents if e['process'].is_alive()]

        # Neu aktivierte Kameras, die noch gar keinen Eintrag haben, jetzt starten
        running_names = {e['name'] for e in agents if e['type'] == 'camera'}
        for name, stream in current_by_name.items():
            if stream.get('enabled', False) and name not in running_names:
                system_logger.info(f"📡 [MASTER] '{name}' wurde neu aktiviert — starte Prozess.")
                new_proc = CameraAgent(stream, half_precision=gpu_profile["half_precision"])
                new_proc.start()
                agents.append({'process': new_proc, 'type': 'camera', 'stream': stream, 'name': name})

        # Interruptible statt time.sleep(15): reagiert sofort auf ein Signal
        # statt bis zu 15s zu blockieren.
        shutdown_requested.wait(15)

    system_logger.info("[MASTER] Shutting down all workers...")
    for entry in agents:
        proc = entry['process']
        proc.stop_agent()
        proc.join(timeout=5)
        if proc.is_alive():
            system_logger.warning(f"⚠️ [MASTER] '{entry['name']}' reagiert nicht — erzwinge Terminate.")
            proc.terminate()
            proc.join(timeout=2)

    system_logger.info("[MASTER] Pipeline shutdown complete.")
