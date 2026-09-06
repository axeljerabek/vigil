from flask import Flask, render_template, request, Response, abort, send_file
import os
import sys
import glob
import uuid
import csv
import io
import subprocess
import shutil
import json
import re
import secrets
import threading
import time
import psutil
import urllib.request
from collections import deque
from datetime import datetime

# Stellt sicher, dass das Arbeitsverzeichnis und der Import-Pfad passen
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(SCRIPT_DIR)
try:
    import ai_analyze
except Exception as e:
    ai_analyze = None
    print(f"⚠️ ai_analyze-Modul konnte nicht importiert werden, eigene Notizen können nicht in die XMP-Sidecar geschrieben werden: {e}")
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
os.chdir(SCRIPT_DIR)

from config import STREAMS, STREAMS_F, ALERTS_DIR, PROJECT_ROOT, SETTINGS_F, YOLO_VERSION, MODEL_SIZE, MODEL_FILENAME, COCO_CLASS_NAMES
from auth import requires_auth
from helpers import (
    LATEST_FRAMES, start_thumbnail_thread, is_pipeline_running,
    load_overrides, load_settings, save_overrides, format_size
)
try:
    import search_index
except ImportError:
    search_index = None  # Optionales Feature — Dashboard läuft unverändert ohne Suche
try:
    import faces_db
except ImportError:
    faces_db = None  # Optionales Feature — Dashboard läuft unverändert ohne Gesichtserkennung

app = Flask(__name__)

# Externe API zur Remote-Steuerung (separate API-Key-Auth, nicht die Dashboard-Session) --
# eigenes Blueprint statt alles hier reinzupacken, siehe mam_api.py.
try:
    from mam_api import mam_bp
    app.register_blueprint(mam_bp)
except Exception as e:
    print(f"⚠️ Externe API konnte nicht geladen werden: {e}")

# Archiv-Unterordner für aufbewahrte Aufnahmen (getrennt von den aktiven Alerts)
ARCHIVE_DIR = os.path.join(ALERTS_DIR, 'archive')
SUMMARIES_DIR = os.path.join(ALERTS_DIR, '.summaries')

@app.route('/api/generate_summary', methods=['POST'])
@requires_auth
def generate_summary():
    _verify_csrf()
    period = request.form.get('period', 'day')
    if period not in ('day', 'week'):
        return json.dumps({'ok': False, 'error': 'Invalid period.'})
    date_arg = request.form.get('date', '').strip()
    # Läuft als eigener Subprozess -- derselbe Grund wie bei der KI-Analyse:
    # der Ollama-Aufruf kann eine Weile dauern, das darf den Flask-Request-
    # Thread nicht blockieren. Frontend pollt danach /api/summaries.
    cmd = [sys.executable, os.path.join(SCRIPT_DIR, 'daily_summary.py'), '--period', period]
    if date_arg:
        cmd += ['--date', date_arg]
    try:
        subprocess.Popen(cmd)
    except Exception as e:
        return json.dumps({'ok': False, 'error': str(e)})
    return json.dumps({'ok': True})

@app.route('/api/summaries')
@requires_auth
def list_summaries():
    """Liefert die zuletzt generierten Zusammenfassungen, neueste zuerst."""
    results = []
    if os.path.isdir(SUMMARIES_DIR):
        for path in sorted(glob.glob(os.path.join(SUMMARIES_DIR, '*.json')), reverse=True)[:20]:
            try:
                with open(path) as f:
                    entry = json.load(f)
                entry['filename'] = os.path.basename(path)
                results.append(entry)
            except Exception:
                continue
    return json.dumps({'summaries': results})

@app.route('/api/delete_summary', methods=['POST'])
@requires_auth
def delete_summary():
    _verify_csrf()
    filename = request.form.get('filename', '')
    # Nur exakt die erwartete Namenskonvention zulassen (day_YYYYMMDD.json /
    # week_YYYYMMDD.json) -- verhindert jeden Pfad-Trick (../../etc), auch
    # wenn os.path.basename() weiter unten schon zusätzlich absichert.
    if not re.match(r'^(day|week)_\d{8}\.json$', filename):
        return json.dumps({'ok': False, 'error': 'Invalid filename.'})
    path = os.path.join(SUMMARIES_DIR, os.path.basename(filename))
    if not os.path.exists(path):
        return json.dumps({'ok': False, 'error': 'Summary not found.'})
    try:
        os.remove(path)
    except Exception as e:
        return json.dumps({'ok': False, 'error': str(e)})
    return json.dumps({'ok': True})

@app.route('/api/agent_config', methods=['GET'])
@requires_auth
def get_agent_config():
    try:
        import agent_permissions
        return json.dumps(agent_permissions.load_config())
    except ImportError:
        return json.dumps({'available': False})

@app.route('/api/agent_config', methods=['POST'])
@requires_auth
def save_agent_config():
    _verify_csrf()
    try:
        import agent_permissions
    except ImportError:
        return json.dumps({'ok': False, 'error': 'agent_permissions module not available.'})
    config = agent_permissions.load_config()
    config['agent_control_enabled'] = request.form.get('agent_control_enabled') == 'on'
    for cap in config.get('capabilities', {}):
        if cap in ('delete', 'export'):
            continue  # nicht implementiert -- Checkbox in der GUI ist bewusst deaktiviert, hier zusätzlich serverseitig ignoriert
        config['capabilities'][cap]['enabled'] = request.form.get(f'cap_{cap}') == 'on'
    with open(agent_permissions.CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)
    return json.dumps({'ok': True})

@app.route('/api/mam_keys', methods=['GET'])
@requires_auth
def list_mam_keys():
    try:
        import mam_api
    except ImportError:
        return json.dumps({'available': False})
    return json.dumps({'available': True, 'keys': mam_api.list_api_keys()})

@app.route('/api/mam_keys', methods=['POST'])
@requires_auth
def create_mam_key():
    _verify_csrf()
    try:
        import mam_api
    except ImportError:
        return json.dumps({'ok': False, 'error': 'mam_api module not available.'})
    label = request.form.get('label', '').strip()
    raw_key = mam_api.generate_api_key(label)
    # Der Klartext-Key wird HIER EINMALIG zurückgegeben -- danach ist er aus
    # vigil selbst nicht mehr abrufbar (nur der Hash wird gespeichert).
    return json.dumps({'ok': True, 'key': raw_key})

@app.route('/api/mam_keys/<key_hash>/revoke', methods=['POST'])
@requires_auth
def delete_mam_key(key_hash):
    _verify_csrf()
    try:
        import mam_api
    except ImportError:
        return json.dumps({'ok': False, 'error': 'mam_api module not available.'})
    ok = mam_api.revoke_api_key(key_hash)
    return json.dumps({'ok': ok})

@app.route('/api/anomaly_status')
@requires_auth
def anomaly_status():
    try:
        import anomaly_detection
    except ImportError:
        return json.dumps({'available': False})
    cameras = anomaly_detection.list_cameras_with_data()
    statuses = []
    for camera in cameras:
        status = anomaly_detection.model_status(camera)
        entry = {'camera': camera, 'trained': status is not None, **(status or {})}
        if status is None:
            entry['available_count'] = len(anomaly_detection.gather_embeddings(camera, 30))
            entry['min_required'] = anomaly_detection.MIN_TRAINING_SAMPLES
        statuses.append(entry)
    return json.dumps({'available': True, 'cameras': statuses})

@app.route('/api/train_anomaly_models', methods=['POST'])
@requires_auth
def train_anomaly_models():
    _verify_csrf()
    try:
        import anomaly_detection
    except ImportError:
        return json.dumps({'ok': False, 'error': 'scikit-learn not installed.'})
    lookback = int(request.form.get('lookback_days', 30))
    # Läuft synchron -- Isolation-Forest-Training ist auf den hier zu
    # erwartenden Datenmengen (hunderte, nicht Millionen Events) eine Sache
    # von Sekunden, kein eigener Hintergrund-Prozess wie bei der Ollama-
    # basierten Zusammenfassung nötig.
    results = anomaly_detection.train_all_cameras(lookback)
    return json.dumps({
        'ok': True,
        'results': {camera: {'trained': ok, 'message': msg} for camera, (ok, msg) in results.items()}
    })

os.makedirs(ARCHIVE_DIR, exist_ok=True)

def _cleanup_old_recordings():
    """Löscht unarchivierte Aufnahmen älter als RETENTION_DAYS (0 = aus).
    Nur ALERTS_DIR, nie ARCHIVE_DIR — Archivieren bedeutet bewusst 'behalten'."""
    while True:
        try:
            days = load_settings().get('RETENTION_DAYS', 0)
            if days and days > 0:
                cutoff = time.time() - days * 86400
                for f in glob.glob(os.path.join(ALERTS_DIR, '*.mp4')):
                    try:
                        if os.path.getmtime(f) < cutoff:
                            os.remove(f)
                            _remove_matching_thumbnail(f)
                    except OSError:
                        pass
        except Exception:
            pass
        time.sleep(3600)

threading.Thread(target=_cleanup_old_recordings, daemon=True).start()

# Harte Obergrenze für geladene Event-Listen, damit glob()/sort() bei Monaten
# an Aufnahmen nicht bei jedem Request/Poll unnötig groß wird.
MAX_EVENTS = 200

# CSRF-Token: pro Prozessstart neu generiert, in jede Seite eingebettet und bei
# jeder zustandsändernden POST-Route geprüft. Schützt vor klassischem CSRF von
# einer fremden Seite aus (die den Token nicht kennt), auch wenn der Browser
# gecachte Basic-Auth-Header automatisch mitschickt.
CSRF_TOKEN = secrets.token_hex(32)

def _verify_csrf():
    token = request.form.get('csrf_token', '')
    if not secrets.compare_digest(token, CSRF_TOKEN):
        abort(403)

AVAILABLE_CLASSES = COCO_CLASS_NAMES

# Worker-Thread starten
start_thumbnail_thread()

# --- Pipeline-Neustart im Hintergrund (Punkt 1: blockiert das UI nicht mehr) ---
pipeline_restart_status = {"restarting": False}
_restart_lock = threading.Lock()

def _restart_pipeline_background():
    with _restart_lock:
        pipeline_restart_status["restarting"] = True
    try:
        subprocess.run(
            ['/bin/bash', os.path.join(PROJECT_ROOT, 'stop.sh')],
            cwd=PROJECT_ROOT
        )
        subprocess.Popen(
            ['/bin/bash', os.path.join(PROJECT_ROOT, 'start_detached.sh')],
            cwd=PROJECT_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        # Kurze Wartezeit, damit is_pipeline_running() den neuen Prozess
        # sicher erkennt, bevor das Restarting-Flag zurückgesetzt wird.
        time.sleep(2)
    finally:
        with _restart_lock:
            pipeline_restart_status["restarting"] = False

def _read_recording_states():
    """Liest die von recorder_pipeline.py geschriebenen State-Dateien (.status/<name>.json)."""
    states = {}
    status_dir = os.path.join(ALERTS_DIR, '.status')
    for f in glob.glob(os.path.join(status_dir, '*.json')):
        try:
            with open(f) as fh:
                states[os.path.splitext(os.path.basename(f))[0]] = json.load(fh).get('state', 'IDLE')
        except Exception:
            pass
    return states

_event_cache = {}  # directory -> (expires_at, events)
EVENT_CACHE_TTL = 4  # Sekunden — knapp über dem 3s-Poll-Intervall, spart die teure

def build_event_list(directory, limit=MAX_EVENTS):
    now = time.time()
    cached = _event_cache.get(directory)
    if cached and cached[0] > now:
        return cached[1]
    events = _build_event_list(directory, limit)
    _event_cache[directory] = (now + EVENT_CACHE_TTL, events)
    return events

def _get_video_duration(path):
    """Liest nur die Container-Metadaten (kein Dekodieren) — schnell genug,
    um bei jedem Event-Listing-Refresh für alle Videos aufgerufen zu
    werden. av lokal importiert (wie in recorder_pipeline.py), damit der
    Web-UI-Prozess av nicht unnötig lädt, wenn diese Funktion nie gebraucht
    wird. Liefert None statt eine 0:00-Anzeige, falls das Lesen fehlschlägt
    — besser gar keine Angabe als eine falsche."""
    try:
        import av
        with av.open(path) as c:
            dur = c.duration
        if not dur:
            return None
        total_seconds = int(dur / 1_000_000)  # av.duration ist in AV_TIME_BASE (Mikrosekunden)
        m, s = divmod(total_seconds, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    except Exception:
        return None


def _event_from_file(f):
    try:
        mtime = os.path.getmtime(f)
        size = os.path.getsize(f)
        # recorder_pipeline.py legt beim Trigger einen Screenshot mit
        # gleichem Basisnamen ab (<name>.mp4 -> <name>.jpg)
        thumb_path = os.path.splitext(f)[0] + '.jpg'
        fs_base_dir = os.path.join(os.path.dirname(f), '.thumbs', os.path.splitext(os.path.basename(f))[0])
        fs_dir = os.path.join(fs_base_dir, 'small')
        fs_count = len(glob.glob(os.path.join(fs_dir, '*.jpg'))) if os.path.isdir(fs_dir) else 0
        # Durch Reservoir Sampling (siehe recorder_pipeline.py) entspricht die
        # Dateinummer NICHT mehr zwangsläufig der zeitlichen Reihenfolge —
        # timestamps.json verrät die echte Chronologie. Fehlt sie (alte
        # Aufnahmen von vor diesem Fix), einfach numerisch sortieren.
        fs_order = list(range(fs_count))
        ts_path = os.path.join(fs_base_dir, 'timestamps.json')
        if fs_count and os.path.exists(ts_path):
            try:
                with open(ts_path) as tsf:
                    ts_map = json.load(tsf)
                fs_order = sorted(range(fs_count), key=lambda i: ts_map.get(str(i), i))
            except Exception:
                pass
        ai_desc = None
        top_topic, top_topic_conf = None, None
        detected_topics = []
        transcript = None
        user_note = ''
        rating = None
        ai_path = os.path.splitext(f)[0] + '.ai.json'
        if os.path.exists(ai_path):
            try:
                with open(ai_path) as af:
                    ai_meta = json.load(af)
                ai_desc = ai_meta.get('description')
                top_topic = ai_meta.get('top_topic')
                top_topic_conf = ai_meta.get('top_topic_confidence')
                detected_topics = ai_meta.get('detected_topics') or []
                transcript = ai_meta.get('transcript')
                user_note = ai_meta.get('user_note', '')
                rating = ai_meta.get('rating')
            except Exception:
                pass
        ai_pending = os.path.exists(os.path.splitext(f)[0] + '.ai.pending')
        trigger_conf, trigger_cls = None, None
        audio_trigger_label, audio_trigger_conf = None, None
        is_manual = False
        trigger_path = os.path.splitext(f)[0] + '.trigger.json'
        if os.path.exists(trigger_path):
            try:
                with open(trigger_path) as tf:
                    tmeta = json.load(tf)
                trigger_conf = tmeta.get('confidence')
                trigger_cls = tmeta.get('class')
                audio_trigger_label = tmeta.get('audio_trigger')
                audio_trigger_conf = tmeta.get('audio_confidence')
                is_manual = bool(tmeta.get('manual', False))
            except Exception:
                pass
        faces_summary = {'people': [], 'unnamed_count': 0}
        if faces_db is not None:
            try:
                faces_summary = faces_db.get_faces_summary_for_video(os.path.basename(f))
            except Exception:
                pass
        # Solange recorder_pipeline.py noch schreibt, ist die Container-Datei
        # unvollständig (moov-Atom oft erst beim Schließen finalisiert) —
        # Dauer währenddessen NICHT lesen (unzuverlässig/inkorrekt), nur das
        # REC-Abzeichen anzeigen. Beides über dieselbe Markerdatei, die
        # recorder_pipeline.py beim Start anlegt und beim Schließen entfernt.
        is_recording = os.path.exists(os.path.splitext(f)[0] + '.recording')
        duration = None if is_recording else _get_video_duration(f)
        return {
            'filename': os.path.basename(f),
            'datetime': datetime.fromtimestamp(mtime).strftime('%d.%m.%Y %H:%M'),
            'size': format_size(size),
            'duration': duration,
            'is_recording': is_recording,
            'has_thumbnail': os.path.exists(thumb_path),
            'filmstrip_count': fs_count,
            'filmstrip_order': fs_order,
            'ai_description': ai_desc,
            'ai_pending': ai_pending,
            'top_topic': top_topic,
            'top_topic_confidence': top_topic_conf,
            'detected_topics': detected_topics,
            'transcript': transcript,
            'user_note': user_note,
            'rating': rating,
            'trigger_confidence': trigger_conf,
            'trigger_class': trigger_cls,
            'manual': is_manual,
            'audio_trigger_label': audio_trigger_label,
            'audio_trigger_confidence': audio_trigger_conf,
            'people_in_video': faces_summary['people'],
            'unrecognized_face_count': faces_summary['unnamed_count']
        }
    except OSError:
        return None

def _build_event_list(directory, limit=MAX_EVENTS):
    """Baut die Event-Liste (Dateiname, Datum, Größe) für ein gegebenes Verzeichnis."""
    files = sorted(glob.glob(os.path.join(directory, '*.mp4')), key=os.path.getmtime, reverse=True)[:limit]
    return [e for e in (_event_from_file(f) for f in files) if e]

def _build_full_event_list(directory):
    """Wie _build_event_list, aber ohne MAX_EVENTS-Obergrenze — fürs
    Export gedacht, wo wirklich der komplette Bestand gebraucht wird,
    nicht nur die für die Dashboard-Ansicht ohnehin gedeckelten neuesten."""
    files = sorted(glob.glob(os.path.join(directory, '*.mp4')), key=os.path.getmtime, reverse=True)
    return [e for e in (_event_from_file(f) for f in files) if e]

def _camera_name_from_filename(filename):
    """Kameraname aus dem Dateinamen extrahieren: <Kamera>_EVENT_<Zeitstempel>.mp4
    oder <Kamera>_MANUAL_<Zeitstempel>.mp4 (manuelle Notruf-Aufnahme, siehe
    api_manual_record_start). Exakter Split statt Prefix-Vergleich, damit z.B.
    'Bed' nicht fälschlich 'Bedroom' matcht."""
    for marker in ('_EVENT_', '_MANUAL_'):
        if marker in filename:
            return filename.split(marker)[0]
    return filename

@app.route('/api/filter_events')
@requires_auth
def api_filter_events():
    """Durchsucht den KOMPLETTEN Bestand (nicht nur die im Dashboard geladenen/
    paginierten Events) nach Kamera/Person/Thema — im Unterschied zu einem rein
    clientseitigen Filter über die schon geladenen Events, der bei Kameras/
    Personen/Themen aus älterer, noch nicht nachgeladener Historie sonst
    unvollständige Ergebnisse liefern würde."""
    camera = request.args.get('camera', '').strip()
    person = request.args.get('person', '').strip()
    topic = request.args.get('topic', '').strip()

    if not camera and not person and not topic:
        return json.dumps({'recent': [], 'archived': []})

    def matches(e):
        if camera and _camera_name_from_filename(e['filename']) != camera:
            return False
        if person and not any(p.get('name') == person for p in (e.get('people_in_video') or [])):
            return False
        if topic and not any(t.get('topic') == topic for t in (e.get('detected_topics') or [])):
            return False
        return True

    recent = [e for e in _build_full_event_list(ALERTS_DIR) if matches(e)]
    archived = [e for e in _build_full_event_list(ARCHIVE_DIR) if matches(e)]
    return json.dumps({'recent': recent, 'archived': archived})

@app.route('/api/export_metadata')
@requires_auth
def api_export_metadata():
    fmt = request.args.get('format', 'csv')
    only_filenames = request.args.get('filenames')
    filter_set = set(only_filenames.split(',')) if only_filenames else None

    recent = _build_full_event_list(ALERTS_DIR)
    for e in recent:
        e['archived'] = False
    archived = _build_full_event_list(ARCHIVE_DIR)
    for e in archived:
        e['archived'] = True
    events = recent + archived
    if filter_set is not None:
        events = [e for e in events if e['filename'] in filter_set]
    # Neueste zuerst, über beide Quellen hinweg einheitlich sortiert
    events.sort(key=lambda e: e['datetime'], reverse=True)

    if fmt == 'json':
        resp = Response(json.dumps(events, indent=2, ensure_ascii=False), mimetype='application/json')
        resp.headers['Content-Disposition'] = 'attachment; filename=vigil_export.json'
        return resp

    output = io.StringIO()
    fieldnames = ['filename', 'archived', 'datetime', 'size', 'trigger_class', 'trigger_confidence',
                  'audio_trigger_label', 'audio_trigger_confidence', 'detected_topics', 'people',
                  'unrecognized_face_count', 'ai_description', 'transcript']
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction='ignore')
    writer.writeheader()
    for e in events:
        row = dict(e)
        row['detected_topics'] = '; '.join(
            f"{t.get('topic')} ({t.get('score')}%)" if t.get('score') is not None else str(t.get('topic'))
            for t in (e.get('detected_topics') or [])
        )
        row['people'] = ', '.join(p.get('name', '') for p in (e.get('people_in_video') or []))
        writer.writerow(row)
    resp = Response(output.getvalue(), mimetype='text/csv')
    resp.headers['Content-Disposition'] = 'attachment; filename=vigil_export.csv'
    return resp

def _get_disk_status():
    try:
        total, used, free = shutil.disk_usage(ALERTS_DIR)
        return {
            'total': round(total / (1024 ** 3), 1),
            'used': round(used / (1024 ** 3), 1),
            'free': round(free / (1024 ** 3), 1),
            'percent': round(used / total * 100, 1) if total else 0
        }
    except Exception:
        return {'total': 0, 'used': 0, 'free': 0, 'percent': 0}

_ollama_check_cache = {'ts': 0, 'status': 'disabled'}
OLLAMA_CHECK_TTL = 20  # Sekunden — kein API-Ping bei jedem 3s-Dashboard-Poll

def _check_ollama_status():
    now = time.time()
    if now - _ollama_check_cache['ts'] < OLLAMA_CHECK_TTL:
        return _ollama_check_cache['status']
    settings = load_settings()
    if not settings.get('AI_ANALYSIS_ENABLED'):
        status = 'disabled'
    else:
        url = settings.get('OLLAMA_URL', 'http://localhost:11434').rstrip('/') + '/api/tags'
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                status = 'ok' if resp.status == 200 else 'error'
        except Exception:
            status = 'error'
    _ollama_check_cache['ts'] = now
    _ollama_check_cache['status'] = status
    return status

def get_detailed_system_status():
    """Ermittelt Modell, VRAM, RAM, CPU, GPU sowie Pipeline-/Event-Status für Dashboard + /api/status"""
    settings = load_settings()
    active_version = settings.get('YOLO_VERSION', YOLO_VERSION)
    active_size = settings.get('MODEL_SIZE', MODEL_SIZE)

    if active_version == "v26":
        active_filename = f"yolo26{active_size}.pt"
    elif active_version == "v12":
        active_filename = f"yolo12{active_size}.pt"
    else:
        active_filename = f"yolov10{active_size}.pt"

    formatted_model_name = f"YOLO {active_version} ({active_size})"

    # Gesamtes System (CPU & RAM via psutil)
    cpu_percent = psutil.cpu_percent(interval=None)
    virtual_mem = psutil.virtual_memory()
    ram_total_gb = round(virtual_mem.total / (1024 ** 3), 1)
    ram_used_gb = round(virtual_mem.used / (1024 ** 3), 1)
    ram_percent = virtual_mem.percent

    # GPU / VRAM über nvidia-smi
    gpu_name = "NVIDIA GeForce RTX 5090"
    vram_used = 0.0
    vram_total = 32.6  # Standardwert in GB
    vram_percent = 0.0
    gpu_temp = 35.0
    encoder_util = 0.0
    decoder_util = 0.0

    try:
        cmd = ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,temperature.gpu,utilization.encoder,utilization.decoder", "--format=csv,noheader,nounits"]
        output = subprocess.check_output(cmd, encoding='utf-8').strip().split('\n')[0]
        parts = [p.strip() for p in output.split(',')]
        if len(parts) >= 6:
            gpu_name = parts[0]
            vram_used = round(float(parts[1]) / 1024.0, 1)   # Umrechnung MB -> GB
            vram_total = round(float(parts[2]) / 1024.0, 1)  # Umrechnung MB -> GB
            vram_percent = round((vram_used / vram_total) * 100, 1) if vram_total > 0 else 0.0
            gpu_temp = float(parts[3])
            encoder_util = float(parts[4])
            decoder_util = float(parts[5])
    except Exception:
        pass

    # CPU-Temperatur (falls unter Linux verfügbar)
    cpu_temp = 42.0
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for name, entries in temps.items():
                if 'coretemp' in name.lower() or 'cpu' in name.lower():
                    for entry in entries:
                        if entry.current:
                            cpu_temp = entry.current
                            break
    except Exception:
        pass

    # Worker-Prozesse ermitteln. Seit der Umstellung auf multiprocessing
    # 'spawn' (siehe recorder_pipeline.py — nötig, um den "Cannot
    # re-initialize CUDA in forked subprocess"-Fehler zu vermeiden) zeigen
    # Kindprozesse in der Kommandozeile NICHT mehr "recorder_pipeline.py"
    # oder "forkserver" — spawn-Kinder starten über einen generischen
    # Bootstrap ("--multiprocessing-fork"), der weder den Skriptnamen noch
    # die alte fork-Markierung enthält (bestätigt an einem echten
    # reproduzierten Prozessbaum). Robuster als reines String-Matching auf
    # die Kommandozeile: die tatsächlichen Kindprozesse des Master-Prozesses
    # über psutil ermitteln, unabhängig von deren eigener Kommandozeile —
    # das bricht nicht bei jeder Python-/Multiprocessing-Detailänderung
    # erneut. "--multiprocessing-fork" allein wäre nicht spezifisch genug
    # (jede Python-Multiprocessing-Anwendung auf dem System nutzt das),
    # UND multiprocessing 'spawn' erzeugt neben den echten Worker-Kindern
    # noch einen "resource_tracker"-Hilfsprozess, der hier explizit
    # ausgeschlossen wird.
    enabled_streams = [s["name"] for s in _load_streams_display() if s.get("enabled", False)]
    worker_procs = []
    try:
        master_candidates = [
            p for p in psutil.process_iter(['pid', 'cmdline'])
            if p.info.get('cmdline')
            and any('recorder_pipeline.py' in arg for arg in p.info['cmdline'])
            and not any('multiprocessing' in arg for arg in p.info['cmdline'])
        ]
        for master in master_candidates:
            for child in master.children(recursive=False):
                try:
                    child_cmdline = ' '.join(child.cmdline())
                    if '--multiprocessing-fork' in child_cmdline and 'resource_tracker' not in child_cmdline:
                        if child.cpu_times().user + child.cpu_times().system > 0.5:
                            worker_procs.append(child)
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    worker_procs.sort(key=lambda p: p.create_time())
    processes_data = []

    watch_folder_enabled = bool(load_settings().get("WATCH_FOLDER_ENABLED", False))

    for idx, proc in enumerate(worker_procs):
        try:
            if idx < len(enabled_streams):
                stream_name = enabled_streams[idx]
            elif watch_folder_enabled and idx == len(enabled_streams):
                # Master startet den Watchfolder-Prozess (falls aktiviert)
                # immer NACH allen Kamera-Prozessen -- landet also zuverlässig
                # genau an dieser Position in der nach Erstellungszeit
                # sortierten Liste. Eine Unterscheidung anhand der Kommandozeile
                # selbst ist nicht möglich, multiprocessing.spawn zeigt für
                # jeden Worker dieselbe generische "--multiprocessing-fork"-
                # Zeile, unabhängig von der ursprünglichen Prozessklasse.
                stream_name = "Watchfolder Import"
            else:
                stream_name = f"Worker #{idx + 1}"

            cpu = proc.cpu_percent(interval=None)
            mem = round(proc.memory_info().rss / (1024 ** 2), 1)

            processes_data.append({
                'name': stream_name,
                'pid': proc.pid,
                'status': 'LÄUFT',
                'cpu': round(cpu, 1),
                'ram': mem
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    with _restart_lock:
        restarting = pipeline_restart_status["restarting"]

    # Exakte Datenstruktur, die das Frontend (Dashboard JS) erwartet
    return {
        'cpu': {
            'percent': cpu_percent,
            'temp': cpu_temp
        },
        'ram': {
            'percent': ram_percent,
            'used': ram_used_gb,
            'total': ram_total_gb
        },
        'vram': {
            'percent': vram_percent,
            'used': vram_used,
            'total': vram_total
        },
        'gpu': {
            'temp': gpu_temp,
            'status': 'Normal' if gpu_temp < 80 else 'Warning',
            'encoder_util': encoder_util,
            'decoder_util': decoder_util
        },
        'model_version': active_version,
        'model_size': active_size,
        'model_filename': active_filename,
        'yolo_version': active_version,
        'active_model': formatted_model_name,
        'gpu_name': gpu_name,
        'processes': processes_data,
        'active_count': len(processes_data),
        'pipeline_active': is_pipeline_running(),
        'restarting': restarting,
        'recent_events': build_event_list(ALERTS_DIR),
        'archived_events': build_event_list(ARCHIVE_DIR),
        'recording_states': _read_recording_states(),
        'disk': _get_disk_status(),
        'ollama_status': _check_ollama_status(),
        'thumbnail_interval_ms': int(round(1000 / (load_settings().get('THUMBNAIL_FPS', 1) or 1))),
    }

def _load_streams_display():
    """Frische Kamera-Liste fürs Rendern — nicht die beim web_ui.py-Start
    fixierte STREAMS-Konstante, damit eine gerade gespeicherte Kamera sofort
    in der GUI auftaucht. Live-Vorschau (helpers.py-Thread) und tatsächliche
    Aufnahme (recorder_pipeline.py) brauchen trotzdem ihren jeweiligen
    Prozess-Neustart, um eine neue Kamera wirklich zu bedienen — das kann
    aus einem laufenden Web-Request heraus nicht sauber selbst ausgelöst
    werden (würde die eigene Antwort mit abwürgen).

    WICHTIG: 'enabled' hier ist NUR die Aufnahme-Einstellung aus dem
    Settings-Formular (Video-Checkbox) — NICHT mit dem Live-Vorschau-
    Override vermischen (das ist ein komplett separates Konzept, die
    Live-Vorschau-Kacheln lesen ihren eigenen 'overrides'-Wert direkt aus
    stream_overrides.json). Diese Datei hier hatte das früher vermischt:
    enabled wurde mit dem Override-Zustand überschrieben, wodurch eine
    gerade abgewählte Kamera nach jedem Reload wieder als aktiviert
    angezeigt wurde, obwohl streams.json korrekt gespeichert hatte."""
    try:
        if os.path.exists(STREAMS_F):
            with open(STREAMS_F) as f:
                loaded = json.load(f)
            if isinstance(loaded, list) and loaded:
                return [dict(s) for s in loaded]
    except Exception:
        pass
    return STREAMS

@app.route('/')
@requires_auth
def dashboard():
    overrides = load_overrides()
    settings = load_settings()
    system_status = get_detailed_system_status()

    streams_full = _load_streams_display()
    streams = [s["name"] for s in streams_full]

    # Konfigurierbare Vorschau-Rate (Grid-Thumbnails + Live-View-Lightbox),
    # per Slider in den Settings zwischen 0.5 und 5 fps einstellbar.
    thumbnail_fps = settings.get('THUMBNAIL_FPS', 1) or 1
    thumbnail_interval_ms = int(round(1000 / thumbnail_fps))

    return render_template(
        'dashboard.html',
        streams=streams,
        streams_full=streams_full,
        overrides=overrides,
        settings=settings,
        available_classes=AVAILABLE_CLASSES,
        recent_events=system_status['recent_events'],
        archived_events=system_status['archived_events'],
        pipeline_active=system_status['pipeline_active'],
        pipeline_restarting=system_status['restarting'],
        system_status=system_status,
        csrf_token=CSRF_TOKEN,
        thumbnail_interval_ms=thumbnail_interval_ms
    )

def _trigger_analysis(base_dir, filename):
    settings = load_settings()
    ai_enabled = settings.get('AI_ANALYSIS_ENABLED')
    transcription_enabled = settings.get('TRANSCRIPTION_ENABLED')
    if not ai_enabled and not transcription_enabled:
        return False, "Neither AI video analysis nor transcription is enabled (Settings)."
    basename = os.path.splitext(filename)[0]
    if ai_enabled:
        fs_dir = os.path.join(base_dir, '.thumbs', basename, 'large')
        if (not os.path.isdir(fs_dir) or not glob.glob(os.path.join(fs_dir, '*.jpg'))) and not transcription_enabled:
            return False, "No filmstrip frames available for this video."
        # Fehlende Filmstrip-Frames sind kein harter Fehler, solange Transkription
        # aktiv ist — postprocess.py lässt die Vision-Analyse dann intern einfach
        # leer laufen (ai_analyze.py prüft das selbst) und transkribiert trotzdem.
    try:
        subprocess.Popen([sys.executable, os.path.join(SCRIPT_DIR, 'postprocess.py'), basename, base_dir])
        _event_cache.clear()
        return True, None
    except Exception as e:
        return False, str(e)

@app.route('/analyze/<filename>', methods=['POST'])
@requires_auth
def analyze_video(filename: str):
    _verify_csrf()
    ok, err = _trigger_analysis(ALERTS_DIR, filename)
    return json.dumps({'ok': ok, 'error': err})

def _save_note(base_dir, filename):
    note = request.form.get('note', None)
    rating_raw = request.form.get('rating', None)
    basename = os.path.splitext(filename)[0]
    meta_path = os.path.join(base_dir, f"{basename}.ai.json")
    meta = {}
    if os.path.exists(meta_path):
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except Exception:
            meta = {}
    if note is not None:
        meta['user_note'] = note
    if rating_raw is not None:
        try:
            rating_val = int(rating_raw)
            meta['rating'] = max(0, min(5, rating_val)) if rating_val > 0 else None
        except (TypeError, ValueError):
            pass
    try:
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump(meta, f)
    except Exception as e:
        return False, str(e)

    # XMP-Sidecar direkt mitaktualisieren -- ohne die komplette KI-Analyse
    # neu laufen zu lassen, nur Notiz/Bewertung in die schon vorhandene
    # Beschreibung/Themen-Struktur einhängen.
    if ai_analyze is not None:
        try:
            qualifying_topics = {
                t: s for t, s in (meta.get('topics') or {}).items()
                if s >= ai_analyze.AI_TOPICS_THRESHOLD
            }
            ai_analyze.write_xmp_sidecar(
                basename, base_dir, meta.get('description', ''),
                qualifying_topics, meta.get('user_note', ''), meta.get('rating')
            )
        except Exception as e:
            print(f"⚠️ XMP-Sidecar konnte bei Notiz/Bewertungs-Speicherung nicht aktualisiert werden: {e}")
    _event_cache.clear()
    return True, None

@app.route('/api/note/<filename>', methods=['POST'])
@requires_auth
def save_note(filename: str):
    _verify_csrf()
    ok, err = _save_note(ALERTS_DIR, filename)
    return json.dumps({'ok': ok, 'error': err})

@app.route('/api/note/archive/<filename>', methods=['POST'])
@requires_auth
def save_note_archived(filename: str):
    _verify_csrf()
    ok, err = _save_note(ARCHIVE_DIR, filename)
    return json.dumps({'ok': ok, 'error': err})

@app.route('/analyze/archive/<filename>', methods=['POST'])
@requires_auth
def analyze_archived_video(filename: str):
    _verify_csrf()
    ok, err = _trigger_analysis(ARCHIVE_DIR, filename)
    return json.dumps({'ok': ok, 'error': err})

def _export_folder_name(filename, topic=None):
    """Event_<Kamera>_<Zeitstempel>[ Topic_<Thema>], Dateisystem-sicher
    (auch für den Fall, dass das Ziel eine Windows-SMB-Freigabe ist)."""
    base = os.path.splitext(filename)[0]
    if '_EVENT_' in base:
        camera, _, timestamp = base.partition('_EVENT_')
    else:
        camera, timestamp = base, ''
    name = f"Event_{camera}_{timestamp}" if timestamp else f"Event_{camera}"
    if topic:
        safe_topic = "".join(c for c in topic if c.isalnum() or c in (' ', '-', '_')).strip()
        if safe_topic:
            name += f" Topic_{safe_topic}"
    return "".join(c if c.isalnum() or c in (' ', '_', '-') else '_' for c in name)

def _sanitize_subfolder_name(name):
    """Macht einen vom Nutzer eingegebenen Unterordner-Namen dateisystem-
    sicher — Positivliste statt Einzelfälle abzufangen (robuster gegen
    Umgehungsversuche über Zeichen-Kombinationen, die bei sequenziellem
    Abfangen einzelner Muster durch die Reihenfolge der Operationen
    durchrutschen könnten): nur Buchstaben, Zahlen, Leerzeichen, Bindestrich,
    Unterstrich erlaubt, alles andere (inkl. / \\ . für Pfad-Traversal)
    wird zu einem einzelnen Bindestrich."""
    if not name:
        return ""
    safe = re.sub(r"[^A-Za-z0-9 _-]+", "-", name.strip())
    safe = re.sub(r"-{2,}", "-", safe).strip("- ")
    return safe[:100]


def _run_export(src_dir, filename, dest_root, subfolder=""):
    """Kopiert Video + alle Sidecar-Metadaten + Filmstrip-Ordner eines Events
    in einen eigenen, benannten Unterordner unter dest_root — optional
    zusätzlich in einen gemeinsamen, selbst benannten Gruppen-Unterordner
    genestet (z.B. "Car accident"), wenn mehrere Events zusammen als
    zusammengehörige Gruppe exportiert werden.

    dest_root kann ein lokaler Pfad ODER ein rsync-Remote-Ziel sein
    (user@host:/pfad) — für Remote-Ziele wird bereits eingerichteter
    passwortloser SSH-Zugriff (Public-Key-Auth) vorausgesetzt; das kann diese
    Funktion nicht für euch einrichten, rsync würde sonst nach einem
    Passwort fragen und (da hier kein Terminal angehängt ist) hängen bleiben
    bis der Timeout greift."""
    base = os.path.splitext(filename)[0]
    video_path = os.path.join(src_dir, filename)
    if not os.path.exists(video_path):
        return False, "Video not found."

    topic = None
    ai_path = os.path.join(src_dir, f"{base}.ai.json")
    if os.path.exists(ai_path):
        try:
            with open(ai_path) as f:
                topic = json.load(f).get('top_topic')
        except Exception:
            pass

    safe_subfolder = _sanitize_subfolder_name(subfolder)
    folder_name = _export_folder_name(filename, topic)
    relative_path = f"{safe_subfolder}/{folder_name}" if safe_subfolder else folder_name
    is_remote = ('@' in dest_root and ':' in dest_root) or dest_root.startswith('rsync://')

    settings = load_settings()
    # .get(..., True): fehlt der Schlüssel (alte Settings-Datei vor diesem
    # Feature), gilt das bisherige Verhalten -- alles exportieren.
    include_video = settings.get("EXPORT_INCLUDE_VIDEO", True)
    include_metadata = settings.get("EXPORT_INCLUDE_METADATA", True)
    include_large_thumbs = settings.get("EXPORT_INCLUDE_LARGE_THUMBS", True)
    include_small_thumbs = settings.get("EXPORT_INCLUDE_SMALL_THUMBS", True)

    candidates = []
    if include_video:
        candidates.append(video_path)
    if include_metadata:
        candidates += [
            os.path.join(src_dir, f"{base}.jpg"),
            os.path.join(src_dir, f"{base}.ai.json"),
            os.path.join(src_dir, f"{base}.trigger.json"),
            os.path.join(src_dir, f"{filename}.xmp"),
        ]
    files_to_copy = [p for p in candidates if os.path.exists(p)]
    thumbs_dir = os.path.join(src_dir, '.thumbs', base)
    include_any_thumbs = include_large_thumbs or include_small_thumbs

    if is_remote:
        remote_target = dest_root.rstrip('/') + '/' + relative_path + '/'
        try:
            args = ['rsync', '-a'] + files_to_copy
            if os.path.isdir(thumbs_dir) and include_any_thumbs:
                if not include_large_thumbs:
                    args += ['--exclude', 'large/']
                if not include_small_thumbs:
                    args += ['--exclude', 'small/']
                args.append(thumbs_dir)
            args.append(remote_target)
            result = subprocess.run(args, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                return False, f"rsync failed: {result.stderr.strip()[:300]}"
            return True, relative_path
        except FileNotFoundError:
            return False, "rsync is not installed on this system."
        except subprocess.TimeoutExpired:
            return False, "rsync timed out (5 min) — is the remote destination reachable?"
        except Exception as e:
            return False, str(e)
    else:
        try:
            dest_dir = os.path.join(dest_root, safe_subfolder, folder_name) if safe_subfolder else os.path.join(dest_root, folder_name)
            os.makedirs(dest_dir, exist_ok=True)
            for p in files_to_copy:
                shutil.copy2(p, dest_dir)
            if os.path.isdir(thumbs_dir) and include_any_thumbs:
                skip_dirs = set()
                if not include_large_thumbs:
                    skip_dirs.add('large')
                if not include_small_thumbs:
                    skip_dirs.add('small')
                ignore = shutil.ignore_patterns(*skip_dirs) if skip_dirs else None
                shutil.copytree(thumbs_dir, os.path.join(dest_dir, 'thumbs'), dirs_exist_ok=True, ignore=ignore)
            return True, relative_path
        except Exception as e:
            return False, str(e)

def _delete_video_files(base_dir, filename):
    """Entfernt ein Video komplett samt aller Nebendateien -- dieselbe Logik
    wie /delete/<filename> und /delete_archived/<filename>, hier als eigene
    Funktion, damit der Export-mit-anschließendem-Löschen-Ablauf sie
    wiederverwenden kann, ohne die bestehenden, bereits funktionierenden
    Lösch-Routen anzufassen (Risiko vermeiden, nicht duplizieren)."""
    file_path = os.path.abspath(os.path.join(base_dir, filename))
    if not (file_path.startswith(os.path.abspath(base_dir)) and os.path.exists(file_path)):
        return False, "Video not found."
    try:
        os.remove(file_path)
    except Exception as e:
        return False, str(e)
    _remove_matching_thumbnail(file_path)
    if search_index is not None:
        search_index.remove_event(filename)
    if faces_db is not None:
        faces_db.remove_faces_for_video(filename)
    return True, None


def _export_route_handler(src_dir, filename):
    src_path = os.path.join(src_dir, filename)
    if os.path.exists(os.path.splitext(src_path)[0] + '.recording'):
        return json.dumps({'ok': False, 'error': 'Recording still in progress — wait until it finishes.'})
    settings = load_settings()
    export_dir = (settings.get('EXPORT_DIR') or '').strip()
    if not export_dir:
        return json.dumps({'ok': False, 'error': 'No export folder configured (Settings).'})
    subfolder = request.form.get('subfolder', '')
    ok, result = _run_export(src_dir, filename, export_dir, subfolder)
    if not ok:
        return json.dumps({'ok': False, 'error': result})
    deleted = False
    if settings.get('EXPORT_DELETE_AFTER', False):
        # NUR löschen, nachdem der Export oben bestätigt erfolgreich war —
        # niemals vorher, niemals wenn _run_export einen Fehler zurückgegeben hat.
        del_ok, del_err = _delete_video_files(src_dir, filename)
        if del_ok:
            deleted = True
        else:
            # Export war erfolgreich, nur das Löschen ist fehlgeschlagen — das
            # dem Nutzer klar getrennt mitteilen, nicht als Export-Fehler
            # verschleiern, die Datei liegt ja tatsächlich sicher exportiert vor.
            return json.dumps({'ok': True, 'folder': result, 'deleted': False, 'delete_error': del_err})
    return json.dumps({'ok': True, 'folder': result, 'deleted': deleted})

@app.route('/export/<filename>', methods=['POST'])
@requires_auth
def export_video(filename: str):
    _verify_csrf()
    return _export_route_handler(ALERTS_DIR, filename)

@app.route('/export/archive/<filename>', methods=['POST'])
@requires_auth
def export_archived_video(filename: str):
    _verify_csrf()
    return _export_route_handler(ARCHIVE_DIR, filename)

@app.route('/api/events/<kind>')
@requires_auth
def api_events_page(kind):
    """Für 'Ältere laden' im Dashboard — umgeht den MAX_EVENTS-Deckel gezielt,
    ohne den normalen 3s-Poll teurer zu machen."""
    directory = ALERTS_DIR if kind == 'recent' else ARCHIVE_DIR if kind == 'archived' else None
    if directory is None:
        return "", 404
    try:
        offset = max(0, int(request.args.get('offset', 0)))
    except (TypeError, ValueError):
        offset = 0
    page_size = 50
    files = sorted(glob.glob(os.path.join(directory, '*.mp4')), key=os.path.getmtime, reverse=True)
    page_files = files[offset:offset + page_size]
    events = [e for e in (_event_from_file(f) for f in page_files) if e]
    return json.dumps({'events': events, 'has_more': offset + page_size < len(files)})

@app.route('/api/log')
@requires_auth
def api_log():
    log_path = os.path.join(PROJECT_ROOT, 'logs', 'pipeline_runtime.log')
    try:
        n = min(max(int(request.args.get('lines', 50)), 1), 500)
    except (TypeError, ValueError):
        n = 50
    if not os.path.exists(log_path):
        return json.dumps({'lines': [], 'error': 'Log-Datei nicht gefunden: ' + log_path})
    try:
        with open(log_path, 'r', errors='replace') as f:
            lines = list(deque(f, maxlen=n))
        return json.dumps({'lines': lines})
    except Exception as e:
        return json.dumps({'lines': [], 'error': str(e)})

@app.route('/api/search')
@requires_auth
def api_search():
    query = request.args.get('q', '').strip()
    if not query:
        return json.dumps({'results': []})

    # (filename, base_dir) -> score, damit Text/Semantik- und Personen-Treffer
    # sich nicht duplizieren, wenn beide auf dasselbe Video zeigen.
    matched = {}

    if search_index is not None:
        try:
            for filename, base_dir, description, score in search_index.search(query):
                matched[(filename, base_dir)] = max(matched.get((filename, base_dir), 0), score)
        except Exception as e:
            if faces_db is None:
                return json.dumps({'results': [], 'error': str(e)})

    # Personensuche: Namen gegen die Anfrage matchen (Teilstring, case-
    # insensitive), gefundene Personen -> deren Videos mit reinmischen.
    # Eigener, fester Score-Bonus, unabhängig vom Text/Semantik-Score, damit
    # ein Namenstreffer nie einfach "verschwindet" nur weil die Beschreibung
    # selbst zufällig niedriger bewertet wurde.
    if faces_db is not None:
        q_lower = query.lower()
        try:
            for person in faces_db.list_people():
                if q_lower in (person.get('name') or '').lower():
                    for face in faces_db.get_faces_for_person(person['id']):
                        key = (face['filename'], face['base_dir'])
                        matched[key] = max(matched.get(key, 0), 0.6)
        except Exception:
            pass

    if not matched:
        return json.dumps({'results': []})

    results = []
    for (filename, base_dir), score in matched.items():
        full_path = os.path.join(base_dir, filename)
        if not os.path.exists(full_path):
            continue  # Datei zwischenzeitlich gelöscht, Index noch nicht nachgezogen
        ev = _event_from_file(full_path)
        if ev:
            ev['archived'] = (os.path.abspath(base_dir) == os.path.abspath(ARCHIVE_DIR))
            ev['_search_score'] = score
            results.append(ev)
    results.sort(key=lambda e: e['_search_score'], reverse=True)
    for ev in results:
        del ev['_search_score']
    return json.dumps({'results': results})

@app.route('/api/people_data')
@requires_auth
def api_people_data():
    if faces_db is None:
        return json.dumps({'people': [], 'clusters': {}, 'error': 'Face recognition module not available.'})
    return json.dumps({
        'people': faces_db.list_people(),
        'clusters': faces_db.list_clusters()
    })

@app.route('/api/person_faces/<int:person_id>')
@requires_auth
def api_person_faces(person_id):
    if faces_db is None:
        return json.dumps({'faces': [], 'error': 'Face recognition module not available.'})
    return json.dumps({'faces': faces_db.get_faces_for_person(person_id)})

@app.route('/api/set_representative_face', methods=['POST'])
@requires_auth
def set_representative_face_route():
    _verify_csrf()
    if faces_db is None:
        return json.dumps({'ok': False, 'error': 'Face recognition module not available.'})
    try:
        person_id = int(request.form.get('person_id'))
        face_id = int(request.form.get('face_id'))
    except (TypeError, ValueError):
        return json.dumps({'ok': False, 'error': 'Invalid person_id/face_id.'})
    ok, err = faces_db.set_representative_face(person_id, face_id)
    return json.dumps({'ok': ok, 'error': err})

@app.route('/face_crop/<int:face_id>')
@requires_auth
def face_crop(face_id):
    if faces_db is None:
        abort(404)
    face = faces_db.get_face(face_id)
    if not face:
        abort(404)
    base_dir, crop_path = face['base_dir'], face['crop_path']
    full_path = os.path.abspath(os.path.join(base_dir, crop_path))
    # Sicherheitscheck: der aufgelöste Pfad muss tatsächlich innerhalb ALERTS_DIR
    # oder ARCHIVE_DIR liegen (verhindert Path-Traversal über einen manipulierten
    # base_dir/crop_path-Datensatz)
    if not (full_path.startswith(os.path.abspath(ALERTS_DIR)) or full_path.startswith(os.path.abspath(ARCHIVE_DIR))):
        abort(403)
    if not os.path.exists(full_path):
        abort(404)
    return send_file(full_path)

@app.route('/api/create_person', methods=['POST'])
@requires_auth
def api_create_person():
    _verify_csrf()
    if faces_db is None:
        return json.dumps({'ok': False, 'error': 'Face recognition module not available.'})
    name = request.form.get('name', '').strip()
    face_ids = [int(x) for x in request.form.getlist('face_ids') if x.isdigit()]
    if not name or not face_ids:
        return json.dumps({'ok': False, 'error': 'Name and at least one face are required.'})
    person_id = faces_db.create_person(name, face_ids)
    return json.dumps({'ok': person_id is not None, 'person_id': person_id})

@app.route('/api/assign_to_person', methods=['POST'])
@requires_auth
def api_assign_to_person():
    _verify_csrf()
    if faces_db is None:
        return json.dumps({'ok': False, 'error': 'Face recognition module not available.'})
    try:
        person_id = int(request.form.get('person_id'))
    except (TypeError, ValueError):
        return json.dumps({'ok': False, 'error': 'Invalid person_id.'})
    face_ids = [int(x) for x in request.form.getlist('face_ids') if x.isdigit()]
    faces_db.assign_faces_to_person(person_id, face_ids)
    return json.dumps({'ok': True})

@app.route('/api/unassign_face', methods=['POST'])
@requires_auth
def api_unassign_face():
    _verify_csrf()
    if faces_db is None:
        return json.dumps({'ok': False, 'error': 'Face recognition module not available.'})
    try:
        face_id = int(request.form.get('face_id'))
    except (TypeError, ValueError):
        return json.dumps({'ok': False, 'error': 'Invalid face_id.'})
    faces_db.unassign_face(face_id)
    return json.dumps({'ok': True})

@app.route('/api/unassign_faces_bulk', methods=['POST'])
@requires_auth
def api_unassign_faces_bulk():
    _verify_csrf()
    if faces_db is None:
        return json.dumps({'ok': False, 'error': 'Face recognition module not available.'})
    try:
        face_ids = [int(fid) for fid in request.form.getlist('face_ids')]
    except (TypeError, ValueError):
        return json.dumps({'ok': False, 'error': 'Invalid face_ids.'})
    if not face_ids:
        return json.dumps({'ok': False, 'error': 'No face_ids provided.'})
    faces_db.unassign_faces(face_ids)
    return json.dumps({'ok': True, 'count': len(face_ids)})

@app.route('/api/reject_face', methods=['POST'])
@requires_auth
def api_reject_face():
    _verify_csrf()
    if faces_db is None:
        return json.dumps({'ok': False, 'error': 'Face recognition module not available.'})
    try:
        face_id = int(request.form.get('face_id'))
    except (TypeError, ValueError):
        return json.dumps({'ok': False, 'error': 'Invalid face_id.'})
    faces_db.reject_face(face_id)
    return json.dumps({'ok': True})

@app.route('/api/reject_faces_bulk', methods=['POST'])
@requires_auth
def api_reject_faces_bulk():
    _verify_csrf()
    if faces_db is None:
        return json.dumps({'ok': False, 'error': 'Face recognition module not available.'})
    try:
        face_ids = [int(fid) for fid in request.form.getlist('face_ids')]
    except (TypeError, ValueError):
        return json.dumps({'ok': False, 'error': 'Invalid face_ids.'})
    if not face_ids:
        return json.dumps({'ok': False, 'error': 'No face_ids provided.'})
    faces_db.reject_faces(face_ids)
    return json.dumps({'ok': True, 'count': len(face_ids)})

@app.route('/api/rename_person', methods=['POST'])
@requires_auth
def api_rename_person():
    _verify_csrf()
    if faces_db is None:
        return json.dumps({'ok': False, 'error': 'Face recognition module not available.'})
    try:
        person_id = int(request.form.get('person_id'))
    except (TypeError, ValueError):
        return json.dumps({'ok': False, 'error': 'Invalid person_id.'})
    new_name = request.form.get('name', '').strip()
    if not new_name:
        return json.dumps({'ok': False, 'error': 'Name cannot be empty.'})
    faces_db.rename_person(person_id, new_name)
    return json.dumps({'ok': True})

@app.route('/api/delete_person', methods=['POST'])
@requires_auth
def api_delete_person():
    _verify_csrf()
    if faces_db is None:
        return json.dumps({'ok': False, 'error': 'Face recognition module not available.'})
    try:
        person_id = int(request.form.get('person_id'))
    except (TypeError, ValueError):
        return json.dumps({'ok': False, 'error': 'Invalid person_id.'})
    faces_db.delete_person(person_id)
    return json.dumps({'ok': True})

@app.route('/api/delete_person_permanently', methods=['POST'])
@requires_auth
def api_delete_person_permanently():
    _verify_csrf()
    if faces_db is None:
        return json.dumps({'ok': False, 'error': 'Face recognition module not available.'})
    try:
        person_id = int(request.form.get('person_id'))
    except (TypeError, ValueError):
        return json.dumps({'ok': False, 'error': 'Invalid person_id.'})
    ok, err = faces_db.delete_person_permanently(person_id)
    return json.dumps({'ok': ok, 'error': err})

@app.route('/api/recluster_faces', methods=['POST'])
@requires_auth
def api_recluster_faces():
    _verify_csrf()
    try:
        result = subprocess.run(
            [sys.executable, os.path.join(SCRIPT_DIR, 'cluster_faces.py')],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            return json.dumps({'ok': False, 'error': result.stderr.strip()[:300]})
        return json.dumps({'ok': True, 'output': result.stdout.strip()[-500:]})
    except subprocess.TimeoutExpired:
        return json.dumps({'ok': False, 'error': 'Clustering timed out (60s).'})
    except Exception as e:
        return json.dumps({'ok': False, 'error': str(e)})

@app.route('/api/ignore_cluster', methods=['POST'])
@requires_auth
def api_ignore_cluster():
    _verify_csrf()
    if faces_db is None:
        return json.dumps({'ok': False, 'error': 'Face recognition module not available.'})
    try:
        cluster_id = int(request.form.get('cluster_id', ''))
    except (TypeError, ValueError):
        return json.dumps({'ok': False, 'error': 'Invalid cluster_id.'})
    ok = faces_db.ignore_cluster(cluster_id)
    return json.dumps({'ok': ok})

@app.route('/api/unignore_cluster', methods=['POST'])
@requires_auth
def api_unignore_cluster():
    _verify_csrf()
    if faces_db is None:
        return json.dumps({'ok': False, 'error': 'Face recognition module not available.'})
    try:
        cluster_id = int(request.form.get('cluster_id', ''))
    except (TypeError, ValueError):
        return json.dumps({'ok': False, 'error': 'Invalid cluster_id.'})
    ok = faces_db.unignore_cluster(cluster_id)
    return json.dumps({'ok': ok})

@app.route('/api/cleanup_orphaned_faces', methods=['POST'])
@requires_auth
def api_cleanup_orphaned_faces():
    _verify_csrf()
    if faces_db is None:
        return json.dumps({'ok': False, 'error': 'Face recognition module not available.'})
    removed = faces_db.remove_orphaned_faces()
    return json.dumps({'ok': True, 'removed': removed})

@app.route('/health')
def health():
    """Bewusst OHNE @requires_auth: für externe Watchdogs/Monitoring gedacht.
    Liefert nur 'läuft', keine sensiblen Daten."""
    return {"status": "ok"}, 200

@app.route('/api/watchfolder_live_status')
@requires_auth
def api_watchfolder_live_status():
    """Ob gerade eine Modus-1-Live-Quelle läuft -- die einzige Möglichkeit,
    das im Dashboard überhaupt zu sehen, da es sich um einen dynamisch vom
    Watchfolder-Prozess erzeugten CameraAgent handelt, der nie in
    streams.json steht und daher nirgendwo sonst auftaucht."""
    status_path = os.path.join(ALERTS_DIR, '.watchfolder_live_status.json')
    if not os.path.exists(status_path):
        return json.dumps({'active': []})
    try:
        with open(status_path) as f:
            data = json.load(f)
        # Veraltete Status-Datei (Watchfolder-Prozess vermutlich abgestürzt,
        # ohne sauber aufzuräumen) nicht als "aktiv" ausgeben -- eine
        # gesunde Schleife schreibt alle paar Sekunden neu.
        if time.time() - data.get('updated_at', 0) > 30:
            return json.dumps({'active': []})
        return json.dumps({'active': data.get('active', [])})
    except Exception:
        return json.dumps({'active': []})

@app.route('/api/storage_status')
@requires_auth
def api_storage_status():
    """Speicherplatz UND Schreibrechte für alle konfigurierbaren Ordner in
    einem Aufruf -- Frühwarnung vor voller Platte, unabhängig davon, ob der
    Ordner selbst schon existiert oder momentan leer ist (disk_usage prüft
    das zugrundeliegende Dateisystem, nicht den Ordnerinhalt -- das ist
    genau das, was für "läuft die Platte voll" zählt, und ist im Gegensatz
    zu einer rekursiven Ordnergrößen-Berechnung auch bei sehr vielen
    Aufnahmen sofort schnell)."""
    settings = load_settings()
    folders = {
        'Recordings (ALERTS_DIR)': (settings.get('ALERTS_DIR_OVERRIDE') or '').strip() or ALERTS_DIR,
        'Export destination': (settings.get('EXPORT_DIR') or '').strip(),
        'Watchfolder (import source)': (settings.get('WATCH_FOLDER_PATH') or '').strip(),
    }
    results = []
    for label, path in folders.items():
        if not path:
            results.append({'label': label, 'path': None, 'configured': False})
            continue
        entry = {'label': label, 'path': path, 'configured': True}
        # rsync-Remote-Ziele (user@host:/pfad) haben kein lokales Dateisystem
        # zum Prüfen -- als solches kennzeichnen statt einen Fehler zu zeigen.
        if '@' in path and ':' in path and not os.path.isabs(path):
            entry['remote'] = True
            results.append(entry)
            continue
        entry['remote'] = False
        try:
            os.makedirs(path, exist_ok=True)
            total, used, free = shutil.disk_usage(path)
            entry['total_bytes'] = total
            entry['free_bytes'] = free
            entry['used_percent'] = round((used / total) * 100, 1) if total else None
            test_file = os.path.join(path, '.vigil_write_test')
            try:
                with open(test_file, 'w') as tf:
                    tf.write('ok')
                os.remove(test_file)
                entry['writable'] = True
            except Exception:
                entry['writable'] = False
        except Exception as e:
            entry['error'] = str(e)
            entry['writable'] = False
        results.append(entry)
    return json.dumps({'folders': results})

@app.route('/api/status')
@requires_auth
def api_status():
    return json.dumps(get_detailed_system_status())

@app.route('/thumbnail/<stream_name>')
@requires_auth
def get_thumbnail(stream_name):
    if stream_name in LATEST_FRAMES:
        return Response(LATEST_FRAMES[stream_name], mimetype='image/jpeg')
    return Response("", status=204)

@app.route('/stream/<stream_name>')
@requires_auth
def video_stream(stream_name):
    """Echter MJPEG-Push-Stream für die Live-View-Lightbox, statt Client-seitigem
    Bild-Polling alle 250ms."""
    def generate():
        boundary = b'--frame'
        while True:
            frame = LATEST_FRAMES.get(stream_name)
            if frame:
                yield (boundary + b'\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            time.sleep(0.1)  # ~10 fps Server-Push
    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/start', methods=['POST'])
@requires_auth
def start_pipeline():
    _verify_csrf()
    subprocess.Popen(
        ['/bin/bash', os.path.join(PROJECT_ROOT, 'start_detached.sh')],
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    return json.dumps({'ok': True})

@app.route('/stop', methods=['POST'])
@requires_auth
def stop_pipeline():
    _verify_csrf()
    subprocess.run(
        ['/bin/bash', os.path.join(PROJECT_ROOT, 'stop.sh')],
        cwd=PROJECT_ROOT
    )
    return json.dumps({'ok': True})

@app.route('/toggle/<name>', methods=['POST'])
@requires_auth
def toggle_stream(name):
    _verify_csrf()
    overrides = load_overrides()
    overrides[name] = 'ON' if overrides.get(name, 'OFF') == 'OFF' else 'OFF'
    save_overrides(overrides)
    return json.dumps({'ok': True, 'state': overrides[name]})

# --- Live-Ansicht (HLS) + manuelle Notruf-Aufnahme ---
# Beide laufen als eigene, vom Erkennungs-Pipeline-Prozess komplett unabhängige
# ffmpeg-Subprozesse, die direkt von der Kamera-URL lesen — nginx-rtmp
# unterstützt mehrere gleichzeitige Clients auf demselben Stream problemlos.
_live_view_procs = {}      # camera_name -> subprocess.Popen
_manual_record_procs = {}  # recording_id -> {'proc', 'camera', 'path', 'filename'}
LIVE_HLS_DIR = os.path.join(PROJECT_ROOT, '.live_hls')
os.makedirs(LIVE_HLS_DIR, exist_ok=True)

def _get_stream_url(camera_name):
    for s in _load_streams_display():
        if s['name'] == camera_name:
            return s.get('url')
    return None

def _stop_proc(proc, timeout=5):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()

def _start_live_ffmpeg(url, m3u8_path, log_path, use_nvenc):
    """Startet den ffmpeg-Prozess fürs Live-HLS. NVENC (Hardware) passt zur
    restlichen Maschine (vigils eigene Aufnahme nutzt schon NVENC, Axels
    exec_push-Skript nutzt Intel QuickSync) — Software-Encoding (libx264)
    ist der garantiert funktionierende Fallback für Maschinen ohne
    NVIDIA-GPU."""
    log_f = open(log_path, 'w')
    video_args = (
        ['-c:v', 'h264_nvenc', '-preset', 'p1', '-tune', 'ull']
        if use_nvenc else
        ['-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'zerolatency']
    )
    return subprocess.Popen([
        'ffmpeg',
        # Axels nginx-rtmp-Config hat "hls_continuous on" explizit, um
        # Zeitstempel-Sprünge in der Quelle zu ignorieren — derselbe
        # Robustheits-Gedanke hier: genpts rekonstruiert fehlende/kaputte
        # PTS-Werte, discardcorrupt verwirft beschädigte Frames statt
        # den ganzen Stream daran hängenzulassen.
        '-fflags', '+genpts+discardcorrupt',
        '-i', url,
    ] + video_args + [
        '-c:a', 'aac', '-ac', '2',
        '-avoid_negative_ts', 'make_zero',
        '-f', 'hls', '-hls_time', '2', '-hls_list_size', '5',
        '-hls_flags', 'delete_segments+omit_endlist',
        m3u8_path
    ], stdout=log_f, stderr=subprocess.STDOUT)

@app.route('/api/live_start/<camera_name>', methods=['POST'])
@requires_auth
def api_live_start(camera_name):
    _verify_csrf()
    url = _get_stream_url(camera_name)
    if not url:
        return json.dumps({'ok': False, 'error': 'Unknown camera or no URL configured.'})

    existing = _live_view_procs.get(camera_name)
    if existing and existing.poll() is None:
        return json.dumps({'ok': True, 'url': f'/live_hls/{camera_name}/stream.m3u8'})

    cam_dir = os.path.join(LIVE_HLS_DIR, camera_name)
    os.makedirs(cam_dir, exist_ok=True)
    for old in glob.glob(os.path.join(cam_dir, '*')):
        try:
            os.remove(old)
        except Exception:
            pass

    m3u8_path = os.path.join(cam_dir, 'stream.m3u8')
    log_path = os.path.join(cam_dir, 'ffmpeg.log')
    try:
        proc = _start_live_ffmpeg(url, m3u8_path, log_path, use_nvenc=True)
    except Exception as e:
        return json.dumps({'ok': False, 'error': str(e)})
    _live_view_procs[camera_name] = proc

    # Ohne dieses Warten meldete die Route sofort "ok: true", bevor ffmpeg
    # überhaupt Zeit hatte, die erste Playlist-Datei zu schreiben — der
    # Browser bekam dann eine URL, hinter der (noch) nichts lag, und
    # scheiterte still. Kurzes Polling auf das tatsächliche Erscheinen der
    # Datei ODER einen frühen Absturz des ffmpeg-Prozesses, damit die
    # Antwort den echten Zustand widerspiegelt.
    tried_software_fallback = False
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            if not tried_software_fallback:
                # NVENC evtl. auf dieser Maschine nicht verfügbar (kein
                # NVIDIA-GPU, oder Treiber-Problem) — automatisch mit
                # Software-Encoding erneut versuchen, statt komplett
                # aufzugeben. Deckt "soll auch auf weniger krassen
                # Maschinen laufen" ab, ohne manuelles Umschalten.
                tried_software_fallback = True
                proc = _start_live_ffmpeg(url, m3u8_path, log_path, use_nvenc=False)
                _live_view_procs[camera_name] = proc
                deadline = time.time() + 15
                continue
            try:
                with open(log_path) as f:
                    tail = f.read()[-500:]
            except Exception:
                tail = ''
            return json.dumps({'ok': False, 'error': f'ffmpeg exited immediately: {tail}' if tail else 'ffmpeg exited immediately.'})
        if os.path.exists(m3u8_path):
            return json.dumps({'ok': True, 'url': f'/live_hls/{camera_name}/stream.m3u8'})
        time.sleep(0.2)

    _stop_proc(proc)
    _live_view_procs.pop(camera_name, None)
    return json.dumps({'ok': False, 'error': 'Timed out waiting for the stream to start — camera may be unreachable.'})

@app.route('/api/live_stop/<camera_name>', methods=['POST'])
@requires_auth
def api_live_stop(camera_name):
    _verify_csrf()
    _stop_proc(_live_view_procs.pop(camera_name, None))
    return json.dumps({'ok': True})

@app.route('/live_hls/<camera_name>/<path:filename>')
@requires_auth
def serve_live_hls(camera_name, filename):
    cam_dir = os.path.join(LIVE_HLS_DIR, camera_name)
    full_path = os.path.abspath(os.path.join(cam_dir, filename))
    if not full_path.startswith(os.path.abspath(cam_dir) + os.sep) or not os.path.exists(full_path):
        abort(404)
    mimetype = 'application/vnd.apple.mpegurl' if filename.endswith('.m3u8') else 'video/mp2t'
    resp = send_file(full_path, mimetype=mimetype, conditional=True)
    # HLS-Wiedergabe soll sich immer die aktuellste Playlist/Segmente holen,
    # kein Browser-Caching zwischenschalten wie bei fertigen Aufnahmen.
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

@app.route('/api/manual_record/start/<camera_name>', methods=['POST'])
@requires_auth
def api_manual_record_start(camera_name):
    _verify_csrf()
    # Nur eine manuelle Aufnahme gleichzeitig — nicht nur UI-seitig verhindert
    # (ein Button, der ausgegraut wird), sondern auch hier serverseitig
    # durchgesetzt, damit z.B. zwei offene Browser-Tabs das nicht umgehen
    # können. Tote Prozesse (bereits von selbst beendet) räumen wir dabei
    # gleich mit auf, statt fälschlich zu blockieren.
    for rid, entry in list(_manual_record_procs.items()):
        if entry['proc'].poll() is None:
            return json.dumps({'ok': False, 'error': f"Already recording {entry['camera']} — stop that first."})
        _manual_record_procs.pop(rid, None)

    url = _get_stream_url(camera_name)
    if not url:
        return json.dumps({'ok': False, 'error': 'Unknown camera or no URL configured.'})

    ts_str = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f'{camera_name}_MANUAL_{ts_str}.mp4'
    file_path = os.path.join(ALERTS_DIR, filename)

    try:
        proc = subprocess.Popen([
            'ffmpeg', '-i', url,
            '-c:v', 'libx264', '-preset', 'veryfast',
            '-c:a', 'aac',
            '-movflags', '+faststart',
            file_path
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return json.dumps({'ok': False, 'error': str(e)})

    recording_id = str(uuid.uuid4())
    _manual_record_procs[recording_id] = {
        'proc': proc, 'camera': camera_name, 'path': file_path, 'filename': filename
    }

    # Sidecar-Trigger-JSON mit demselben Mechanismus wie bei normalen
    # Erkennungs-Aufnahmen (siehe _event_from_file) — das Dashboard zeigt bei
    # manual=True "Manual Record" statt einer Erkennungs-Konfidenz an.
    trigger_path = os.path.splitext(file_path)[0] + '.trigger.json'
    with open(trigger_path, 'w') as f:
        json.dump({'manual': True}, f)

    return json.dumps({'ok': True, 'recording_id': recording_id})

@app.route('/api/manual_record/stop/<recording_id>', methods=['POST'])
@requires_auth
def api_manual_record_stop(recording_id):
    _verify_csrf()
    entry = _manual_record_procs.pop(recording_id, None)
    if not entry:
        return json.dumps({'ok': False, 'error': 'No such active recording.'})
    _stop_proc(entry['proc'])
    _event_cache.clear()
    return json.dumps({'ok': True, 'filename': entry['filename']})

def _clamp(value, lo, hi):
    return max(lo, min(hi, value))

@app.route('/save_settings', methods=['POST'])
@requires_auth
def save_pipeline_settings():
    _verify_csrf()

    # Server-seitige Validierung/Clamping: verhindert unsinnige/negative Werte,
    # auch falls das Formular umgangen oder manipuliert wird.
    try:
        target_fps = int(request.form.get('TARGET_FPS', 30))
    except (TypeError, ValueError):
        target_fps = 30
    try:
        confidence = float(request.form.get('CONFIDENCE_THRESHOLD', 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    try:
        pre_roll = int(request.form.get('PRE_ROLL_SEC', 10))
    except (TypeError, ValueError):
        pre_roll = 10
    try:
        post_roll = int(request.form.get('POST_ROLL_SEC', 30))
    except (TypeError, ValueError):
        post_roll = 30
    try:
        thumbnail_fps = float(request.form.get('THUMBNAIL_FPS', 1))
    except (TypeError, ValueError):
        thumbnail_fps = 1.0
    try:
        retention_days = int(request.form.get('RETENTION_DAYS', 0))
    except (TypeError, ValueError):
        retention_days = 0
    try:
        filmstrip_count = int(request.form.get('FILMSTRIP_COUNT', 0))
    except (TypeError, ValueError):
        filmstrip_count = 0
    try:
        filmstrip_interval = float(request.form.get('FILMSTRIP_INTERVAL_SEC', 2.0))
    except (TypeError, ValueError):
        filmstrip_interval = 2.0
    try:
        ai_max_frames = int(request.form.get('AI_ANALYZE_MAX_FRAMES', 12))
    except (TypeError, ValueError):
        ai_max_frames = 12
    try:
        ollama_context_size = int(request.form.get('OLLAMA_CONTEXT_SIZE', 0))
    except (TypeError, ValueError):
        ollama_context_size = 0
    try:
        audio_threshold = float(request.form.get('AUDIO_TRIGGER_THRESHOLD', 0.3))
    except (TypeError, ValueError):
        audio_threshold = 0.3
    try:
        audio_interval = float(request.form.get('AUDIO_TRIGGER_INTERVAL_SEC', 2.0))
    except (TypeError, ValueError):
        audio_interval = 2.0
    audio_categories = [
        line.strip() for line in request.form.get('AUDIO_TRIGGER_CATEGORIES', '').splitlines()
        if line.strip()
    ][:20]  # Sicherheitsdecke — 20 Kategorien sind mehr als genug, jede kostet einen CLAP-Vergleich pro Durchlauf
    try:
        topics_threshold = float(request.form.get('AI_TOPICS_THRESHOLD', 50))
    except (TypeError, ValueError):
        topics_threshold = 50
    try:
        watch_folder_stability = float(request.form.get('WATCH_FOLDER_STABILITY_SEC', 5))
    except (TypeError, ValueError):
        watch_folder_stability = 5
    try:
        mqtt_port = int(request.form.get('MQTT_PORT', 1883))
    except (TypeError, ValueError):
        mqtt_port = 1883
    try:
        pose_fall_angle = float(request.form.get('POSE_FALL_ANGLE_THRESHOLD', 55))
    except (TypeError, ValueError):
        pose_fall_angle = 55
    try:
        pose_loitering_seconds = float(request.form.get('POSE_LOITERING_SECONDS', 30))
    except (TypeError, ValueError):
        pose_loitering_seconds = 30
    try:
        face_min_confidence = float(request.form.get('FACE_MIN_CONFIDENCE', 0.5))
    except (TypeError, ValueError):
        face_min_confidence = 0.5
    try:
        face_known_threshold = float(request.form.get('FACE_KNOWN_PERSON_THRESHOLD', 0.5))
    except (TypeError, ValueError):
        face_known_threshold = 0.5
    try:
        face_cluster_eps = float(request.form.get('FACE_CLUSTER_EPS', 0.4))
    except (TypeError, ValueError):
        face_cluster_eps = 0.4
    try:
        face_cluster_min_samples = int(request.form.get('FACE_CLUSTER_MIN_SAMPLES', 2))
    except (TypeError, ValueError):
        face_cluster_min_samples = 2
    ai_topics = [
        line.strip() for line in request.form.get('AI_TOPICS', '').splitlines()
        if line.strip()
    ][:15]  # Sicherheitsdecke — jedes Thema kostet einen Ollama-Vergleich pro Analyse

    old_settings = load_settings()

    settings = {
        "YOLO_VERSION": request.form.get('YOLO_VERSION', 'v26'),
        "MODEL_SIZE": request.form.get('MODEL_SIZE', 'x'),
        "TARGET_FPS": _clamp(target_fps, 1, 60),
        "CONFIDENCE_THRESHOLD": round(_clamp(confidence, 0.05, 1.0), 2),
        "PRE_ROLL_SEC": _clamp(pre_roll, 0, 120),
        "POST_ROLL_SEC": _clamp(post_roll, 0, 300),
        "DETECTION_CLASSES": [int(x) for x in request.form.getlist('DETECTION_CLASSES')] or [0],
        "THUMBNAIL_FPS": round(_clamp(thumbnail_fps, 0.5, 5.0), 1),
        "THEME": request.form.get('THEME', 'dark') if request.form.get('THEME') in ('dark', 'light') else 'dark',
        "RETENTION_DAYS": _clamp(retention_days, 0, 365),
        "FILMSTRIP_COUNT": _clamp(filmstrip_count, 0, 2000),
        "FILMSTRIP_INTERVAL_SEC": round(_clamp(filmstrip_interval, 0.5, 30.0), 1),
        "AI_ANALYSIS_ENABLED": request.form.get('AI_ANALYSIS_ENABLED') == 'on',
        "OLLAMA_URL": request.form.get('OLLAMA_URL', 'http://localhost:11434').strip() or 'http://localhost:11434',
        "OLLAMA_VISION_MODEL": request.form.get('OLLAMA_VISION_MODEL', 'llava:latest').strip() or 'llava:latest',
        "AI_ANALYZE_MAX_FRAMES": _clamp(ai_max_frames, 1, 64),
        "OLLAMA_CONTEXT_SIZE": _clamp(ollama_context_size, 0, 262144),
        "SHOW_DETECTION_BOXES": request.form.get('SHOW_DETECTION_BOXES') == 'on',
        "AUDIO_TRIGGER_ENABLED": request.form.get('AUDIO_TRIGGER_ENABLED') == 'on',
        "AUDIO_TRIGGER_CATEGORIES": audio_categories,
        "AUDIO_TRIGGER_THRESHOLD": round(_clamp(audio_threshold, 0.05, 0.95), 2),
        "AUDIO_TRIGGER_INTERVAL_SEC": round(_clamp(audio_interval, 0.5, 30.0), 1),
        "AI_TOPICS_ENABLED": request.form.get('AI_TOPICS_ENABLED') == 'on',
        "AI_TOPICS": ai_topics,
        "AI_TOPICS_THRESHOLD": round(_clamp(topics_threshold, 0, 100), 0),
        "EXPORT_DIR": request.form.get('EXPORT_DIR', '').strip(),
        "EXPORT_INCLUDE_VIDEO": request.form.get('EXPORT_INCLUDE_VIDEO') == 'on',
        "EXPORT_INCLUDE_METADATA": request.form.get('EXPORT_INCLUDE_METADATA') == 'on',
        "EXPORT_INCLUDE_LARGE_THUMBS": request.form.get('EXPORT_INCLUDE_LARGE_THUMBS') == 'on',
        "EXPORT_INCLUDE_SMALL_THUMBS": request.form.get('EXPORT_INCLUDE_SMALL_THUMBS') == 'on',
        "EXPORT_DELETE_AFTER": request.form.get('EXPORT_DELETE_AFTER') == 'on',
        "MQTT_ENABLED": request.form.get('MQTT_ENABLED') == 'on',
        "MQTT_BROKER": request.form.get('MQTT_BROKER', '').strip(),
        "MQTT_PORT": _clamp(mqtt_port, 1, 65535),
        "MQTT_USERNAME": request.form.get('MQTT_USERNAME', '').strip(),
        "MQTT_PASSWORD": request.form.get('MQTT_PASSWORD', ''),
        "MQTT_TOPIC_PREFIX": (request.form.get('MQTT_TOPIC_PREFIX', 'vigil').strip() or 'vigil'),
        "MQTT_HA_DISCOVERY": request.form.get('MQTT_HA_DISCOVERY') == 'on',
        "AGENT_WEBHOOK_URL": request.form.get('AGENT_WEBHOOK_URL', '').strip(),
        "AGENT_WEBHOOK_ANOMALY_ONLY": request.form.get('AGENT_WEBHOOK_ANOMALY_ONLY') == 'on',
        "ANOMALY_DETECTION_ENABLED": request.form.get('ANOMALY_DETECTION_ENABLED') == 'on',
        "POSE_ESTIMATION_ENABLED": request.form.get('POSE_ESTIMATION_ENABLED') == 'on',
        "POSE_FALL_ANGLE_THRESHOLD": _clamp(pose_fall_angle, 20, 85),
        "POSE_RAISED_HANDS_ENABLED": request.form.get('POSE_RAISED_HANDS_ENABLED') == 'on',
        "POSE_LOITERING_ENABLED": request.form.get('POSE_LOITERING_ENABLED') == 'on',
        "POSE_LOITERING_SECONDS": _clamp(pose_loitering_seconds, 5, 600),
        "POSE_MOVEMENT_ENABLED": request.form.get('POSE_MOVEMENT_ENABLED') == 'on',
        "POSE_PROXIMITY_ENABLED": request.form.get('POSE_PROXIMITY_ENABLED') == 'on',
        "POSE_GAZE_ENABLED": request.form.get('POSE_GAZE_ENABLED') == 'on',
        "POSE_POINTING_ENABLED": request.form.get('POSE_POINTING_ENABLED') == 'on',
        "TRANSCRIPTION_ENABLED": request.form.get('TRANSCRIPTION_ENABLED') == 'on',
        "WHISPER_MODEL_SIZE": request.form.get('WHISPER_MODEL_SIZE', 'small') if request.form.get('WHISPER_MODEL_SIZE') in ('tiny', 'base', 'small', 'medium', 'large-v3') else 'small',
        "TRANSCRIPTION_LANGUAGE": request.form.get('TRANSCRIPTION_LANGUAGE', '').strip(),
        "FACE_RECOGNITION_ENABLED": request.form.get('FACE_RECOGNITION_ENABLED') == 'on',
        "FACE_MODEL_PACK": request.form.get('FACE_MODEL_PACK', 'buffalo_s') if request.form.get('FACE_MODEL_PACK') in ('buffalo_s', 'buffalo_m', 'buffalo_l', 'antelopev2') else 'buffalo_s',
        "FACE_MIN_CONFIDENCE": round(_clamp(face_min_confidence, 0.1, 0.95), 2),
        "FACE_KNOWN_PERSON_THRESHOLD": round(_clamp(face_known_threshold, 0.1, 0.95), 2),
        "FACE_CLUSTER_EPS": round(_clamp(face_cluster_eps, 0.1, 0.9), 2),
        "FACE_CLUSTER_MIN_SAMPLES": _clamp(face_cluster_min_samples, 2, 10),
        "WATCH_FOLDER_ENABLED": request.form.get('WATCH_FOLDER_ENABLED') == 'on',
        "WATCH_FOLDER_PATH": request.form.get('WATCH_FOLDER_PATH', '').strip(),
        "WATCH_FOLDER_SOURCE_NAME": (request.form.get('WATCH_FOLDER_SOURCE_NAME', 'Import').strip() or 'Import'),
        "WATCH_FOLDER_STABILITY_SEC": _clamp(watch_folder_stability, 1, 300),
        "WATCH_FOLDER_DELETE_SOURCE": request.form.get('WATCH_FOLDER_DELETE_SOURCE') == 'on',
        "WATCH_FOLDER_RUN_DETECTION": request.form.get('WATCH_FOLDER_RUN_DETECTION') == 'on',
        "WATCH_FOLDER_LIVE_MODE_ENABLED": request.form.get('WATCH_FOLDER_LIVE_MODE_ENABLED') == 'on',
        "ALERTS_DIR_OVERRIDE": request.form.get('ALERTS_DIR_OVERRIDE', '').strip(),
    }

    with open(SETTINGS_F, 'w') as f:
        json.dump(settings, f, indent=4)

    # Nur neu starten, wenn sich tatsächlich pipeline-relevante Werte geändert
    # haben — THUMBNAIL_FPS ist eine reine Anzeige-Einstellung fürs Dashboard
    # und würde sonst unnötig einen Neustart auslösen.
    PIPELINE_RELEVANT_KEYS = (
        "YOLO_VERSION", "MODEL_SIZE", "TARGET_FPS", "CONFIDENCE_THRESHOLD",
        "PRE_ROLL_SEC", "POST_ROLL_SEC", "DETECTION_CLASSES", "WATCH_FOLDER_ENABLED",
        "ALERTS_DIR_OVERRIDE"
    )
    pipeline_relevant_changed = any(
        old_settings.get(k) != settings.get(k) for k in PIPELINE_RELEVANT_KEYS
    )

    restarted = False
    if pipeline_relevant_changed and is_pipeline_running():
        threading.Thread(target=_restart_pipeline_background, daemon=True).start()
        restarted = True

    return json.dumps({'ok': True, 'restarted': restarted, 'theme': settings['THEME']})

@app.route('/save_streams', methods=['POST'])
@requires_auth
def save_streams():
    _verify_csrf()
    names = request.form.getlist('stream_name')
    urls = request.form.getlist('stream_url')
    # Ein Hidden-Feld pro Zeile (per Checkbox-onchange auf '0'/'1' gesetzt),
    # NICHT positions-/index-basiert — bleibt so auch nach dynamischem
    # Hinzufügen/Entfernen von Zeilen im JS korrekt korreliert.
    enabled_flags = request.form.getlist('stream_enabled_flag')
    audio_enabled_flags = request.form.getlist('stream_audio_enabled_flag')

    new_streams = []
    seen_names = set()
    error = None
    for name, url, flag, audio_flag in zip(names, urls, enabled_flags, audio_enabled_flags):
        name = name.strip()
        url = url.strip()
        if not name or not url:
            continue  # leere Zeile (z.B. gerade erst per "+ Kamera" hinzugefügt, noch nicht ausgefüllt) überspringen
        if name in seen_names:
            error = f"Doppelter Kamera-Name: '{name}' — Namen müssen eindeutig sein."
            break
        seen_names.add(name)
        new_streams.append({
            "name": name,
            "url": url,
            "enabled": flag == '1',
            "audio_enabled": audio_flag == '1',
            "type": "VIDEO"
        })

    if error:
        return json.dumps({'ok': False, 'error': error})
    if not new_streams:
        return json.dumps({'ok': False, 'error': 'At least one camera with a name and URL is required.'})

    try:
        with open(STREAMS_F, 'w') as f:
            json.dump(new_streams, f, indent=2)
    except Exception as e:
        return json.dumps({'ok': False, 'error': f'Could not save: {e}'})

    # Kamera-Liste ist immer pipeline-relevant (neue/entfernte CameraAgent-Prozesse) —
    # anders als die meisten Settings in save_settings() gibt es hier keinen
    # "live ohne Neustart"-Fall.
    restarted = False
    if is_pipeline_running():
        threading.Thread(target=_restart_pipeline_background, daemon=True).start()
        restarted = True

    overrides = load_overrides()
    display_streams = [
        {'name': s['name'], 'override_on': overrides.get(s['name'], 'ON') == 'ON', 'enabled': s.get('enabled', False)}
        for s in new_streams
    ]
    return json.dumps({'ok': True, 'restarted': restarted, 'streams': display_streams})

def _remove_matching_thumbnail(video_path):
    """Löscht Trigger-Screenshot, Filmstrip-Ordner und AI-Metadaten/-Sidecar zu einem Video."""
    base = os.path.splitext(video_path)[0]
    for extra in (base + '.jpg', base + '.ai.json', base + '.trigger.json', video_path + '.xmp'):
        if os.path.exists(extra):
            try:
                os.remove(extra)
            except Exception as e:
                print(f"Fehler beim Löschen von {extra}: {e}")
    fs_dir = os.path.join(os.path.dirname(video_path), '.thumbs', os.path.basename(base))
    if os.path.isdir(fs_dir):
        try:
            shutil.rmtree(fs_dir)
        except Exception as e:
            print(f"Fehler beim Löschen des Filmstrips: {e}")

@app.route('/delete/<filename>', methods=['POST'])
@requires_auth
def delete_video(filename: str):
    _verify_csrf()
    file_path = os.path.abspath(os.path.join(ALERTS_DIR, filename))
    if file_path.startswith(os.path.abspath(ALERTS_DIR)) and os.path.exists(file_path):
        try:
            os.remove(file_path)
        except Exception as e:
            _event_cache.clear()
            return json.dumps({'ok': False, 'error': str(e)})
        _remove_matching_thumbnail(file_path)
        if search_index is not None:
            search_index.remove_event(filename)
        if faces_db is not None:
            faces_db.remove_faces_for_video(filename)
    _event_cache.clear()
    return json.dumps({'ok': True})

@app.route('/archive/<filename>', methods=['POST'])
@requires_auth
def archive_video(filename: str):
    _verify_csrf()
    src_path = os.path.abspath(os.path.join(ALERTS_DIR, filename))
    # Sicherheitscheck: Datei muss direkt (nicht rekursiv) im ALERTS_DIR liegen
    if not (src_path.startswith(os.path.abspath(ALERTS_DIR)) and os.path.isfile(src_path)
            and os.path.dirname(src_path) == os.path.abspath(ALERTS_DIR)):
        return json.dumps({'ok': False, 'error': 'Video not found.'})
    if os.path.exists(os.path.splitext(src_path)[0] + '.recording'):
        # Noch aktiv aufgezeichnet -- der Recorder-Prozess hat den Dateipfad
        # noch offen und erwartet ihn dort für die eigene Ende-Bereinigung
        # (Marker entfernen etc.). Ein Verschieben mittendrin würde diese
        # Pfad-Erwartung zerstören, auch wenn der offene Dateihandle selbst
        # unter Linux technisch gültig bliebe.
        return json.dumps({'ok': False, 'error': 'Recording still in progress — wait until it finishes.'})
    try:
        shutil.move(src_path, os.path.join(ARCHIVE_DIR, os.path.basename(src_path)))
    except Exception as e:
        _event_cache.clear()
        return json.dumps({'ok': False, 'error': str(e)})
    # Passenden Trigger-Screenshot mitnehmen, damit er im Archiv weiterhin angezeigt wird
    thumb_src = os.path.splitext(src_path)[0] + '.jpg'
    if os.path.exists(thumb_src):
        try:
            shutil.move(thumb_src, os.path.join(ARCHIVE_DIR, os.path.basename(thumb_src)))
        except Exception as e:
            print(f"Fehler beim Archivieren des Thumbnails: {e}")
    # AI-Metadaten (JSON fürs Dashboard + XMP-Sidecar für Immich) mitnehmen
    for extra in (os.path.splitext(src_path)[0] + '.ai.json', os.path.splitext(src_path)[0] + '.trigger.json', src_path + '.xmp'):
        if os.path.exists(extra):
            try:
                shutil.move(extra, os.path.join(ARCHIVE_DIR, os.path.basename(extra)))
            except Exception as e:
                print(f"Fehler beim Archivieren von {extra}: {e}")
    # Filmstrip-Ordner mitnehmen
    fs_src = os.path.join(ALERTS_DIR, '.thumbs', os.path.splitext(os.path.basename(src_path))[0])
    if os.path.isdir(fs_src):
        try:
            shutil.move(fs_src, os.path.join(ARCHIVE_DIR, '.thumbs', os.path.basename(fs_src)))
        except Exception as e:
            print(f"Fehler beim Archivieren des Filmstrips: {e}")
    if search_index is not None:
        search_index.update_location(filename, ARCHIVE_DIR)
    if faces_db is not None:
        faces_db.update_base_dir(filename, ARCHIVE_DIR)
    _event_cache.clear()
    return json.dumps({'ok': True})

@app.route('/delete_archived/<filename>', methods=['POST'])
@requires_auth
def delete_archived_video(filename: str):
    _verify_csrf()
    file_path = os.path.abspath(os.path.join(ARCHIVE_DIR, filename))
    if file_path.startswith(os.path.abspath(ARCHIVE_DIR)) and os.path.exists(file_path):
        try:
            os.remove(file_path)
        except Exception as e:
            _event_cache.clear()
            return json.dumps({'ok': False, 'error': str(e)})
        _remove_matching_thumbnail(file_path)
        if search_index is not None:
            search_index.remove_event(filename)
        if faces_db is not None:
            faces_db.remove_faces_for_video(filename)
    _event_cache.clear()
    return json.dumps({'ok': True})

@app.route('/api/delete_events_bulk', methods=['POST'])
@requires_auth
def api_delete_events_bulk():
    _verify_csrf()
    filenames = request.form.getlist('filenames')
    archived_flags = request.form.getlist('archived')
    if not filenames or len(filenames) != len(archived_flags):
        return json.dumps({'ok': False, 'error': 'Invalid selection.'})

    deleted = 0
    failed = []
    for filename, archived_str in zip(filenames, archived_flags):
        directory = ARCHIVE_DIR if archived_str == 'true' else ALERTS_DIR
        file_path = os.path.abspath(os.path.join(directory, filename))
        if not (file_path.startswith(os.path.abspath(directory)) and os.path.exists(file_path)):
            failed.append(filename)
            continue
        try:
            os.remove(file_path)
        except Exception:
            failed.append(filename)
            continue
        _remove_matching_thumbnail(file_path)
        if search_index is not None:
            search_index.remove_event(filename)
        if faces_db is not None:
            faces_db.remove_faces_for_video(filename)
        deleted += 1

    _event_cache.clear()
    return json.dumps({'ok': True, 'deleted': deleted, 'failed': failed})

@app.route('/thumb/<filename>')
@requires_auth
def serve_thumbnail_image(filename):
    file_path = os.path.abspath(os.path.join(ALERTS_DIR, filename))
    if not file_path.startswith(os.path.abspath(ALERTS_DIR)) or not os.path.exists(file_path):
        return "", 404
    return Response(open(file_path, 'rb').read(), mimetype='image/jpeg')

@app.route('/thumb/archive/<filename>')
@requires_auth
def serve_archived_thumbnail_image(filename):
    file_path = os.path.abspath(os.path.join(ARCHIVE_DIR, filename))
    if not file_path.startswith(os.path.abspath(ARCHIVE_DIR)) or not os.path.exists(file_path):
        return "", 404
    return Response(open(file_path, 'rb').read(), mimetype='image/jpeg')

def _serve_filmstrip(base_dir, basename, index):
    file_path = os.path.abspath(os.path.join(base_dir, '.thumbs', basename, 'small', f'{index:04d}.jpg'))
    if not file_path.startswith(os.path.abspath(os.path.join(base_dir, '.thumbs'))) or not os.path.exists(file_path):
        return "", 404
    return Response(open(file_path, 'rb').read(), mimetype='image/jpeg')

@app.route('/filmstrip/<basename>/<int:index>')
@requires_auth
def serve_filmstrip(basename, index):
    return _serve_filmstrip(ALERTS_DIR, basename, index)

@app.route('/filmstrip/archive/<basename>/<int:index>')
@requires_auth
def serve_archived_filmstrip(basename, index):
    return _serve_filmstrip(ARCHIVE_DIR, basename, index)



def _transcode_stream(input_file):
    """Verpackt eine fertige Aufnahme für die Streaming-Wiedergabe im
    Dashboard neu (fragmentiertes MP4 für sofortigen Playback-Start, ohne
    die komplette Datei erst laden zu müssen) — OHNE erneutes Encoding.

    Vorher wurde hier komplett neu encodiert (libx264 ultrafast/zerolatency,
    Audio nach MP3) — das kostet nicht nur unnötig CPU/Zeit bei JEDER
    Wiedergabe einer bereits fertigen Aufnahme, sondern verzerrt auch
    nachweislich das Timing: bei einem Testlauf blieb die Video-Frame-Zahl
    zwar gleich, aber die Gesamtdauer verschob sich (3.000000s -> 3.025065s),
    und bei Audio ging sogar echte Frame-Zahl verloren (130 -> 117) — beides
    Symptome, die sich beim Abspielen als leichtes Ruckeln bemerkbar machen
    können, obwohl die Originalaufnahme selbst sauber war. Reines Stream-
    Copy/Remuxing (keine Neucodierung, nur Container-Umverpackung) behält
    exakt das Original-Timing bei und ist nebenbei um ein Vielfaches
    schneller."""
    def generate():
        process = subprocess.Popen(
            ['ffmpeg', '-i', input_file, '-c:v', 'copy', '-c:a', 'copy', '-f', 'mp4',
             '-movflags', 'frag_keyframe+empty_moov', 'pipe:1'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=-1
        )
        try:
            while True:
                chunk = process.stdout.read(8192)
                if not chunk:
                    break
                yield chunk
        except Exception:
            process.kill()
        finally:
            process.stdout.close()

    return Response(generate(), mimetype='video/mp4')

def _is_finished_video(path):
    """Schnelle ffprobe-Prüfung, ob eine MP4-Datei bereits fertig
    geschrieben ist (moov-Atom vorhanden, gültige Dauer auslesbar) oder
    noch aktiv von der Pipeline beschrieben wird. Nur für den zweiten Fall
    braucht die Wiedergabe das Remuxing über _transcode_stream — eine
    fertige Aufnahme (Recent wie Archive) kann direkt mit Range-Support
    ausgeliefert werden, ohne jeden Frame nochmal anzufassen."""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', path],
            capture_output=True, text=True, timeout=5
        )
        return float(result.stdout.strip()) > 0
    except Exception:
        return False

@app.route('/video/<filename>')
@requires_auth
def serve_annot_video(filename):
    input_file = os.path.join(ALERTS_DIR, filename)
    if not os.path.exists(input_file):
        return f"File not found: {filename}", 404
    if _is_finished_video(input_file):
        return send_file(input_file, mimetype='video/mp4', conditional=True)
    return _transcode_stream(input_file)

@app.route('/video/archive/<filename>')
@requires_auth
def serve_archived_video(filename):
    input_file = os.path.join(ARCHIVE_DIR, filename)
    if not os.path.exists(input_file):
        return f"File not found: {filename}", 404
    if _is_finished_video(input_file):
        return send_file(input_file, mimetype='video/mp4', conditional=True)
    return _transcode_stream(input_file)

if __name__ == '__main__':
    # threaded=True: sonst blockiert ein laufender Pipeline-Neustart (stop.sh +
    # start_detached.sh) die komplette Web-UI inkl. /api/status-Polling, da der
    # Flask-Dev-Server standardmäßig single-threaded ist.
    #
    # Hinweis für Dauerbetrieb: der eingebaute Dev-Server ist nicht für
    # Produktivbetrieb gedacht. Für fenrir empfiehlt sich gunicorn/waitress
    # dahinter plus ein Reverse Proxy (Caddy/nginx) mit TLS davor, siehe Chat.
    app.run(host='0.0.0.0', port=19473, threaded=True)
