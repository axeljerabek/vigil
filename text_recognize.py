"""
text_recognize.py - Allgemeine OCR-Erkennung auf den Filmstrip-Frames einer
Aufnahme. Liest JEDEN Text im Bild -- Hausnummern, Paketaufkleber,
Straßenschilder, Aufschriften, Kennzeichen, alles -- statt nur auf
Fahrzeug-Boxen beschränkt zu sein. Ergebnis geht in ocr_db.py (allgemein
durchsuchbar) UND wird an plate_recognize.py weitergereicht, das daraus
NUR die Treffer herausfiltert, die räumlich in einer Fahrzeug-Box liegen
und wie ein Kennzeichen aussehen -- Kennzeichen-Erkennung ist damit kein
eigener OCR-Durchlauf mehr, sondern ein Filter auf denselben Ergebnissen.

Läuft wie face_recognize.py/plate_recognize.py postprocess-seitig auf den
bereits vorhandenen Filmstrip-Bildern, keine eigene Video-Dekodierung.
"""
import os
import sys
import glob

VEHICLE_CLASS_NAMES = {"car", "truck", "bus", "motorcycle"}

# Ganze Frames zu scannen ist teurer als nur einen kleinen Fahrzeug-Crop
# (mehr Pixel, mehr potenzielle Textregionen) -- deshalb bewusst nur eine
# Stichprobe der Filmstrip-Frames, nicht alle 64. Reicht für die meisten
# Zwecke: derselbe Text (Hausnummer, Paketaufkleber) ist über mehrere
# Sekunden hinweg sichtbar, muss nicht in jedem einzelnen Frame neu
# gefunden werden.
MAX_FRAMES_TO_SCAN = 10
TEXT_MIN_CONFIDENCE = 0.35


def _load_settings():
    try:
        from config import PROJECT_ROOT
        import json
        settings_path = os.path.join(PROJECT_ROOT, "pipeline_settings.json")
        if os.path.exists(settings_path):
            with open(settings_path) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


_settings = _load_settings()
OCR_ENABLED = bool(_settings.get("OCR_ENABLED", False))
PLATE_MATCHING_ENABLED = bool(_settings.get("PLATE_RECOGNITION_ENABLED", False))

try:
    import ocr_db
except ImportError:
    ocr_db = None

try:
    import plates_db
except ImportError:
    plates_db = None

try:
    import search_index
except ImportError:
    search_index = None

_yolo_model = None
_ocr_reader = None


def _get_yolo():
    global _yolo_model
    if _yolo_model is not None:
        return _yolo_model
    try:
        from ultralytics import YOLO
        from config import MODEL_PATH
        _yolo_model = YOLO(MODEL_PATH)
    except Exception as e:
        print(f"⚠️ Texterkennung: YOLO-Modell konnte nicht geladen werden: {e}")
        _yolo_model = False
    return _yolo_model or None


def _get_ocr_reader():
    global _ocr_reader
    if _ocr_reader is not None:
        return _ocr_reader
    try:
        import easyocr
        import torch
        use_gpu = torch.cuda.is_available()
        _ocr_reader = easyocr.Reader(['en'], gpu=use_gpu)
    except Exception as e:
        print(f"⚠️ Texterkennung: EasyOCR konnte nicht geladen werden: {e}")
        _ocr_reader = False
    return _ocr_reader or None


def _bbox_center_inside(text_bbox, vehicle_box):
    """Prüft, ob der Mittelpunkt einer OCR-Textbox innerhalb einer
    Fahrzeug-Bounding-Box liegt -- einfache, robuste Heuristik statt einer
    exakten Überlappungsberechnung, reicht für 'liegt das Kennzeichen auf
    diesem Fahrzeug'."""
    xs = [p[0] for p in text_bbox]
    ys = [p[1] for p in text_bbox]
    cx = sum(xs) / len(xs)
    cy = sum(ys) / len(ys)
    x1, y1, x2, y2 = vehicle_box
    return x1 <= cx <= x2 and y1 <= cy <= y2


def recognize(video_basename, base_dir):
    if not OCR_ENABLED:
        return
    if ocr_db is None:
        return
    frame_dir = os.path.join(base_dir, ".thumbs", video_basename, "large")
    if not os.path.isdir(frame_dir):
        return
    all_frames = sorted(glob.glob(os.path.join(frame_dir, "*.jpg")))
    if not all_frames:
        return

    # Gleichmäßig über die Aufnahme verteilte Stichprobe statt nur der
    # ersten N Frames -- ein Paketaufkleber, der erst nach der Hälfte der
    # Aufnahme ins Bild kommt, soll genauso gefunden werden.
    if len(all_frames) > MAX_FRAMES_TO_SCAN:
        step = len(all_frames) / MAX_FRAMES_TO_SCAN
        frames = [all_frames[int(i * step)] for i in range(MAX_FRAMES_TO_SCAN)]
    else:
        frames = all_frames

    reader = _get_ocr_reader()
    if reader is None:
        return

    model = _get_yolo() if PLATE_MATCHING_ENABLED else None

    import cv2
    filename = f"{video_basename}.mp4"
    seen_texts = set()  # dieselbe Textzeile nicht x-fach aus mehreren Frames speichern
    plate_candidates = []  # (text, confidence, frame_index) -- an plate_recognize.py weitergereicht

    for frame_idx, frame_path in enumerate(frames):
        img = cv2.imread(frame_path)
        if img is None:
            continue

        try:
            ocr_results = reader.readtext(img)
        except Exception as e:
            print(f"⚠️ OCR auf Frame {frame_path} fehlgeschlagen: {e}")
            continue

        # Fahrzeug-Boxen für diesen Frame nur berechnen, wenn Kennzeichen-
        # Abgleich überhaupt an ist -- sonst unnötige YOLO-Inferenz.
        vehicle_boxes = []
        if model is not None:
            try:
                results = model.predict(img, verbose=False)[0]
                for box in results.boxes:
                    class_name = model.names[int(box.cls[0])]
                    if class_name in VEHICLE_CLASS_NAMES:
                        vehicle_boxes.append([float(v) for v in box.xyxy[0].tolist()])
            except Exception as e:
                print(f"⚠️ Fahrzeugerkennung auf Frame {frame_path} fehlgeschlagen: {e}")

        for bbox, text, conf in ocr_results:
            if conf < TEXT_MIN_CONFIDENCE:
                continue
            clean = text.strip()
            if not clean:
                continue

            is_on_vehicle = any(_bbox_center_inside(bbox, vb) for vb in vehicle_boxes)
            source_class = "vehicle" if is_on_vehicle else None

            dedup_key = clean.lower()
            if dedup_key not in seen_texts:
                seen_texts.add(dedup_key)
                ocr_db.add_text(filename, base_dir, frame_idx, clean, float(conf), bbox=bbox, source_class=source_class)

        # Kennzeichen-Kandidaten getrennt aufbauen: pro Fahrzeug-Box ALLE
        # überlappenden Text-Fragmente zu einem String zusammenfügen (z.B.
        # "B" + "MW 500" -> "B MW 500") statt sie als unabhängige Kandidaten
        # zu behandeln -- EasyOCR zerlegt ein Kennzeichen regelmäßig in
        # mehrere Textboxen, ein einzelnes Fragment allein fällt oft schon
        # an der Mindestlänge durch. Zusätzlich einen echten Fahrzeug-Crop
        # speichern (nicht nur den ganzen Frame referenzieren) -- sonst wäre
        # das Profilfoto eines benannten Fahrzeugs später die komplette
        # Szene statt eines knappen Fahrzeug-Ausschnitts.
        if PLATE_MATCHING_ENABLED and vehicle_boxes:
            plate_crop_dir = os.path.join(base_dir, ".thumbs", video_basename, "plates")
            os.makedirs(plate_crop_dir, exist_ok=True)
            for vb_idx, vb in enumerate(vehicle_boxes):
                fragments = [(text.strip(), conf) for bbox, text, conf in ocr_results
                             if conf >= TEXT_MIN_CONFIDENCE and text.strip() and _bbox_center_inside(bbox, vb)]
                if not fragments:
                    continue
                combined_text = " ".join(t for t, _ in fragments)
                avg_conf = sum(c for _, c in fragments) / len(fragments)

                x1, y1, x2, y2 = [max(0, int(v)) for v in vb]
                vehicle_crop = img[y1:y2, x1:x2]
                if vehicle_crop.size == 0:
                    continue
                crop_filename = f"{frame_idx:04d}_{vb_idx}.jpg"
                crop_full_path = os.path.join(plate_crop_dir, crop_filename)
                try:
                    cv2.imwrite(crop_full_path, vehicle_crop)
                except Exception:
                    continue
                crop_rel_path = os.path.join(".thumbs", video_basename, "plates", crop_filename)
                plate_candidates.append((combined_text, avg_conf, crop_rel_path))

    # Erkannten Text in den Suchindex einspeisen, damit "Ask Vaelen" und die
    # normale Suche auch nach sichtbarem Text fragen/finden können.
    if search_index is not None and seen_texts:
        try:
            combined = ocr_db.get_combined_text_for_search(filename)
            if combined:
                search_index.index_event(filename, base_dir, ocr_text=combined)
        except Exception as e:
            print(f"⚠️ Konnte erkannten Text nicht in den Suchindex einspeisen: {e}")

    if seen_texts:
        print(f"✅ Texterkennung für {video_basename}: {len(seen_texts)} eindeutige Textfragmente gefunden.")

    # Kennzeichen-Abgleich als Filter auf den bereits gesammelten Fahrzeug-
    # Text-Fundstellen -- kein zweiter OCR-Durchlauf mehr.
    if PLATE_MATCHING_ENABLED and plate_candidates and plates_db is not None:
        try:
            import plate_recognize
            plate_recognize.match_from_candidates(filename, base_dir, plate_candidates)
        except Exception as e:
            print(f"⚠️ Kennzeichen-Abgleich fehlgeschlagen: {e}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: text_recognize.py <video_basename> <base_dir>")
        sys.exit(1)
    recognize(sys.argv[1], sys.argv[2])
