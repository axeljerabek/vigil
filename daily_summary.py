#!/usr/bin/env python3
"""
daily_summary.py — fasst alle Ereignisse eines Tages oder einer Woche zu
einer zusammenhängenden Erzählung zusammen ("Heute: zwei Lieferungen,
Hundespaziergang um 15 Uhr, sonst nichts Auffälliges").

Bewusst eine REINE Text-Aufgabe: es werden nur die bereits vorhandenen
KI-Beschreibungen aus den .ai.json-Dateien gesammelt und an Ollama zur
Zusammenfassung geschickt — keine Bilder, kein neues Modell, keine neue
Infrastruktur. Nutzt denselben Ollama-Endpunkt und dasselbe Modell wie
ai_analyze.py (OLLAMA_URL / OLLAMA_VISION_MODEL aus den Settings), da
vision-fähige Modelle wie llava auf einem text-fähigen Sprachmodell aufbauen
und reine Text-Prompts ebenfalls beantworten können — kein separates
"Text-Modell" nötig.

Kann als Cronjob laufen (--period day / --period week) oder manuell über den
Dashboard-Button für einen Zeitraum aus der Vergangenheit.
"""
import os
import sys
import json
import glob
import re
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timedelta

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(DIR)
try:
    from config import ALERTS_DIR, SETTINGS_F
except ImportError:
    ALERTS_DIR = "./alerts"
    SETTINGS_F = "pipeline_settings.json"

ARCHIVE_DIR = os.path.join(ALERTS_DIR, "archive")
SUMMARIES_DIR = os.path.join(ALERTS_DIR, ".summaries")

# Dieselbe Namenskonvention wie überall sonst im System:
# <Kamera>_EVENT_<YYYYMMDD>_<HHMMSS>.mp4
FILENAME_RE = re.compile(r"^(.+?)_EVENT_(\d{8})_(\d{6})\.mp4$")


def _load_settings():
    try:
        with open(SETTINGS_F) as f:
            return json.load(f)
    except Exception:
        return {}


def _parse_event_time(filename):
    """Extrahiert Kameraname + Zeitstempel aus dem Dateinamen. Gibt
    (None, None) zurück, falls der Name nicht dem Standard-Schema folgt
    (z.B. bei manuell benannten oder älteren Dateien) -- solche Events
    werden dann einfach übersprungen, statt einen Fehler zu werfen."""
    m = FILENAME_RE.match(os.path.basename(filename))
    if not m:
        return None, None
    camera, date_str, time_str = m.groups()
    try:
        dt = datetime.strptime(date_str + time_str, "%Y%m%d%H%M%S")
    except ValueError:
        return None, None
    return camera, dt


def gather_events(start, end, dirs):
    """Sammelt alle Events mit vorhandener KI-Beschreibung im Zeitraum
    [start, end). Events ohne Beschreibung (KI-Analyse aus, fehlgeschlagen,
    oder noch nicht durchgelaufen) werden übersprungen -- für die
    Zusammenfassung gibt's dann schlicht nichts zu sagen."""
    events = []
    for base_dir in dirs:
        if not os.path.isdir(base_dir):
            continue
        for ai_path in glob.glob(os.path.join(base_dir, "*.ai.json")):
            video_name = os.path.basename(ai_path)[: -len(".ai.json")] + ".mp4"
            camera, dt = _parse_event_time(video_name)
            if dt is None or not (start <= dt < end):
                continue
            try:
                with open(ai_path) as f:
                    meta = json.load(f)
            except Exception:
                continue
            desc = meta.get("description")
            if not desc:
                continue
            events.append({
                "camera": camera,
                "time": dt,
                "description": desc,
                "topics": list((meta.get("topics") or {}).keys()),
            })
    events.sort(key=lambda e: e["time"])
    return events


def build_prompt(events, period_label):
    # Bei einer Woche mit vielen Events lohnt sich der Wochentag im
    # Zeitstempel, bei einem einzelnen Tag reicht die Uhrzeit -- sonst
    # wird der Prompt unnötig unübersichtlich für ein Ollama-Modell mit
    # begrenztem Kontext.
    show_weekday = (events[-1]["time"] - events[0]["time"]).days >= 1 if events else False
    lines = []
    for e in events:
        time_str = e["time"].strftime("%a %H:%M") if show_weekday else e["time"].strftime("%H:%M")
        topics_str = f" [{', '.join(e['topics'])}]" if e["topics"] else ""
        lines.append(f"- {time_str} ({e['camera']}): {e['description']}{topics_str}")
    events_text = "\n".join(lines)
    return (
        f"Below is a chronological log of home security camera events from {period_label}. "
        f"Write a short, natural-language summary (a few sentences, like briefing someone "
        f"who was away) of what happened. Group similar or repeated events together "
        f"(e.g. multiple deliveries, the dog being let out several times) rather than "
        f"listing every single one. Mention anything that seems unusual or worth flagging. "
        f"Do not just restate the log verbatim, write it as a short narrative.\n\n"
        f"{events_text}\n\nSummary:"
    )


def _call_ollama(prompt, settings, timeout=120):
    ollama_url = (settings.get("OLLAMA_URL") or "http://localhost:11434").rstrip("/")
    model = settings.get("OLLAMA_VISION_MODEL") or "llava:latest"
    payload = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode("utf-8")
    req = urllib.request.Request(
        f"{ollama_url}/api/generate", data=payload,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read())
    return (result.get("response") or "").strip()


def summarize(events, period_label, settings):
    if not events:
        return "No recorded events in this period.", None
    prompt = build_prompt(events, period_label)
    try:
        return _call_ollama(prompt, settings), None
    except Exception as e:
        return None, str(e)


def run(period="day", reference_date=None):
    settings = _load_settings()
    reference_date = reference_date or datetime.now()

    if period == "day":
        start = reference_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        period_label = start.strftime("%A, %B %d, %Y")
    elif period == "week":
        start = (reference_date - timedelta(days=reference_date.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end = start + timedelta(days=7)
        period_label = f"the week of {start.strftime('%B %d, %Y')}"
    else:
        raise ValueError(f"Unknown period: {period!r} (expected 'day' or 'week')")

    events = gather_events(start, end, [ALERTS_DIR, ARCHIVE_DIR])
    summary_text, error = summarize(events, period_label, settings)

    result = {
        "period": period,
        "period_label": period_label,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "event_count": len(events),
        "cameras": sorted({e["camera"] for e in events}),
        "summary": summary_text,
        "error": error,
        "generated_at": datetime.now().isoformat(),
    }

    os.makedirs(SUMMARIES_DIR, exist_ok=True)
    out_path = os.path.join(SUMMARIES_DIR, f"{period}_{start.strftime('%Y%m%d')}.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate a daily or weekly summary of vaelen events.")
    parser.add_argument("--period", choices=["day", "week"], default="day")
    parser.add_argument("--date", help="Reference date (YYYY-MM-DD), defaults to today", default=None)
    args = parser.parse_args()
    ref = datetime.strptime(args.date, "%Y-%m-%d") if args.date else None
    result = run(args.period, ref)
    print(json.dumps(result, indent=2))
