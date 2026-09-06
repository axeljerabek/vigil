"""
mqtt_client.py — optionale MQTT-Anbindung für Home Assistant und andere
Automatisierungs-Systeme. Publiziert Kamera-Events (Aufnahme läuft, Aufnahme
fertig mit KI-Beschreibung) an einen MQTT-Broker, inklusive Home-Assistant-
MQTT-Discovery, damit die Entitäten dort automatisch auftauchen -- kein
manuelles YAML nötig.

Zentrales Designprinzip, weil dieses Modul von mehreren, teils
zeitkritischen Stellen aus aufgerufen wird (Kamera-Aufnahme-Loop,
Post-Processing): NIEMALS blockierend, NIEMALS eine Exception werfend.
Jede eigentliche Netzwerk-Operation läuft in einem eigenen, kurzlebigen
Hintergrund-Thread -- ein unerreichbarer oder langsamer Broker darf unter
keinen Umständen die Aufnahme-Pipeline verzögern. Fehler werden geloggt,
nie propagiert.

Off by default. Braucht `pip install paho-mqtt`, aber auch ohne
installiertes Paket stürzt nichts ab -- es wird nur eine Warnung geloggt
und das Event verworfen.
"""
import json
import re
import time
import threading

try:
    import paho.mqtt.publish as _mqtt_publish
    PAHO_AVAILABLE = True
except ImportError:
    PAHO_AVAILABLE = False

try:
    from config import SETTINGS_F
except ImportError:
    SETTINGS_F = "pipeline_settings.json"

_settings_cache = {}
_settings_cache_time = 0.0
_SETTINGS_CACHE_TTL = 5.0  # Sekunden -- Settings-Datei nicht bei jedem einzelnen Event neu einlesen

_discovery_published = set()  # welche Kameras haben ihre HA-Discovery-Config schon bekommen (pro Prozess)


def _get_settings():
    global _settings_cache, _settings_cache_time
    now = time.time()
    if now - _settings_cache_time > _SETTINGS_CACHE_TTL:
        try:
            with open(SETTINGS_F) as f:
                _settings_cache = json.load(f)
        except Exception:
            _settings_cache = {}
        _settings_cache_time = now
    return _settings_cache


def _safe_id(name):
    """Kameranamen können Leerzeichen/Sonderzeichen enthalten -- MQTT-Topics
    und Home-Assistant-Entity-IDs vertragen das nicht zuverlässig."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def _publish_raw_worker(topic, payload, retain, qos, broker, port, username, password):
    if not PAHO_AVAILABLE:
        print("⚠️ [MQTT] paho-mqtt ist nicht installiert -- 'pip install paho-mqtt' im venv, Event wird verworfen.")
        return
    auth = {"username": username, "password": password} if username else None
    try:
        _mqtt_publish.single(
            topic, payload=payload, retain=retain, qos=qos,
            hostname=broker, port=port, auth=auth,
            client_id="vaelen", keepalive=5
        )
    except Exception as e:
        print(f"⚠️ [MQTT] Publish an '{topic}' fehlgeschlagen (Broker erreichbar? Zugangsdaten korrekt?): {e}")


def _fire_and_forget(topic, payload, retain, qos, broker, port, username, password):
    """Startet die eigentliche Netzwerk-Operation in einem eigenen Thread --
    die aufrufende Stelle (Aufnahme-Loop, Post-Processing) wartet nicht auf
    das Ergebnis, bekommt also von einem hängenden/unerreichbaren Broker
    nichts mit."""
    t = threading.Thread(
        target=_publish_raw_worker,
        args=(topic, payload, retain, qos, broker, port, username, password),
        daemon=True
    )
    t.start()


def publish(topic_suffix, payload, retain=False, qos=0):
    """Publiziert ein Event unter <MQTT_TOPIC_PREFIX>/<topic_suffix>.
    payload wird automatisch zu JSON, falls es kein String ist. Tut
    still und leise nichts, wenn MQTT nicht aktiviert oder kein Broker
    konfiguriert ist -- kein Fehler, kein Log-Spam im Normalfall (MQTT aus)."""
    settings = _get_settings()
    if not settings.get("MQTT_ENABLED", False):
        return
    broker = (settings.get("MQTT_BROKER") or "").strip()
    if not broker:
        return
    port = int(settings.get("MQTT_PORT", 1883) or 1883)
    username = settings.get("MQTT_USERNAME") or None
    password = settings.get("MQTT_PASSWORD") or None
    prefix = (settings.get("MQTT_TOPIC_PREFIX") or "vaelen").strip("/") or "vaelen"
    full_topic = f"{prefix}/{topic_suffix}"
    if not isinstance(payload, str):
        payload = json.dumps(payload)
    _fire_and_forget(full_topic, payload, retain, qos, broker, port, username, password)


def publish_ha_discovery(camera_name):
    """Veröffentlicht Home-Assistant-MQTT-Discovery-Konfiguration für eine
    Kamera -- HA erstellt die Entitäten dann automatisch (Settings ->
    Devices & Services -> MQTT), ohne dass von Hand YAML geschrieben werden
    muss. Retained, damit HA sie auch nach einem HA-Neustart wiederfindet.
    Wird pro Kamera nur einmal pro Prozesslauf gesendet (nicht bei jedem
    einzelnen Event erneut), da sich die Konfiguration selbst nicht ändert."""
    settings = _get_settings()
    if not settings.get("MQTT_ENABLED", False) or not settings.get("MQTT_HA_DISCOVERY", True):
        return
    if camera_name in _discovery_published:
        return
    broker = (settings.get("MQTT_BROKER") or "").strip()
    if not broker:
        return
    port = int(settings.get("MQTT_PORT", 1883) or 1883)
    username = settings.get("MQTT_USERNAME") or None
    password = settings.get("MQTT_PASSWORD") or None
    prefix = (settings.get("MQTT_TOPIC_PREFIX") or "vaelen").strip("/") or "vaelen"
    safe_name = _safe_id(camera_name)

    device = {
        "identifiers": [f"vaelen_{safe_name}"],
        "name": f"vaelen {camera_name}",
        "manufacturer": "vaelen",
    }
    entities = [
        ("binary_sensor", f"vaelen_{safe_name}_recording", {
            "name": f"{camera_name} Recording",
            "unique_id": f"vaelen_{safe_name}_recording",
            "state_topic": f"{prefix}/{safe_name}/recording",
            "payload_on": "ON",
            "payload_off": "OFF",
            "device_class": "motion",
            "device": device,
        }),
        ("sensor", f"vaelen_{safe_name}_last_event", {
            "name": f"{camera_name} Last Event",
            "unique_id": f"vaelen_{safe_name}_last_event",
            "state_topic": f"{prefix}/{safe_name}/last_event_summary",
            "device": device,
        }),
    ]
    # Home-Assistant-Discovery-Topics liegen fest unter "homeassistant/",
    # UNABHÄNGIG vom eigenen MQTT_TOPIC_PREFIX -- HA sucht nur dort.
    for component, object_id, config in entities:
        discovery_topic = f"homeassistant/{component}/{object_id}/config"
        _fire_and_forget(discovery_topic, json.dumps(config), True, 0, broker, port, username, password)

    _discovery_published.add(camera_name)


def publish_recording_state(camera_name, is_recording):
    """ON/OFF, retained -- damit Home Assistant beim eigenen Neustart sofort
    den aktuellen Zustand kennt, statt bis zum nächsten Event zu warten."""
    publish_ha_discovery(camera_name)
    safe_name = _safe_id(camera_name)
    publish(f"{safe_name}/recording", "ON" if is_recording else "OFF", retain=True)


def publish_event_analyzed(camera_name, description, topics, filename):
    """Wird nach abgeschlossener KI-Analyse aufgerufen (aus postprocess.py) --
    liefert sowohl eine kompakte Text-Zeile (für die simple HA-Sensor-
    Anzeige) als auch das volle Event als JSON (für eigene HA-Automationen,
    die mehr als nur den Text brauchen)."""
    publish_ha_discovery(camera_name)
    safe_name = _safe_id(camera_name)
    summary = (description or "")[:255]  # HA-Sensor-States haben ein Zeichenlimit
    publish(f"{safe_name}/last_event_summary", summary, retain=True)
    publish(f"{safe_name}/event", {
        "camera": camera_name,
        "filename": filename,
        "description": description,
        "topics": list((topics or {}).keys()),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
