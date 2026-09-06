"""
anomaly_detection.py — Isolation-Forest-basierte Anomalie-Erkennung auf den
bereits vorhandenen Text-Embeddings aus search_index.db (sentence-
transformers, dieselben Vektoren, die auch die semantische Suche nutzt).
Kein neues Modell, keine neue Extraktion, keine neue Abhängigkeit
(scikit-learn ist schon für die Gesichts-Clusterung (DBSCAN) im System) --
reine Wiederverwendung von etwas, das ohnehin schon berechnet wird.

Pro Kamera wird ein eigener Isolation Forest trainiert, auf einem
gleitenden Zeitfenster (Default 30 Tage) -- unterschiedliche Kameras sehen
strukturell unterschiedliche "normale" Muster (Eingangstür vs. Garten),
ein gemeinsames Modell würde das verwischen.

Training läuft getrennt vom Live-Betrieb (Cronjob oder manuell über die
GUI, siehe Dashboard-Karte). Inferenz selbst ist Millisekunden-schnell und
läuft synchron als letzter Schritt in ai_analyze.py, direkt nachdem das
Embedding fürs Such-Index sowieso schon berechnet wurde.
"""
import os
import sys
import json
import pickle
import sqlite3
import struct
import re
import time

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(DIR)
try:
    from config import SETTINGS_F
except ImportError:
    SETTINGS_F = "pipeline_settings.json"

DB_PATH = os.path.join(DIR, "search_index.db")
MODELS_DIR = os.path.join(DIR, ".anomaly_models")

FILENAME_RE = re.compile(r"^(.+?)_EVENT_\d{8}_\d{6}$")

# Isolation Forest liefert bei zu wenig Trainingsdaten keine sinnvollen
# Ergebnisse -- unter dieser Schwelle wird das Training übersprungen
# (kein Fehler, einfach "noch keine Baseline für diese Kamera").
MIN_TRAINING_SAMPLES = 15


def _load_settings():
    try:
        with open(SETTINGS_F) as f:
            return json.load(f)
    except Exception:
        return {}


def _camera_from_filename(filename):
    base = os.path.splitext(filename)[0]
    m = FILENAME_RE.match(base)
    return m.group(1) if m else None


def _unpack(blob):
    n = len(blob) // 4
    return struct.unpack(f"{n}f", blob)


def _model_path(camera):
    os.makedirs(MODELS_DIR, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", camera)
    return os.path.join(MODELS_DIR, f"{safe}.pkl")


def gather_embeddings(camera, lookback_days):
    """Holt alle vorhandenen Embeddings dieser Kamera aus den letzten
    lookback_days Tagen aus dem Such-Index."""
    cutoff = time.time() - lookback_days * 86400
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        rows = conn.execute(
            "SELECT filename, embedding FROM events WHERE embedding IS NOT NULL AND updated_at >= ?",
            (cutoff,)
        ).fetchall()
    finally:
        conn.close()
    return [_unpack(blob) for filename, blob in rows if _camera_from_filename(filename) == camera]


def train_baseline(camera, lookback_days=30):
    """Trainiert und speichert einen Isolation Forest für eine Kamera.
    Gibt (ok, message) zurück. Zu wenig Daten ist kein Fehler, sondern ein
    normaler Zustand für eine neue oder selten auslösende Kamera --
    entsprechend als ok=False mit erklärender Meldung, nicht als Exception."""
    vectors = gather_embeddings(camera, lookback_days)
    if len(vectors) < MIN_TRAINING_SAMPLES:
        return False, (f"Only {len(vectors)} events with a description in the last "
                        f"{lookback_days} days (need at least {MIN_TRAINING_SAMPLES}) "
                        f"— not enough history yet.")
    from sklearn.ensemble import IsolationForest
    import numpy as np
    X = np.array(vectors)
    # contamination='auto' lässt sklearn selbst schätzen, welcher Anteil der
    # Trainingsdaten als Ausreißer gilt, statt einen festen Prozentsatz
    # vorzugeben -- der wäre für sehr unterschiedlich stark frequentierte
    # Kameras (Haustür vs. Gartenschuppen) ohnehin nicht sinnvoll pauschal
    # zu setzen.
    clf = IsolationForest(n_estimators=100, contamination="auto", random_state=42)
    clf.fit(X)
    with open(_model_path(camera), "wb") as f:
        pickle.dump({"model": clf, "trained_at": time.time(), "sample_count": len(vectors)}, f)
    return True, f"Trained on {len(vectors)} events."


def list_cameras_with_data():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        rows = conn.execute("SELECT filename FROM events").fetchall()
    finally:
        conn.close()
    return sorted({c for (fn,) in rows if (c := _camera_from_filename(fn))})


def train_all_cameras(lookback_days=30):
    """Trainiert für jede Kamera mit vorhandenen Daten ein eigenes Modell --
    für einen Cronjob oder den GUI-Button gedacht, kein Aufrufer muss die
    Kameraliste selbst kennen."""
    return {camera: train_baseline(camera, lookback_days) for camera in list_cameras_with_data()}


def model_status(camera):
    """Für die GUI: wann wurde zuletzt trainiert, mit wie vielen Events."""
    path = _model_path(camera)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            data = pickle.load(f)
        return {"trained_at": data.get("trained_at"), "sample_count": data.get("sample_count")}
    except Exception:
        return None


def check_anomaly(camera, embedding):
    """Prüft ein einzelnes Embedding gegen das trainierte Modell dieser
    Kamera. Gibt (is_anomaly: bool, score: float|None) zurück -- score ist
    der rohe Isolation-Forest-Score (kleiner/negativer = ungewöhnlicher).
    Gibt (False, None) zurück, wenn (noch) kein Modell für diese Kamera
    existiert -- kein Fehler, einfach "noch keine Baseline"."""
    path = _model_path(camera)
    if not os.path.exists(path):
        return False, None
    try:
        import numpy as np
        with open(path, "rb") as f:
            data = pickle.load(f)
        clf = data["model"]
        X = np.array([embedding])
        prediction = clf.predict(X)[0]  # -1 = Anomalie, 1 = normal
        score = float(clf.score_samples(X)[0])
        return bool(prediction == -1), score
    except Exception as e:
        print(f"⚠️ [Anomaly] Prüfung für Kamera '{camera}' fehlgeschlagen: {e}")
        return False, None


def check_anomaly_for_event(filename):
    """Komfort-Funktion für ai_analyze.py: liest das gerade erst von
    search_index.py gespeicherte Embedding für dieses Event direkt aus der
    Datenbank zurück (statt es ein zweites Mal zu berechnen) und prüft es.
    Gibt (is_anomaly, score, camera) zurück -- camera ist None, falls der
    Dateiname nicht dem Standard-Schema folgt."""
    camera = _camera_from_filename(os.path.splitext(filename)[0])
    if camera is None:
        return False, None, None
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        row = conn.execute("SELECT embedding FROM events WHERE filename = ?", (filename,)).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return False, None, camera
    is_anomaly, score = check_anomaly(camera, _unpack(row[0]))
    return is_anomaly, score, camera


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Train anomaly-detection baselines for vaelen cameras.")
    parser.add_argument("--camera", help="Train only this camera (default: all cameras with data)")
    parser.add_argument("--lookback-days", type=int, default=30)
    args = parser.parse_args()
    if args.camera:
        ok, msg = train_baseline(args.camera, args.lookback_days)
        print(f"{args.camera}: {'OK' if ok else 'SKIPPED'} — {msg}")
    else:
        for camera, (ok, msg) in train_all_cameras(args.lookback_days).items():
            print(f"{camera}: {'OK' if ok else 'SKIPPED'} — {msg}")
