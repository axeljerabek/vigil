"""
mam_api.py — externe API zur Remote-Steuerung von vaelen.

Erlaubt fremden Systemen, Video/Audio/Bilder von außen einzureichen,
die vaelen komplett durch dieselbe Pipeline schickt wie eine eigene
Aufnahme (Codec-Absicherung, Filmstrip, KI-Analyse, Gesichtserkennung),
mit Job-spezifischen Parametern statt der globalen Settings, und liefert
das Ergebnis per Webhook-Callback UND per Status-Abfrage zurück.

Als eigenes Flask-Blueprint gebaut statt alles in web_ui.py zu packen --
klare Trennung, eigene Auth (API-Keys, nicht die Dashboard-Session), und
web_ui.py ist mit fast 2000 Zeilen ohnehin schon groß genug.

AUTH-MODELL: API-Keys werden gehasht gespeichert (wie Passwörter) -- der
Klartext-Key wird nur EINMAL beim Erzeugen angezeigt, danach ist er nicht
mehr abrufbar. Ein Leak von api_keys.json allein reicht nicht, um die
Keys selbst zu rekonstruieren.
"""
import os
import sys
import json
import time
import uuid
import hashlib
import secrets
import threading
import subprocess
from functools import wraps

from flask import Blueprint, request, jsonify, send_file, g

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(DIR)
try:
    from config import ALERTS_DIR, SETTINGS_F, STREAMS_F, PROJECT_ROOT
except ImportError:
    ALERTS_DIR = "./alerts"
    SETTINGS_F = "pipeline_settings.json"
    STREAMS_F = "streams.json"
    PROJECT_ROOT = DIR

import agent_permissions

API_KEYS_PATH = os.path.join(DIR, "api_keys.json")
JOBS_DIR = os.path.join(DIR, ".mam_jobs")
JOBS_UPLOAD_DIR = os.path.join(JOBS_DIR, "uploads")
JOBS_OUTPUT_DIR = os.path.join(JOBS_DIR, "output")

os.makedirs(JOBS_UPLOAD_DIR, exist_ok=True)
os.makedirs(JOBS_OUTPUT_DIR, exist_ok=True)

mam_bp = Blueprint("mam_api", __name__, url_prefix="/api/v1")

# Erlaubte Medientypen und ihre Dateiendungen -- alles andere wird beim
# Upload abgelehnt, statt der Pipeline etwas Unbekanntes unterzujubeln.
ALLOWED_VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".ts"}
ALLOWED_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".flac", ".ogg"}
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}


# ---------------------------------------------------------------------------
# API-Key-Verwaltung
# ---------------------------------------------------------------------------

def _hash_key(raw_key):
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _load_api_keys():
    try:
        with open(API_KEYS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_api_keys(keys):
    with open(API_KEYS_PATH, "w") as f:
        json.dump(keys, f, indent=2)


def generate_api_key(label):
    """Erzeugt einen neuen API-Key, speichert nur dessen Hash. Gibt den
    Klartext-Key EINMALIG zurück -- das ist die einzige Gelegenheit, ihn zu
    sehen, danach ist er aus vaelen selbst nicht mehr rekonstruierbar."""
    raw_key = "idg_" + secrets.token_urlsafe(32)
    keys = _load_api_keys()
    keys[_hash_key(raw_key)] = {
        "label": label or "Unnamed key",
        "created_at": time.time(),
        "last_used_at": None,
    }
    _save_api_keys(keys)
    return raw_key


def revoke_api_key(key_hash):
    keys = _load_api_keys()
    if key_hash in keys:
        del keys[key_hash]
        _save_api_keys(keys)
        return True
    return False


def list_api_keys():
    """Für die GUI: Label + Metadaten, NIE den Key selbst (der ist nach der
    Erzeugung ohnehin nur noch als Hash vorhanden)."""
    keys = _load_api_keys()
    return [
        {"key_hash": h, "label": v.get("label"), "created_at": v.get("created_at"),
         "last_used_at": v.get("last_used_at")}
        for h, v in keys.items()
    ]


def _touch_key_last_used(key_hash):
    keys = _load_api_keys()
    if key_hash in keys:
        keys[key_hash]["last_used_at"] = time.time()
        _save_api_keys(keys)


def requires_api_key(f):
    """Prüft Authorization: Bearer <key> ODER X-API-Key: <key> -- beide
    gängigen Konventionen unterstützt, damit möglichst viele externe
    Systeme ohne Anpassung funktionieren."""
    @wraps(f)
    def decorated(*args, **kwargs):
        raw_key = None
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            raw_key = auth_header[len("Bearer "):].strip()
        if not raw_key:
            raw_key = request.headers.get("X-API-Key", "").strip()
        if not raw_key:
            return jsonify({"error": "Missing API key (Authorization: Bearer <key> or X-API-Key header)."}), 401
        key_hash = _hash_key(raw_key)
        keys = _load_api_keys()
        if key_hash not in keys:
            return jsonify({"error": "Invalid API key."}), 401
        g.api_key_hash = key_hash
        g.api_key_label = keys[key_hash].get("label")
        _touch_key_last_used(key_hash)
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Job-Zustand
# ---------------------------------------------------------------------------

def _job_path(job_id):
    return os.path.join(JOBS_DIR, f"{job_id}.json")


def _load_job(job_id):
    try:
        with open(_job_path(job_id)) as f:
            return json.load(f)
    except Exception:
        return None


def _save_job(job):
    with open(_job_path(job["job_id"]), "w") as f:
        json.dump(job, f, indent=2)


def _create_job(media_type, original_filename, params):
    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "status": "queued",  # queued -> processing -> done | failed
        "media_type": media_type,
        "original_filename": original_filename,
        "params": params,
        "submitted_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "error": None,
        "result": None,
        "callback_url": params.get("callback_url"),
        "callback_delivered": False,
        "callback_attempts": 0,
    }
    _save_job(job)
    return job


# ---------------------------------------------------------------------------
# Verarbeitung -- läuft in einem Hintergrund-Thread pro Job, damit der
# einreichende HTTP-Request sofort mit der job_id antworten kann, statt auf
# die komplette Analyse zu warten (die je nach Ollama-Last mehrere Minuten
# dauern kann).
# ---------------------------------------------------------------------------

def _process_video_job(job_id, upload_path, params):
    job = _load_job(job_id)
    job["status"] = "processing"
    job["started_at"] = time.time()
    _save_job(job)

    try:
        import watch_folder
        import ai_analyze
        import face_recognize
        import backfill_filmstrips

        # Dieselbe Codec-Absicherung wie beim Watchfolder-Import -- Job kann
        # von irgendeinem fremden System kommen, dessen Video-Codec nicht
        # zwingend browser-/pipeline-kompatibel ist.
        mp4_path = watch_folder._ensure_mp4(upload_path, logger=print)
        if not mp4_path:
            raise RuntimeError("Could not process the uploaded video (unsupported or corrupt file).")

        basename = f"mam_{job_id}"
        dest_path = os.path.join(JOBS_OUTPUT_DIR, basename + ".mp4")
        if mp4_path != dest_path:
            os.replace(mp4_path, dest_path)
        if upload_path != mp4_path and os.path.exists(upload_path):
            os.remove(upload_path)

        # Filmstrip -- dieselbe Funktion wie überall sonst im System.
        thumbs_root = os.path.join(JOBS_OUTPUT_DIR, ".thumbs")
        os.makedirs(thumbs_root, exist_ok=True)
        backfill_filmstrips.backfill_filmstrip(dest_path, thumbs_root, 12)

        # KI-Analyse -- mit Job-spezifischen Themen, falls mitgegeben,
        # sonst exakt dasselbe Verhalten wie bei einer normalen Aufnahme
        # (globale Settings-Themen).
        topics_override = params.get("topics") or None
        ai_analyze.analyze(basename, JOBS_OUTPUT_DIR, topics_override=topics_override)

        # Gesichtserkennung -- optional, falls im Job angefordert.
        if params.get("detect_faces", True):
            try:
                face_recognize.recognize(basename, JOBS_OUTPUT_DIR)
            except Exception as e:
                print(f"⚠️ [API] Gesichtserkennung für Job {job_id} fehlgeschlagen: {e}")

        meta_path = os.path.join(JOBS_OUTPUT_DIR, f"{basename}.ai.json")
        meta = {}
        if os.path.exists(meta_path):
            with open(meta_path) as f:
                meta = json.load(f)

        job["status"] = "done"
        job["finished_at"] = time.time()
        job["result"] = {
            "video_filename": basename + ".mp4",
            "description": meta.get("description"),
            "topics": meta.get("topics"),
            "transcript": meta.get("transcript"),
            "faces": meta.get("faces"),
        }
    except Exception as e:
        job["status"] = "failed"
        job["finished_at"] = time.time()
        job["error"] = str(e)
        print(f"❌ [API] Job {job_id} fehlgeschlagen: {e}")
    finally:
        _save_job(job)
        _deliver_callback(job)


def _deliver_callback(job, max_attempts=5):
    """Feuert den Webhook, falls konfiguriert -- mit Retry bei
    Fehlschlag (der Aufrufer könnte gerade neu starten o.ä.). Läuft
    NICHT blockierend für die Job-Verarbeitung selbst (wird nach deren
    Abschluss aufgerufen, aber in einem eigenen Thread, damit ein
    langsamer/unerreichbarer Callback-Empfänger nicht den Worker-Thread
    blockiert, der ja schon fertig ist -- reine Absicherung)."""
    callback_url = job.get("callback_url")
    if not callback_url:
        return

    def _attempt():
        import urllib.request
        import urllib.error
        payload = json.dumps({
            "job_id": job["job_id"],
            "status": job["status"],
            "result": job.get("result"),
            "error": job.get("error"),
        }).encode("utf-8")
        delay = 2
        for attempt in range(1, max_attempts + 1):
            try:
                req = urllib.request.Request(
                    callback_url, data=payload,
                    headers={"Content-Type": "application/json"}, method="POST"
                )
                urllib.request.urlopen(req, timeout=15)
                current = _load_job(job["job_id"])
                if current:
                    current["callback_delivered"] = True
                    current["callback_attempts"] = attempt
                    _save_job(current)
                return
            except Exception as e:
                print(f"⚠️ [API] Callback-Zustellung an {callback_url} fehlgeschlagen (Versuch {attempt}/{max_attempts}): {e}")
                time.sleep(delay)
                delay = min(delay * 2, 60)
        current = _load_job(job["job_id"])
        if current:
            current["callback_attempts"] = max_attempts
            current.setdefault("callback_delivered", False)
            _save_job(current)

    threading.Thread(target=_attempt, daemon=True).start()


# ---------------------------------------------------------------------------
# Routen
# ---------------------------------------------------------------------------

@mam_bp.route("/jobs", methods=["POST"])
@requires_api_key
def submit_job():
    """Nimmt ein Video per multipart-Upload entgegen (Feldname 'file'),
    plus optionale Formularfelder:
      topics            - kommagetrennte Liste, überschreibt für diesen
                           Job die globalen Themen-Settings
      detect_faces      - "true"/"false", Default an
      callback_url       - wird bei Fertigstellung per POST benachrichtigt
    Antwortet SOFORT mit der job_id -- die eigentliche Verarbeitung läuft
    im Hintergrund, kann je nach Ollama-Last mehrere Minuten dauern."""
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded (expected multipart field 'file')."}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "Empty filename."}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext in ALLOWED_VIDEO_EXT:
        media_type = "video"
    elif ext in ALLOWED_AUDIO_EXT:
        return jsonify({"error": "Audio-only jobs are not yet supported — video only for now."}), 400
    elif ext in ALLOWED_IMAGE_EXT:
        return jsonify({"error": "Image-only jobs are not yet supported — video only for now."}), 400
    else:
        return jsonify({"error": f"Unsupported file type '{ext}'."}), 400

    topics_raw = request.form.get("topics", "").strip()
    params = {
        "topics": [t.strip() for t in topics_raw.split(",") if t.strip()] if topics_raw else None,
        "detect_faces": request.form.get("detect_faces", "true").lower() != "false",
        "callback_url": request.form.get("callback_url", "").strip() or None,
    }

    job = _create_job(media_type, file.filename, params)
    upload_path = os.path.join(JOBS_UPLOAD_DIR, f"{job['job_id']}{ext}")
    file.save(upload_path)

    threading.Thread(target=_process_video_job, args=(job["job_id"], upload_path, params), daemon=True).start()

    return jsonify({"job_id": job["job_id"], "status": "queued"}), 202


@mam_bp.route("/jobs/<job_id>", methods=["GET"])
@requires_api_key
def get_job_status(job_id):
    """Passiver Status-Abruf -- funktioniert unabhängig vom Callback, für
    Aufrufer, die lieber pollen als einen Webhook-Endpunkt zu betreiben."""
    job = _load_job(job_id)
    if job is None:
        return jsonify({"error": "Job not found."}), 404
    return jsonify(job)


@mam_bp.route("/jobs/<job_id>/video", methods=["GET"])
@requires_api_key
def get_job_video(job_id):
    """Liefert das verarbeitete Video aus. Optionale Query-Parameter
    ?start=SEKUNDEN&end=SEKUNDEN schneiden nur diesen Ausschnitt heraus
    (per ffmpeg, Stream-Copy wo möglich -- kein Neu-Encoding, außer der
    Schnittpunkt liegt nicht exakt auf einem Keyframe, dann entscheidet
    ffmpeg selbst, ob es re-encodieren muss für einen exakten Schnitt)."""
    job = _load_job(job_id)
    if job is None or job.get("status") != "done":
        return jsonify({"error": "Job not found or not finished yet."}), 404
    video_path = os.path.join(JOBS_OUTPUT_DIR, job["result"]["video_filename"])
    if not os.path.exists(video_path):
        return jsonify({"error": "Video file missing on disk."}), 404

    start = request.args.get("start")
    end = request.args.get("end")
    if start is None and end is None:
        return send_file(video_path, mimetype="video/mp4", conditional=True)

    try:
        start_f = float(start) if start is not None else 0.0
        segment_path = os.path.join(JOBS_OUTPUT_DIR, f"{job_id}_segment_{start}_{end}.mp4")
        if not os.path.exists(segment_path):
            args = ["ffmpeg", "-y", "-ss", str(start_f), "-i", video_path]
            if end is not None:
                duration = float(end) - start_f
                if duration <= 0:
                    return jsonify({"error": "'end' must be greater than 'start'."}), 400
                args += ["-t", str(duration)]
            args += ["-c", "copy", segment_path]
            result = subprocess.run(args, capture_output=True, text=True, timeout=60)
            if result.returncode != 0 or not os.path.exists(segment_path):
                return jsonify({"error": f"Could not extract segment: {result.stderr[-300:]}"}), 500
        return send_file(segment_path, mimetype="video/mp4", conditional=True)
    except ValueError:
        return jsonify({"error": "'start'/'end' must be numbers (seconds)."}), 400


@mam_bp.route("/jobs/<job_id>/metadata", methods=["GET"])
@requires_api_key
def get_job_metadata(job_id):
    job = _load_job(job_id)
    if job is None:
        return jsonify({"error": "Job not found."}), 404
    if job.get("status") != "done":
        return jsonify({"job_id": job_id, "status": job["status"], "error": job.get("error")})
    return jsonify({"job_id": job_id, "status": "done", **job["result"]})


# ---------------------------------------------------------------------------
# Agent control -- separate capability gate on top of the normal API-key
# check. See agent_config.json / AGENT_CONFIG.md. Off by default; every
# route below checks its own capability before doing anything.
# ---------------------------------------------------------------------------

def requires_agent_capability(capability):
    def decorator(f):
        @wraps(f)
        @requires_api_key
        def wrapped(*args, **kwargs):
            if not agent_permissions.is_capability_allowed(capability):
                return jsonify({
                    "error": f"Agent capability '{capability}' is not enabled. "
                             f"See agent_config.json / AGENT_CONFIG.md."
                }), 403
            return f(*args, **kwargs)
        return wrapped
    return decorator


def _load_streams():
    try:
        with open(STREAMS_F) as f:
            return json.load(f)
    except Exception:
        return []


def _save_streams(streams):
    with open(STREAMS_F, "w") as f:
        json.dump(streams, f, indent=2)


@mam_bp.route("/agent/cameras", methods=["GET"])
@requires_agent_capability("cameras_toggle")
def agent_list_cameras():
    # URL intentionally omitted -- may contain embedded credentials.
    return jsonify([
        {"name": s.get("name"), "enabled": s.get("enabled", False), "audio_enabled": s.get("audio_enabled", False)}
        for s in _load_streams()
    ])


@mam_bp.route("/agent/cameras/<name>/enable", methods=["POST"])
@requires_agent_capability("cameras_toggle")
def agent_enable_camera(name):
    return _set_camera_enabled(name, True)


@mam_bp.route("/agent/cameras/<name>/disable", methods=["POST"])
@requires_agent_capability("cameras_toggle")
def agent_disable_camera(name):
    return _set_camera_enabled(name, False)


def _get_optional_bool(key):
    """Liest ein optionales Bool-Feld aus JSON-Body ODER Form-Body -- Aufrufer
    könnten beides schicken, beide werden akzeptiert. None = Feld nicht
    mitgeschickt, unterscheidet sich bewusst von False (nicht mitgeschickt
    heißt 'unverändert lassen', nicht 'auf False setzen')."""
    payload = request.get_json(silent=True) or {}
    if key in payload:
        return bool(payload[key])
    if key in request.form:
        return request.form.get(key) in ("true", "1", "on", "True")
    return None


def _set_camera_enabled(name, enabled):
    """Setzt enabled, UND optional audio_enabled, falls im Request-Body
    mitgeschickt (JSON oder Form, siehe _get_optional_bool) -- vorher
    wurde audio_enabled hier komplett ignoriert, ein mitgeschicktes
    {"audio_enabled": false} hatte schlicht keine Wirkung."""
    streams = _load_streams()
    audio_override = _get_optional_bool("audio_enabled")
    for s in streams:
        if s.get("name") == name:
            s["enabled"] = enabled
            if audio_override is not None:
                s["audio_enabled"] = audio_override
            _save_streams(streams)
            return jsonify({"ok": True, "name": name, "enabled": enabled, "audio_enabled": s.get("audio_enabled", False)})
    return jsonify({"error": f"Camera '{name}' not found."}), 404


@mam_bp.route("/agent/cameras/<name>/audio/enable", methods=["POST"])
@requires_agent_capability("cameras_toggle")
def agent_enable_camera_audio(name):
    return _set_camera_audio(name, True)


@mam_bp.route("/agent/cameras/<name>/audio/disable", methods=["POST"])
@requires_agent_capability("cameras_toggle")
def agent_disable_camera_audio(name):
    return _set_camera_audio(name, False)


def _set_camera_audio(name, audio_enabled):
    """Dedizierter Audio-only-Endpunkt, für den Fall, dass ein Aufrufer nur
    das Audio-Flag ändern will, ohne den Video-Enabled-Status anzufassen --
    _set_camera_enabled() braucht immer einen enabled-Wert, das hier nicht."""
    streams = _load_streams()
    for s in streams:
        if s.get("name") == name:
            s["audio_enabled"] = audio_enabled
            _save_streams(streams)
            return jsonify({"ok": True, "name": name, "enabled": s.get("enabled", False), "audio_enabled": audio_enabled})
    return jsonify({"error": f"Camera '{name}' not found."}), 404


@mam_bp.route("/agent/settings", methods=["GET"])
@requires_agent_capability("settings_change")
def agent_get_settings():
    try:
        with open(SETTINGS_F) as f:
            settings = json.load(f)
    except Exception:
        settings = {}
    # Nur die per Allowlist überhaupt änderbaren Werte zurückgeben -- alles
    # andere (Zugangsdaten, Pfade) geht den Agenten nichts an, auch lesend nicht.
    return jsonify({k: v for k, v in settings.items() if k in agent_permissions.SETTINGS_ALLOWLIST})


@mam_bp.route("/agent/settings", methods=["POST"])
@requires_agent_capability("settings_change")
def agent_update_settings():
    payload = request.get_json(silent=True) or {}
    rejected = [k for k in payload if k not in agent_permissions.SETTINGS_ALLOWLIST]
    if rejected:
        return jsonify({
            "error": f"Not allowed to change: {rejected}. Only these keys can be changed via the agent API: "
                     f"{sorted(agent_permissions.SETTINGS_ALLOWLIST)}"
        }), 403
    try:
        with open(SETTINGS_F) as f:
            settings = json.load(f)
    except Exception:
        settings = {}
    settings.update(payload)
    with open(SETTINGS_F, "w") as f:
        json.dump(settings, f, indent=2)
    return jsonify({"ok": True, "updated": list(payload.keys())})


@mam_bp.route("/agent/pipeline/status", methods=["GET"])
@requires_agent_capability("pipeline_control")
def agent_pipeline_status():
    try:
        from helpers import is_pipeline_running
        running = is_pipeline_running()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"running": running})


@mam_bp.route("/agent/pipeline/start", methods=["POST"])
@requires_agent_capability("pipeline_control")
def agent_start_pipeline():
    try:
        subprocess.Popen(["/bin/bash", os.path.join(PROJECT_ROOT, "start_detached.sh")], cwd=PROJECT_ROOT)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "status": "starting"})


@mam_bp.route("/agent/pipeline/stop", methods=["POST"])
@requires_agent_capability("pipeline_control")
def agent_stop_pipeline():
    try:
        subprocess.Popen(["/bin/bash", os.path.join(PROJECT_ROOT, "stop.sh")], cwd=PROJECT_ROOT)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "status": "stopping"})


@mam_bp.route("/agent/search", methods=["GET"])
@requires_agent_capability("search")
def agent_search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"results": []})
    results = []
    try:
        import search_index
        for filename, base_dir, description, score in search_index.search(query):
            results.append({"filename": filename, "base_dir": base_dir, "description": description, "score": score})
    except Exception as e:
        return jsonify({"results": [], "error": str(e)})
    results.sort(key=lambda r: r["score"], reverse=True)
    return jsonify({"results": results[:50]})


@mam_bp.route("/agent/cameras/<name>/notify_only/enable", methods=["POST"])
@requires_agent_capability("manual_trigger")
def agent_enable_notify_only(name):
    return _set_notify_only(name, True)


@mam_bp.route("/agent/cameras/<name>/notify_only/disable", methods=["POST"])
@requires_agent_capability("manual_trigger")
def agent_disable_notify_only(name):
    return _set_notify_only(name, False)


def _set_notify_only(name, notify_only):
    """Schaltet eine Kamera zwischen normal (YOLO-Erkennung löst direkt eine
    Aufnahme aus) und notify-only (YOLO erkennt weiter, meldet aber nur --
    Aufnahme startet erst über /trigger) um. Braucht KEINEN Pipeline-
    Neustart -- CameraAgent.notify_only wird nur beim Prozessstart aus
    streams.json gelesen, ein bereits laufender Worker merkt eine spätere
    Änderung hier also erst nach dem nächsten Neustart dieser Kamera."""
    streams = _load_streams()
    for s in streams:
        if s.get("name") == name:
            s["notify_only"] = notify_only
            _save_streams(streams)
            return jsonify({
                "ok": True, "name": name, "notify_only": notify_only,
                "note": "Takes effect after this camera's process restarts (stop/start pipeline), not instantly."
            })
    return jsonify({"error": f"Camera '{name}' not found."}), 404


@mam_bp.route("/agent/cameras/<name>/trigger", methods=["POST"])
@requires_agent_capability("manual_trigger")
def agent_trigger_recording(name):
    """Löst JETZT eine Aufnahme für diese Kamera aus -- funktioniert für
    JEDE Kamera (nicht nur notify_only), nutzt dieselbe Pre-Roll-Logik wie
    ein normaler YOLO-Trigger. Reine Datei-Existenzprüfung als Signal an
    den laufenden CameraAgent-Prozess -- der liest und löscht sie selbst
    in seiner Hauptschleife, kein Locking nötig (nur ein Schreiber hier,
    nur ein Leser dort)."""
    streams = _load_streams()
    if not any(s.get("name") == name for s in streams):
        return jsonify({"error": f"Camera '{name}' not found."}), 404
    if not any(s.get("name") == name and s.get("enabled") for s in streams):
        return jsonify({"error": f"Camera '{name}' is disabled -- enable it first."}), 400
    trigger_dir = os.path.join(ALERTS_DIR, ".triggers")
    os.makedirs(trigger_dir, exist_ok=True)
    open(os.path.join(trigger_dir, f"{name}.flag"), "w").close()
    return jsonify({"ok": True, "name": name, "status": "trigger_sent"})


@mam_bp.route("/agent/cameras/<name>/stop", methods=["POST"])
@requires_agent_capability("manual_trigger")
def agent_stop_recording(name):
    """Beendet eine LAUFENDE Aufnahme für diese Kamera sofort -- das ist NICHT
    dasselbe wie cameras_toggle/disable: disable setzt nur 'enabled' in
    streams.json, was ein bereits laufender Worker-Prozess nie erneut
    liest, eine aktive Aufnahme also unbeeinflusst weiterlaufen lässt.
    Dieser Endpunkt ist der tatsächlich richtige Weg, eine laufende
    Aufnahme von außen jetzt zu beenden -- dasselbe Datei-Flag-Muster wie
    /trigger, nur umgekehrt."""
    streams = _load_streams()
    if not any(s.get("name") == name for s in streams):
        return jsonify({"error": f"Camera '{name}' not found."}), 404
    stop_dir = os.path.join(ALERTS_DIR, ".stops")
    os.makedirs(stop_dir, exist_ok=True)
    open(os.path.join(stop_dir, f"{name}.flag"), "w").close()
    return jsonify({"ok": True, "name": name, "status": "stop_sent"})


@mam_bp.route("/agent/cameras/<name>/quick_record", methods=["POST"])
@requires_agent_capability("manual_trigger")
def agent_quick_record(name):
    """Sofort-Aufnahme UNABHÄNGIG von der laufenden Pipeline -- kein YOLO,
    keine Zustandsmaschine, funktioniert auch wenn die Pipeline komplett
    gestoppt ist oder die Kamera dort deaktiviert ist. Für "nimm das
    schnell für eine Minute auf" statt für ereignisgesteuerte Erkennung.
    Nutzt ffmpegs eigenes -t-Flag für exakte Dauer."""
    try:
        import quick_record
    except ImportError:
        return jsonify({"error": "quick_record module not available."}), 500
    duration = request.form.get("duration", request.args.get("duration", 30))
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        return jsonify({"error": "'duration' must be a number of seconds."}), 400
    job_id, error = quick_record.start_quick_record(name, duration)
    if error:
        return jsonify({"error": error}), 404
    return jsonify({"ok": True, "job_id": job_id, "duration_sec": min(duration, quick_record.MAX_DURATION_SEC)}), 202


@mam_bp.route("/agent/quick_record/<job_id>", methods=["GET"])
@requires_agent_capability("manual_trigger")
def agent_quick_record_status(job_id):
    try:
        import quick_record
    except ImportError:
        return jsonify({"error": "quick_record module not available."}), 500
    job = quick_record.load_job(job_id)
    if job is None:
        return jsonify({"error": "Job not found."}), 404
    return jsonify(job)


@mam_bp.route("/agent/events/<filename>", methods=["GET"])
@requires_agent_capability("search")
def agent_get_event(filename):
    """Volle Metadaten zu einem einzelnen Video -- /search liefert nur
    Treffer mit Score, hier gibt's die kompletten Details (Themen,
    Transkript, Gesichter, Bewertung, Anomalie-Status)."""
    filename = os.path.basename(filename)  # Pfad-Trick-Absicherung
    basename = os.path.splitext(filename)[0]
    for base_dir in (ALERTS_DIR, os.path.join(ALERTS_DIR, "archive")):
        meta_path = os.path.join(base_dir, f"{basename}.ai.json")
        if os.path.exists(meta_path):
            try:
                with open(meta_path) as f:
                    return jsonify(json.load(f))
            except Exception as e:
                return jsonify({"error": str(e)}), 500
    return jsonify({"error": f"No metadata found for '{filename}'."}), 404


@mam_bp.route("/agent/summaries", methods=["GET"])
@requires_agent_capability("search")
def agent_list_summaries():
    """Dieselben Tages-/Wochen-Zusammenfassungen wie im Dashboard --
    beantwortet 'was ist heute passiert?' ohne durch Einzelereignisse
    suchen zu müssen."""
    summaries_dir = os.path.join(ALERTS_DIR, ".summaries")
    results = []
    if os.path.isdir(summaries_dir):
        import glob as _glob
        for path in sorted(_glob.glob(os.path.join(summaries_dir, "*.json")), reverse=True)[:20]:
            try:
                with open(path) as f:
                    results.append(json.load(f))
            except Exception:
                continue
    return jsonify({"summaries": results})


@mam_bp.route("/agent/system_status", methods=["GET"])
@requires_agent_capability("search")
def agent_system_status():
    """Hardware-Werte (GPU-Temperatur, VRAM, CPU/RAM) -- derselbe Code wie
    die Dashboard-Statuskarte. Lazy-Import von web_ui, um einen
    zirkulären Import zu vermeiden (web_ui.py importiert dieses Modul)."""
    try:
        import web_ui
        return jsonify(web_ui.get_detailed_system_status())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@mam_bp.route("/agent/reanalyze/<filename>", methods=["POST"])
@requires_agent_capability("manual_trigger")
def agent_reanalyze(filename):
    """Stößt eine erneute KI-Analyse für ein bestehendes Video an --
    derselbe Re-Analyze-Knopf wie im Dashboard, nur für den Agenten
    freigegeben. Läuft als Hintergrund-Prozess, da eine Ollama-Analyse
    eine Weile dauern kann."""
    filename = os.path.basename(filename)
    basename = os.path.splitext(filename)[0]
    base_dir = None
    for candidate in (ALERTS_DIR, os.path.join(ALERTS_DIR, "archive")):
        if os.path.exists(os.path.join(candidate, filename)):
            base_dir = candidate
            break
    if base_dir is None:
        return jsonify({"error": f"Video '{filename}' not found."}), 404
    try:
        subprocess.Popen([sys.executable, os.path.join(DIR, "postprocess.py"), basename, base_dir])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "status": "reanalysis_started"})


@mam_bp.route("/agent/anomaly/train", methods=["POST"])
@requires_agent_capability("manual_trigger")
def agent_train_anomaly():
    """Trainiert die Anomalie-Baselines neu -- derselbe Dashboard-Button,
    für den Agenten freigegeben. Läuft synchron (Training selbst dauert
    typischerweise nur Sekunden, siehe anomaly_detection.py)."""
    try:
        import anomaly_detection
    except ImportError:
        return jsonify({"error": "anomaly_detection module not available."}), 500
    lookback = request.form.get("lookback_days", request.args.get("lookback_days", 30))
    try:
        lookback = int(lookback)
    except (TypeError, ValueError):
        lookback = 30
    results = anomaly_detection.train_all_cameras(lookback)
    return jsonify({
        "ok": True,
        "results": {camera: {"trained": ok, "message": msg} for camera, (ok, msg) in results.items()}
    })


@mam_bp.route("/agent/detections", methods=["GET"])
@requires_agent_capability("manual_trigger")
def agent_list_detections():
    """Letzte gemeldete Erkennung pro Kamera -- v.a. für notify_only-Kameras
    gedacht (dort ist das der einzige Weg zu erfahren, dass gerade etwas
    erkannt wurde, ohne dass automatisch aufgenommen wird), funktioniert
    aber für jede Kamera, die überhaupt schon mal erkannt hat."""
    detection_dir = os.path.join(ALERTS_DIR, ".detections")
    results = []
    if os.path.isdir(detection_dir):
        for fn in os.listdir(detection_dir):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(detection_dir, fn)) as f:
                    results.append(json.load(f))
            except Exception:
                continue
    results.sort(key=lambda r: r.get("timestamp", 0), reverse=True)
    return jsonify({"detections": results})




@mam_bp.route("/agent/capabilities", methods=["GET"])
@requires_api_key
def agent_capabilities():
    """One-call orientation for an agent: what's currently allowed, and
    (only for capabilities that are actually enabled) the concrete data
    that goes with it -- camera list, pipeline status, changeable settings
    keys. Deliberately NOT gated behind agent_control_enabled itself: this
    is read-only self-description, telling an agent what it can do costs
    nothing and saves it from guessing via trial and error."""
    config = agent_permissions.load_config()
    master_on = config.get("agent_control_enabled", False)
    capabilities = config.get("capabilities", {})

    def cap_info(name):
        c = capabilities.get(name, {})
        return {"enabled": master_on and bool(c.get("enabled", False)), "risk": c.get("risk"), "description": c.get("description")}

    response = {
        "agent_control_enabled": master_on,
        "capabilities": {
            "search": cap_info("search"),
            "cameras_toggle": cap_info("cameras_toggle"),
            "pipeline_control": cap_info("pipeline_control"),
            "manual_trigger": cap_info("manual_trigger"),
            "settings_change": cap_info("settings_change"),
            "delete": cap_info("delete"),
            "export": cap_info("export"),
        },
    }
    response["capabilities"]["settings_change"]["allowed_keys"] = sorted(agent_permissions.SETTINGS_ALLOWLIST)

    if response["capabilities"]["cameras_toggle"]["enabled"]:
        response["cameras"] = [
            {"name": s.get("name"), "enabled": s.get("enabled", False), "audio_enabled": s.get("audio_enabled", False)}
            for s in _load_streams()
        ]

    if response["capabilities"]["pipeline_control"]["enabled"]:
        try:
            from helpers import is_pipeline_running
            response["pipeline_running"] = is_pipeline_running()
        except Exception:
            response["pipeline_running"] = None

    return jsonify(response)
