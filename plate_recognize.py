"""
plate_recognize.py - Fahrzeugerkennung (YOLO, COCO-Klassen car/truck/bus/
motorcycle) + Kennzeichen-OCR (EasyOCR) auf den Filmstrip-Frames einer
Aufnahme. Läuft postprocess-seitig, genau wie face_recognize.py -- eigene,
schlanke Modell-Instanz statt Zugriff auf den Zustand der Live-Pipeline.

Ergebnis wird in plates_db.py gespeichert und dort sofort gegen bereits
bekannte ("benannte") Fahrzeuge abgeglichen.
"""
import os
import sys
import glob
import re

VEHICLE_CLASS_NAMES = {"car", "truck", "bus", "motorcycle"}

# Wie sehr ein gelesener Text wie ein Kennzeichen aussehen muss, um
# gespeichert zu werden -- bewusst großzügig (Länder-Formate unterscheiden
# sich stark), aber verhindert offensichtlichen OCR-Unsinn (z.B. ein
# einzelnes Zeichen oder ein Treffer, der nur aus Sonderzeichen besteht).
PLATE_MIN_CONFIDENCE = 0.35
PLATE_MIN_LENGTH = 4
PLATE_MAX_LENGTH = 12


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
PLATE_RECOGNITION_ENABLED = bool(_settings.get("PLATE_RECOGNITION_ENABLED", False))

try:
    import plates_db
except ImportError:
    plates_db = None

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
        print(f"⚠️ Kennzeichen-Erkennung: YOLO-Modell konnte nicht geladen werden: {e}")
        _yolo_model = False
    return _yolo_model or None


def _get_ocr_reader():
    global _ocr_reader
    if _ocr_reader is not None:
        return _ocr_reader
    try:
        import easyocr
        # Nur lateinische Schrift -- deckt die meisten Kennzeichen-Formate ab
        # und hält die Modellgröße/Ladezeit klein. GPU wird automatisch
        # genutzt, falls verfügbar (torch erkennt das selbst).
        import torch
        use_gpu = torch.cuda.is_available()
        _ocr_reader = easyocr.Reader(['en'], gpu=use_gpu)
    except Exception as e:
        print(f"⚠️ Kennzeichen-Erkennung: EasyOCR konnte nicht geladen werden: {e}")
        _ocr_reader = False
    return _ocr_reader or None


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


def recognize(video_basename, base_dir):
    if not PLATE_RECOGNITION_ENABLED:
        return
    if plates_db is None:
        return
    frame_dir = os.path.join(base_dir, ".thumbs", video_basename, "large")
    if not os.path.isdir(frame_dir):
        return
    frames = sorted(glob.glob(os.path.join(frame_dir, "*.jpg")))
    if not frames:
        return

    model = _get_yolo()
    reader = _get_ocr_reader()
    if model is None or reader is None:
        return

    import cv2
    crop_dir = os.path.join(base_dir, ".thumbs", video_basename, "plates")
    os.makedirs(crop_dir, exist_ok=True)

    saved_count = 0
    matched_count = 0
    seen_texts_this_video = set()  # dieselbe Platte nicht x-fach pro Video speichern

    for i, frame_path in enumerate(frames):
        img = cv2.imread(frame_path)
        if img is None:
            continue
        try:
            results = model.predict(img, verbose=False)[0]
        except Exception as e:
            print(f"⚠️ Fahrzeugerkennung auf Frame {frame_path} fehlgeschlagen: {e}")
            continue

        for j, box in enumerate(results.boxes):
            class_name = model.names[int(box.cls[0])]
            if class_name not in VEHICLE_CLASS_NAMES:
                continue
            x1, y1, x2, y2 = [max(0, int(v)) for v in box.xyxy[0].tolist()]
            vehicle_crop = img[y1:y2, x1:x2]
            if vehicle_crop.size == 0:
                continue

            try:
                ocr_results = reader.readtext(vehicle_crop)
            except Exception as e:
                print(f"⚠️ OCR auf Fahrzeug-Crop fehlgeschlagen: {e}")
                continue

            # WICHTIG: EasyOCR zerlegt ein Kennzeichen öfter mal in mehrere
            # Textboxen (z.B. "B" und "MW 500" statt "B MW 500" als ein
            # Treffer) -- nur den EINEN besten Einzeltreffer zu nehmen würde
            # in so einem Fall regelmäßig an der Mindestlänge scheitern.
            # Stattdessen alle qualifizierenden Treffer zusammenfügen, in der
            # Reihenfolge, in der EasyOCR sie zurückgibt (von links nach
            # rechts durch die Boundingbox-Position bereits näherungsweise
            # sortiert).
            qualifying_results = [(text, conf) for _, text, conf in ocr_results if conf >= PLATE_MIN_CONFIDENCE]
            if not qualifying_results:
                continue
            combined_text = " ".join(text for text, _ in qualifying_results)
            avg_conf = sum(conf for _, conf in qualifying_results) / len(qualifying_results)

            plate_text = _clean_plate_text(combined_text)
            if plate_text is None or plate_text in seen_texts_this_video:
                continue
            seen_texts_this_video.add(plate_text)

            crop_filename = f"{i:04d}_{j}.jpg"
            crop_path = os.path.join(crop_dir, crop_filename)
            try:
                cv2.imwrite(crop_path, vehicle_crop)
            except Exception:
                continue

            plate_id, vehicle_id = plates_db.add_plate(
                f"{video_basename}.mp4", base_dir,
                os.path.join(".thumbs", video_basename, "plates", crop_filename),
                plate_text, float(avg_conf), vehicle_type=class_name
            )
            saved_count += 1
            if vehicle_id is not None:
                matched_count += 1

    if saved_count:
        print(f"✅ Kennzeichen-Erkennung für {video_basename}: {saved_count} Lesung(en) gespeichert, {matched_count} bekannten Fahrzeugen zugeordnet.")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: plate_recognize.py <video_basename> <base_dir>")
        sys.exit(1)
    recognize(sys.argv[1], sys.argv[2])
