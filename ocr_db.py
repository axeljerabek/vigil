"""
ocr_db.py - SQLite-Speicher für JEDEN per OCR erkannten Text im Bild, nicht
nur Kennzeichen. Hausnummern, Paketaufkleber, Straßenschilder, Aufschriften
auf Kleidung, Firmenlogos mit Text -- alles, was EasyOCR auf einem
Filmstrip-Frame findet, landet hier. Kennzeichen-Erkennung (plate_recognize.py)
ist jetzt nur noch ein FILTER auf diesen allgemeinen Ergebnissen (Text, der
räumlich in einer Fahrzeug-Box liegt und wie ein Kennzeichen aussieht),
kein eigener, auf Fahrzeuge beschränkter OCR-Durchlauf mehr.
"""
import os
import sqlite3
import time

DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DIR, "ocr_text.db")


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ocr_text (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            base_dir TEXT NOT NULL,
            frame_index INTEGER,
            text TEXT NOT NULL,
            confidence REAL,
            bbox TEXT,
            source_class TEXT,
            created_at REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ocr_filename ON ocr_text(filename)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ocr_text ON ocr_text(text)")
    return conn


def add_text(filename, base_dir, frame_index, text, confidence, bbox=None, source_class=None):
    """source_class ist optional -- z.B. 'vehicle' wenn der Text-Fund
    räumlich in einer erkannten Fahrzeug-Box lag, sonst None (Text irgendwo
    sonst im Bild). Rein informativ, keine Filterlogik hier."""
    conn = _connect()
    try:
        import json as _json
        # EasyOCR liefert Bbox-Koordinaten als numpy-Skalare (z.B. int32),
        # die json.dumps nicht direkt serialisieren kann -- defensiv in
        # reine Python-Zahlen umwandeln, unabhängig davon, was der Aufrufer
        # tatsächlich übergibt.
        safe_bbox = None
        if bbox is not None:
            safe_bbox = [[float(x), float(y)] for x, y in bbox]
        cur = conn.execute("""
            INSERT INTO ocr_text (filename, base_dir, frame_index, text, confidence, bbox, source_class, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (filename, base_dir, frame_index, text, confidence,
              _json.dumps(safe_bbox) if safe_bbox is not None else None, source_class, time.time()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_text_for_video(filename):
    """Alle erkannten Text-Fragmente einer Aufnahme, für Anzeige und um sie
    in den Suchindex einzuspeisen."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT text, confidence, frame_index, source_class FROM ocr_text WHERE filename = ? ORDER BY frame_index",
            (filename,)
        ).fetchall()
        return [{"text": r[0], "confidence": r[1], "frame_index": r[2], "source_class": r[3]} for r in rows]
    finally:
        conn.close()


def get_combined_text_for_search(filename, min_confidence=0.4):
    """Dedupliziert und zu einem String zusammengefügt, direkt geeignet, um
    an search_index.py übergeben zu werden -- damit 'Ask Vaelen' und die
    normale Suche auch nach sichtbarem Text fragen/finden können, nicht nur
    nach der KI-Beschreibung."""
    entries = get_text_for_video(filename)
    seen = set()
    parts = []
    for e in entries:
        if e["confidence"] is not None and e["confidence"] < min_confidence:
            continue
        t = e["text"].strip()
        if t and t not in seen:
            seen.add(t)
            parts.append(t)
    return " ".join(parts)


def remove_text_for_video(filename):
    conn = _connect()
    try:
        conn.execute("DELETE FROM ocr_text WHERE filename = ?", (filename,))
        conn.commit()
    finally:
        conn.close()


def update_base_dir(filename, new_base_dir):
    conn = _connect()
    try:
        conn.execute("UPDATE ocr_text SET base_dir = ? WHERE filename = ?", (new_base_dir, filename))
        conn.commit()
    finally:
        conn.close()
