"""
plates_db.py - SQLite-Speicher für erkannte Kennzeichen und benannte
("bekannte") Fahrzeuge. Eigene Datenbank (plates.db), getrennt von
faces.db/search_index.db, nach demselben Grundmuster wie faces_db.py:

Modell: plate_recognize.py läuft auf denselben YOLO-Fahrzeugerkennungen
(car/truck/bus/motorcycle), die die Pipeline ohnehin schon berechnet --
schneidet die Fahrzeug-Box aus, lässt EasyOCR den Text lesen, und prüft
sofort, ob der erkannte Text zu einem bereits bekannten Fahrzeug passt
(exakter Text-Abgleich, im Gegensatz zu Gesichtern kein Embedding-Vergleich
nötig, da ein Kennzeichen-Text eindeutig ist). Was nicht zugeordnet werden
kann, bleibt als "unbekanntes Fahrzeug" stehen, bis der Nutzer es im
Dashboard manuell benennt -- danach werden künftige Erkennungen desselben
Kennzeichens automatisch zugeordnet.
"""
import os
import shutil
import sqlite3
import time

DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DIR, "plates.db")

try:
    from config import ALERTS_DIR
except ImportError:
    ALERTS_DIR = os.path.join(DIR, "alerts")

# Permanenter Foto-Speicher für benannte Fahrzeuge -- unabhängig vom Ordner
# des Quellvideos, aus demselben Grund wie PEOPLE_PHOTOS_DIR in faces_db.py:
# löscht man das Quellvideo, soll das Fahrzeug trotzdem benannt und künftig
# wiedererkennbar bleiben.
VEHICLE_PHOTOS_DIR = os.path.join(ALERTS_DIR, ".vehicle_photos")


def _archive_plate_photo(base_dir, crop_path, plate_id):
    """Kopiert den Fahrzeug-Crop in den permanenten Speicher, analog zu
    _archive_face_photo() in faces_db.py."""
    try:
        os.makedirs(VEHICLE_PHOTOS_DIR, exist_ok=True)
        source_path = os.path.join(base_dir, crop_path)
        if not os.path.exists(source_path):
            return base_dir, crop_path
        ext = os.path.splitext(crop_path)[1] or ".jpg"
        new_filename = f"vehicle_{plate_id}{ext}"
        dest_path = os.path.join(VEHICLE_PHOTOS_DIR, new_filename)
        if os.path.abspath(source_path) == os.path.abspath(dest_path):
            return VEHICLE_PHOTOS_DIR, new_filename
        shutil.copy2(source_path, dest_path)
        return VEHICLE_PHOTOS_DIR, new_filename
    except Exception as e:
        print(f"⚠️ Konnte Fahrzeugfoto {plate_id} nicht permanent archivieren: {e}")
        return base_dir, crop_path


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vehicles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            plate_text TEXT NOT NULL UNIQUE,
            representative_plate_id INTEGER,
            created_at REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS plates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            base_dir TEXT NOT NULL,
            crop_path TEXT NOT NULL,
            plate_text TEXT NOT NULL,
            ocr_confidence REAL,
            vehicle_type TEXT,
            vehicle_id INTEGER,
            rejected INTEGER DEFAULT 0,
            created_at REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plates_vehicle ON plates(vehicle_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plates_text ON plates(plate_text)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plates_filename ON plates(filename)")
    return conn


def get_plates_summary_for_video(filename):
    """Kurzfassung für die Dashboard-Kachel: Liste erkannter Kennzeichen
    (Text + Fahrzeugname, falls bekannt) für eine Aufnahme."""
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT p.plate_text, v.name
            FROM plates p LEFT JOIN vehicles v ON p.vehicle_id = v.id
            WHERE p.filename = ? AND p.rejected = 0
        """, (filename,)).fetchall()
        return [{"plate_text": r[0], "vehicle_name": r[1]} for r in rows]
    finally:
        conn.close()


def update_base_dir(filename, new_base_dir):
    """Beim Archivieren/Verschieben eines Videos: base_dir alter Einträge
    nachziehen, analog zu faces_db.update_base_dir()."""
    conn = _connect()
    try:
        conn.execute("UPDATE plates SET base_dir = ? WHERE filename = ?", (new_base_dir, filename))
        conn.commit()
    finally:
        conn.close()


def remove_plates_for_video(filename):
    """Löscht Kennzeichen-Einträge eines gelöschten Videos -- außer solchen,
    die einem BENANNTEN Fahrzeug zugeordnet sind (deren Foto liegt ja schon
    permanent in VEHICLE_PHOTOS_DIR, die DB-Zeile soll erhalten bleiben,
    genau wie bei faces_db.remove_faces_for_video())."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM plates WHERE filename = ? AND vehicle_id IS NULL", (filename,))
        conn.commit()
    finally:
        conn.close()


def find_matching_vehicle(plate_text):
    """Exakter Text-Abgleich gegen bekannte Fahrzeuge. Anders als bei
    Gesichtern (Embedding-Ähnlichkeit) reicht hier ein einfacher String-
    Vergleich, da ein korrekt gelesenes Kennzeichen eindeutig ist -- die
    Unsicherheit steckt in der OCR-Erkennung selbst (siehe ocr_confidence),
    nicht im Abgleich danach."""
    conn = _connect()
    try:
        row = conn.execute("SELECT id, name FROM vehicles WHERE plate_text = ?", (plate_text,)).fetchone()
        return {"id": row[0], "name": row[1]} if row else None
    finally:
        conn.close()


def add_plate(filename, base_dir, crop_path, plate_text, confidence, vehicle_type=None):
    """Fügt eine erkannte Kennzeichen-Lesung hinzu und ordnet sie sofort
    einem bekannten Fahrzeug zu, falls der Text exakt übereinstimmt."""
    conn = _connect()
    try:
        match = None
        row = conn.execute("SELECT id FROM vehicles WHERE plate_text = ?", (plate_text,)).fetchone()
        if row:
            match = row[0]
        cur = conn.execute("""
            INSERT INTO plates (filename, base_dir, crop_path, plate_text, ocr_confidence, vehicle_type, vehicle_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (filename, base_dir, crop_path, plate_text, confidence, vehicle_type, match, time.time()))
        conn.commit()
        return cur.lastrowid, match
    finally:
        conn.close()


def get_plate(plate_id):
    conn = _connect()
    try:
        row = conn.execute("""
            SELECT id, filename, base_dir, crop_path, plate_text, ocr_confidence, vehicle_type, vehicle_id, rejected, created_at
            FROM plates WHERE id = ?
        """, (plate_id,)).fetchone()
        if not row:
            return None
        keys = ["id", "filename", "base_dir", "crop_path", "plate_text", "ocr_confidence", "vehicle_type", "vehicle_id", "rejected", "created_at"]
        return dict(zip(keys, row))
    finally:
        conn.close()


def create_vehicle(name, plate_text, plate_id=None):
    """Legt ein neues bekanntes Fahrzeug an und ordnet -- falls angegeben --
    die auslösende plate-Zeile gleich zu, inklusive permanentem Foto-Archiv,
    analog zu faces_db.create_person()."""
    conn = _connect()
    try:
        existing = conn.execute("SELECT id FROM vehicles WHERE plate_text = ?", (plate_text,)).fetchone()
        if existing:
            raise ValueError(f"Ein Fahrzeug mit Kennzeichen '{plate_text}' existiert bereits.")
        cur = conn.execute(
            "INSERT INTO vehicles (name, plate_text, created_at) VALUES (?, ?, ?)",
            (name, plate_text, time.time())
        )
        vehicle_id = cur.lastrowid
        if plate_id is not None:
            row = conn.execute("SELECT base_dir, crop_path FROM plates WHERE id = ?", (plate_id,)).fetchone()
            if row:
                new_base_dir, new_crop_path = _archive_plate_photo(row[0], row[1], plate_id)
                conn.execute(
                    "UPDATE plates SET vehicle_id = ?, base_dir = ?, crop_path = ? WHERE id = ?",
                    (vehicle_id, new_base_dir, new_crop_path, plate_id)
                )
            conn.execute("UPDATE vehicles SET representative_plate_id = ? WHERE id = ?", (plate_id, vehicle_id))
        # Alle anderen, bereits vorhandenen Lesungen desselben Kennzeichentexts
        # rückwirkend zuordnen -- z.B. wenn dasselbe Auto schon mehrfach
        # gesehen wurde, bevor es benannt wurde.
        conn.execute(
            "UPDATE plates SET vehicle_id = ? WHERE plate_text = ? AND vehicle_id IS NULL",
            (vehicle_id, plate_text)
        )
        conn.commit()
        return vehicle_id
    finally:
        conn.close()


def rename_vehicle(vehicle_id, new_name):
    conn = _connect()
    try:
        conn.execute("UPDATE vehicles SET name = ? WHERE id = ?", (new_name, vehicle_id))
        conn.commit()
    finally:
        conn.close()


def delete_vehicle(vehicle_id, keep_plates=True):
    """Entfernt ein benanntes Fahrzeug. Standardmäßig bleiben die
    zugehörigen plate-Zeilen als 'unbekannt' erhalten (vehicle_id wird
    NULL), analog zum 'Un-name'-Verhalten bei Personen. Mit
    keep_plates=False werden auch die Lesungen selbst gelöscht (entspricht
    'delete_person_permanently')."""
    conn = _connect()
    try:
        if keep_plates:
            conn.execute("UPDATE plates SET vehicle_id = NULL WHERE vehicle_id = ?", (vehicle_id,))
        else:
            rows = conn.execute("SELECT base_dir, crop_path FROM plates WHERE vehicle_id = ?", (vehicle_id,)).fetchall()
            for base_dir, crop_path in rows:
                try:
                    full_path = os.path.join(base_dir, crop_path)
                    if os.path.abspath(base_dir) == os.path.abspath(VEHICLE_PHOTOS_DIR) and os.path.exists(full_path):
                        os.remove(full_path)
                except Exception:
                    pass
            conn.execute("DELETE FROM plates WHERE vehicle_id = ?", (vehicle_id,))
        conn.execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,))
        conn.commit()
    finally:
        conn.close()


def reject_plate(plate_id):
    """Falscherkennung (z.B. OCR hat Unsinn aus einem Nummernschild-losen
    Fahrzeug gelesen) -- analog zu reject_face()."""
    conn = _connect()
    try:
        conn.execute("UPDATE plates SET rejected = 1 WHERE id = ?", (plate_id,))
        conn.commit()
    finally:
        conn.close()


def list_vehicles():
    """Alle bekannten Fahrzeuge mit Sichtungs-Anzahl, für die Dashboard-
    Übersicht (analog zur People-Grid)."""
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT v.id, v.name, v.plate_text, v.representative_plate_id, v.created_at,
                   (SELECT COUNT(*) FROM plates WHERE vehicle_id = v.id AND rejected = 0) as sighting_count
            FROM vehicles v ORDER BY v.name
        """).fetchall()
        keys = ["id", "name", "plate_text", "representative_plate_id", "created_at", "sighting_count"]
        return [dict(zip(keys, r)) for r in rows]
    finally:
        conn.close()


def list_unknown_plates(limit=100):
    """Erkannte, aber noch keinem Fahrzeug zugeordnete Kennzeichen --
    typischerweise fremde/unbekannte Fahrzeuge, oder noch nicht benannte
    eigene. Neueste zuerst."""
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT id, filename, base_dir, crop_path, plate_text, ocr_confidence, vehicle_type, created_at
            FROM plates WHERE vehicle_id IS NULL AND rejected = 0
            ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        keys = ["id", "filename", "base_dir", "crop_path", "plate_text", "ocr_confidence", "vehicle_type", "created_at"]
        return [dict(zip(keys, r)) for r in rows]
    finally:
        conn.close()
