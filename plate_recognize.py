"""
plate_recognize.py - Kennzeichen-Abgleich gegen benannte Fahrzeuge.

Läuft KEINEN eigenen OCR-Durchlauf mehr -- das übernimmt jetzt
text_recognize.py einmal, allgemein, über den ganzen Frame. Dieses Modul
bekommt von dort bereits fertig zusammengesetzte Text-Kandidaten
übergeben (Text-Fragmente, die räumlich in einer Fahrzeug-Box lagen,
schon zu einem String kombiniert), validiert sie gegen ein plausibles
Kennzeichen-Format, und gleicht sie gegen plates_db.py ab.
"""
import re

# Wie sehr ein gelesener Text wie ein Kennzeichen aussehen muss, um
# gespeichert zu werden -- bewusst großzügig (Länder-Formate unterscheiden
# sich stark), aber verhindert offensichtlichen OCR-Unsinn (z.B. ein
# einzelnes Zeichen oder ein Treffer, der nur aus Sonderzeichen besteht).
PLATE_MIN_LENGTH = 4
PLATE_MAX_LENGTH = 12

try:
    import plates_db
except ImportError:
    plates_db = None


def _clean_plate_text(raw_text):
    """Normalisiert einen OCR-Treffer zu einem plausiblen Kennzeichen-Text:
    Großbuchstaben, nur alphanumerisch + Leerzeichen/Bindestrich, getrimmt.
    Gibt None zurück, wenn das Ergebnis zu kurz/lang oder leer ist."""
    cleaned = re.sub(r"[^A-Za-z0-9\- ]", "", raw_text).strip().upper()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned.replace(" ", "").replace("-", "")) < PLATE_MIN_LENGTH:
        return None
    if len(cleaned) > PLATE_MAX_LENGTH:
        return None
    return cleaned or None


def match_from_candidates(filename, base_dir, candidates):
    """candidates: Liste von (roher_text, confidence, crop_path) -- bereits
    auf Fahrzeug-Boxen eingegrenzt, pro Fahrzeug-Sichtung zusammengefügt,
    und mit einem bereits gespeicherten Fahrzeug-Ausschnittsbild versehen,
    von text_recognize.py. Validiert jeden Kandidaten als plausibles
    Kennzeichen und speichert/verknüpft ihn über plates_db.py."""
    if plates_db is None or not candidates:
        return

    saved_count = 0
    matched_count = 0
    seen_texts_this_video = set()  # dieselbe Platte nicht x-fach pro Video speichern

    for raw_text, confidence, crop_path in candidates:
        plate_text = _clean_plate_text(raw_text)
        if plate_text is None or plate_text in seen_texts_this_video:
            continue
        seen_texts_this_video.add(plate_text)

        plate_id, vehicle_id = plates_db.add_plate(
            filename, base_dir, crop_path, plate_text, float(confidence), vehicle_type=None
        )
        saved_count += 1
        if vehicle_id is not None:
            matched_count += 1

    if saved_count:
        print(f"✅ Kennzeichen-Abgleich für {filename}: {saved_count} Lesung(en) gespeichert, {matched_count} bekannten Fahrzeugen zugeordnet.")
