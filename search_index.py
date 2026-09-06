"""
search_index.py - Volltext + semantische Suche über die KI-Videobeschreibungen.

SQLite statt einer "echten" Vektordatenbank: bei den hier zu erwartenden
Datenmengen (hunderte bis niedrige tausende Aufnahmen) ist ein linearer
Scan + Cosine-Similarity in Python in Millisekunden erledigt — eine
dedizierte Vektordatenbank (Qdrant, Chroma, pgvox) wäre für einen
Einzelnutzer-Server deutlich überdimensioniert.

Kombiniert zwei Signale pro Suche:
  1. Volltext (Substring-Match, case-insensitive) — schnell, exakt,
     funktioniert auch ganz ohne Embedding-Modell.
  2. Semantische Ähnlichkeit (sentence-transformers, all-MiniLM-L6-v2,
     ~80 MB) — findet "Person trägt Karton" auch wenn die Beschreibung
     "Individuum hält Paket" sagt.

Fehlt sentence-transformers oder schlägt der Modell-Download fehl, fällt
die Suche automatisch auf reinen Volltext zurück — kein Crash, keine
harte Abhängigkeit.
"""
import os
import sqlite3
import struct
import threading

DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DIR, "search_index.db")

_model = None
_model_lock = threading.Lock()
_model_load_failed = False

# Ab dieser Cosine-Ähnlichkeit gilt ein Treffer als semantisch relevant
# genug, um OHNE Volltext-Übereinstimmung trotzdem angezeigt zu werden.
SEMANTIC_THRESHOLD = 0.25


def _get_model():
    global _model, _model_load_failed
    if _model is not None or _model_load_failed:
        return _model
    with _model_lock:
        if _model is not None or _model_load_failed:
            return _model
        try:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer("all-MiniLM-L6-v2")
        except Exception as e:
            print(f"⚠️ Semantisches Suchmodell konnte nicht geladen werden (pip install "
                  f"sentence-transformers nötig?) — Suche bleibt rein textbasiert: {e}")
            _model_load_failed = True
    return _model


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            filename TEXT PRIMARY KEY,
            base_dir TEXT NOT NULL,
            description TEXT NOT NULL,
            embedding BLOB,
            topics TEXT,
            transcript TEXT,
            ocr_text TEXT,
            updated_at REAL
        )
    """)
    # Bestehende Datenbanken (von vor der Themen-/Transkript-/OCR-Funktion)
    # fehlen die Spalten — ALTER TABLE nachrüsten, statt eine Migration zu verlangen.
    for column_sql in ("ALTER TABLE events ADD COLUMN topics TEXT",
                        "ALTER TABLE events ADD COLUMN transcript TEXT",
                        "ALTER TABLE events ADD COLUMN ocr_text TEXT"):
        try:
            conn.execute(column_sql)
        except sqlite3.OperationalError:
            pass  # Spalte existiert schon — Normalfall nach dem ersten Start mit dieser Version
    return conn


def _pack(vec):
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob):
    n = len(blob) // 4
    return struct.unpack(f"{n}f", blob)


def index_event(filename, base_dir, description=None, topics=None, transcript=None, ocr_text=None):
    """Von ai_analyze.py, transcribe_audio.py und/oder text_recognize.py
    aufgerufen. Liest einen evtl. schon vorhandenen Eintrag, ergänzt NUR die
    hier übergebenen Felder (None = "unverändert lassen", nicht "leeren")
    und berechnet das Embedding aus allem bisher Bekannten (Beschreibung +
    Themen + Transkript + erkannter Text) neu — so spielt es keine Rolle,
    in welcher Reihenfolge die einzelnen Analyse-Schritte fertig werden,
    keiner überschreibt einen anderen. ocr_text ist bewusst eine EIGENE
    Spalte, nicht im transcript-Feld untergebracht — sonst würde erkannter
    Bildtext die echte Sprach-Transkription verdrängen oder umgekehrt,
    je nachdem welcher Schritt zuletzt schreibt.
    topics: Liste von Themen-Namen (optional)."""
    try:
        conn = _connect()
        row = conn.execute(
            "SELECT description, topics, transcript, ocr_text FROM events WHERE filename = ?", (filename,)
        ).fetchone()
    except Exception as e:
        print(f"⚠️ Suchindex-Lesefehler: {e}")
        return

    old_description, old_topics, old_transcript, old_ocr_text = row if row else (None, None, None, None)
    final_description = description if description is not None else (old_description or "")
    final_topics = ", ".join(topics) if topics is not None else old_topics
    final_transcript = transcript if transcript is not None else old_transcript
    final_ocr_text = ocr_text if ocr_text is not None else old_ocr_text

    if not final_description and not final_transcript and not final_ocr_text:
        conn.close()
        return  # nichts Sinnvolles zu indexieren

    combined_text = " ".join(filter(None, [final_description, final_topics, final_transcript, final_ocr_text]))
    embedding = None
    model = _get_model()
    if model is not None and combined_text.strip():
        try:
            embedding = _pack(model.encode(combined_text, normalize_embeddings=True).tolist())
        except Exception as e:
            print(f"⚠️ Konnte Such-Embedding nicht berechnen: {e}")

    try:
        conn.execute(
            "INSERT INTO events (filename, base_dir, description, embedding, topics, transcript, ocr_text, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%s','now')) "
            "ON CONFLICT(filename) DO UPDATE SET base_dir=excluded.base_dir, "
            "description=excluded.description, embedding=excluded.embedding, "
            "topics=excluded.topics, transcript=excluded.transcript, ocr_text=excluded.ocr_text, "
            "updated_at=excluded.updated_at",
            (filename, base_dir, final_description, embedding, final_topics, final_transcript, final_ocr_text)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Suchindex-Update fehlgeschlagen: {e}")


def remove_event(filename):
    """Beim endgültigen Löschen eines Videos aufrufen."""
    try:
        conn = _connect()
        conn.execute("DELETE FROM events WHERE filename = ?", (filename,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def update_location(filename, new_base_dir):
    """Beim Archivieren aufrufen — sonst zeigt ein Suchtreffer auf den
    falschen (nicht mehr existierenden) Video-Pfad."""
    try:
        conn = _connect()
        conn.execute("UPDATE events SET base_dir = ? WHERE filename = ?", (new_base_dir, filename))
        conn.commit()
        conn.close()
    except Exception:
        pass


def search(query, top_k=30):
    """Kombiniert Volltext + semantische Ähnlichkeit.
    Gibt Liste von (filename, base_dir, description, score) zurück,
    nach Score absteigend sortiert."""
    query = (query or "").strip()
    if not query:
        return []
    try:
        conn = _connect()
        rows = conn.execute("SELECT filename, base_dir, description, embedding, topics, transcript, ocr_text FROM events").fetchall()
        conn.close()
    except Exception:
        return []

    q_lower = query.lower()
    model = _get_model()
    q_embedding = None
    if model is not None:
        try:
            q_embedding = model.encode(query, normalize_embeddings=True)
        except Exception:
            q_embedding = None

    results = []
    for filename, base_dir, description, embedding_blob, topics_text, transcript_text, ocr_text in rows:
        text_hit = (
            q_lower in (description or "").lower()
            or q_lower in (topics_text or "").lower()
            or q_lower in (transcript_text or "").lower()
            or q_lower in (ocr_text or "").lower()
        )
        sim = 0.0
        if q_embedding is not None and embedding_blob:
            try:
                vec = _unpack(embedding_blob)
                sim = float(sum(a * b for a, b in zip(q_embedding, vec)))  # beide normalisiert -> Cosine == Dot-Product
            except Exception:
                sim = 0.0
        if not (text_hit or sim >= SEMANTIC_THRESHOLD):
            continue
        # Volltext-Treffer bekommt einen festen Bonus — exakte Wörter sollen
        # immer vor rein semantischer Ähnlichkeit ranken.
        score = sim + (0.5 if text_hit else 0.0)
        results.append((filename, base_dir, description, score))

    results.sort(key=lambda r: r[3], reverse=True)
    return results[:top_k]
