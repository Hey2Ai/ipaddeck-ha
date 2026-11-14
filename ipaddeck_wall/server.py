#!/usr/bin/env python3
"""iPadDeck Wohnzimmer: a kiosk dashboard for an iPad, served directly by Home Assistant.

Runs as a Home Assistant app (add-on). It talks to Home Assistant through the Supervisor proxy with the
app's own SUPERVISOR_TOKEN, so no long-lived token exists anywhere. The iPad only ever sees this server.

Only entities that appear in the layout file can be read or controlled, with a fixed set of actions.
Standard library only.
"""
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HA = os.environ.get("HA_URL", "http://supervisor/core/api")
SUP_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
PORT = int(os.environ.get("PORT", "8099"))
WEB = os.path.join(os.path.dirname(__file__), "web")
DATA = os.environ.get("DATA_DIR", "/data")
LAYOUT_PATHS = [os.environ.get("LAYOUT", "/homeassistant/dashboards/ipaddeck_wall.json"), os.path.join(os.path.dirname(__file__), "wall.default.json")]
ID_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ---------------------------------------------------------------- access code

def load_code():
    opts = {}
    try:
        with open(os.path.join(DATA, "options.json")) as f:
            opts = json.load(f)
    except (OSError, ValueError):
        pass
    code = str(opts.get("code") or "").strip().lower()
    if code:
        return code
    path = os.path.join(DATA, "code")
    try:
        with open(path) as f:
            code = f.read().strip()
    except OSError:
        code = ""
    if not code:
        code = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(12))
        with open(path, "w") as f:
            f.write(code)
    return code


CODE = load_code()


# ---------------------------------------------------------------- layout

def options():
    try:
        with open(os.path.join(DATA, "options.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def layout():
    # 1) the app's own configuration (private, set in the app's Configuration tab)
    raw = options().get("layout")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except ValueError as e:
            log("Layout in der Konfiguration ist kein gültiges JSON: %s" % e)
    # 2) a file in Home Assistant's config folder, 3) the empty default
    for p in LAYOUT_PATHS:
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            continue
    return {"rooms": [], "scenes": []}


def allowed(lay=None):
    ids = set()

    def walk(v):
        if isinstance(v, str):
            if ID_RE.match(v):
                ids.add(v)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
    walk(lay if lay is not None else layout())
    return ids


# ---------------------------------------------------------------- Home Assistant

def ha(method, path, body=None, raw=False):
    """Returns (status, parsed json | bytes | None). status 0 = unreachable."""
    req = urllib.request.Request(HA + path, method=method)
    req.add_header("Authorization", "Bearer " + SUP_TOKEN)
    req.add_header("Content-Type", "application/json")
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=8) as r:
            payload = r.read()
            if raw:
                return r.status, payload
            return r.status, (json.loads(payload) if payload else None)
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, OSError, ValueError) as e:
        log("Home Assistant nicht erreichbar (%s): %s" % (path, e))
        return 0, None


def error_name(status):
    if status in (401, 403):
        return "auth"
    if status in (400, 404, 405, 422, 500):
        return "rejected"
    return "offline"


TURN_ON, TURN_OFF, PAUSE, PLAY = 128, 256, 1, 16384


def media_power(entity, action):
    """Speakers like Sonos have no turn_on/turn_off: map power to play/pause where needed."""
    status, st = ha("GET", "/states/" + entity)
    sf = int(num(((st or {}).get("attributes") or {}).get("supported_features")) or 0) if status == 200 else 0
    on = (st or {}).get("state") not in ("off", "standby", "unavailable", None)
    if action == "toggle":
        action = "off" if on else "on"
    if action == "on":
        if sf & TURN_ON:
            return "media_player/turn_on"
        if sf & PLAY and on:   # a switched-off TV cannot be woken by "play"
            return "media_player/media_play"
        return None
    if sf & TURN_OFF:
        return "media_player/turn_off"
    if sf & PAUSE:
        return "media_player/media_pause"
    return None


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def states():
    lay = layout()
    ids = allowed(lay)
    status, data = ha("GET", "/states")
    if status != 200 or not isinstance(data, list):
        return {"error": error_name(status)}
    alert_cfg = lay.get("alerts") or {}
    battery_below = num(alert_cfg.get("batteryBelow"))
    low_batteries, updates, out = [], [], {}
    for s in data:
        eid = s.get("entity_id", "")
        a = s.get("attributes") or {}
        st = s.get("state", "")
        if battery_below is not None and eid.startswith("sensor.") and "filter" not in eid \
                and a.get("device_class") == "battery" and num(st) is not None and num(st) <= battery_below:
            name = a.get("friendly_name") or eid
            for suffix in (" Battery level", " Battery Level", " Batterie", " Battery", " Akku"):
                if name.endswith(suffix):
                    name = name[: -len(suffix)]
            low_batteries.append({"name": name, "level": int(num(st))})
        if alert_cfg.get("updates") and eid.startswith("update.") and st == "on":
            updates.append(a.get("title") or a.get("friendly_name") or eid)
        if eid not in ids:
            continue
        slim = {"state": st}
        if num(a.get("brightness")) is not None:
            slim["bri"] = int(round(num(a["brightness"]) / 255 * 100))
        for src, dst in (("color_temp_kelvin", "kelvin"), ("min_color_temp_kelvin", "kmin"), ("max_color_temp_kelvin", "kmax"),
                         ("current_position", "pos"), ("supported_features", "sf"), ("percentage", "pct")):
            if num(a.get(src)) is not None:
                slim[dst] = int(num(a[src]))
        for src, dst in (("current_temperature", "cur"), ("temperature", "target"), ("min_temp", "tmin"),
                         ("max_temp", "tmax"), ("target_temp_step", "tstep"), ("humidity", "hum")):
            if num(a.get(src)) is not None:
                slim[dst] = num(a[src])
        modes = a.get("supported_color_modes")
        if isinstance(modes, list):
            slim["ct"] = "color_temp" in modes
            slim["dim"] = not (len(modes) == 1 and modes[0] == "onoff")
        if isinstance(a.get("hvac_modes"), list):
            slim["modes"] = a["hvac_modes"]
        if isinstance(a.get("hvac_action"), str):
            slim["action"] = a["hvac_action"]
        if isinstance(a.get("preset_modes"), list):
            slim["presets"] = a["preset_modes"]
        if isinstance(a.get("preset_mode"), str):
            slim["preset"] = a["preset_mode"]
        if num(a.get("volume_level")) is not None:
            slim["vol"] = int(round(num(a["volume_level"]) * 100))
        if isinstance(a.get("is_volume_muted"), bool):
            slim["muted"] = a["is_volume_muted"]
        for src, dst in (("media_title", "title"), ("media_artist", "artist"), ("unit_of_measurement", "unit"),
                         ("temperature_unit", "unit")):
            if isinstance(a.get(src), str):
                slim[dst] = a[src]
        out[eid] = slim
    result = {"states": out}
    if alert_cfg:
        result["alerts"] = {"batteries": sorted(low_batteries, key=lambda b: b["level"]), "updates": updates}
    return result


def call(entity, action, value):
    lay = layout()
    if entity not in allowed(lay):
        return 403, {"error": "not_allowed"}
    domain = entity.split(".")[0]
    data = {"entity_id": entity}
    bad = (400, {"error": "bad_value"})

    def as_int(lo, hi):
        try:
            v = int(value)
        except (TypeError, ValueError):
            return None
        return v if lo <= v <= hi else None

    if domain == "media_player" and action == "favorite":
        return play_favorite(lay, entity, value or "")

    service = None
    if domain == "media_player" and action in ("toggle", "on", "off"):
        service = media_power(entity, action)
        if not service:
            return 400, {"error": "not_supported"}
    elif action == "toggle" and domain in ("light", "switch", "fan", "input_boolean", "automation"):
        service = domain + "/toggle"
    elif action in ("on", "off") and domain in ("light", "switch", "fan"):
        service = "%s/turn_%s" % (domain, action)
    elif domain == "light" and action == "brightness":
        v = as_int(1, 100)
        if v is None:
            return bad
        data["brightness_pct"] = v
        service = "light/turn_on"
    elif domain == "light" and action == "kelvin":
        v = as_int(1500, 9000)
        if v is None:
            return bad
        data["color_temp_kelvin"] = v
        service = "light/turn_on"
    elif domain == "cover" and action in ("open", "close", "stop"):
        service = "cover/%s_cover" % action
    elif domain == "cover" and action == "position":
        v = as_int(0, 100)
        if v is None:
            return bad
        data["position"] = v
        service = "cover/set_cover_position"
    elif domain == "climate" and action == "temp":
        v = num(value)
        if v is None or not 5 <= v <= 30:
            return bad
        data["temperature"] = v
        service = "climate/set_temperature"
    elif domain == "climate" and action == "mode":
        if value not in ("heat", "cool", "off", "auto", "heat_cool"):
            return bad
        data["hvac_mode"] = value
        service = "climate/set_hvac_mode"
    elif domain == "fan" and action == "percentage":
        v = as_int(0, 100)
        if v is None:
            return bad
        data["percentage"] = v
        service = "fan/set_percentage"
    elif domain == "fan" and action == "preset":
        if value not in ("auto", "night", "sleep", "eco", "boost"):
            return bad
        data["preset_mode"] = value
        service = "fan/set_preset_mode"
    elif domain == "media_player" and action in ("playpause", "next", "previous"):
        service = {"playpause": "media_player/media_play_pause", "next": "media_player/media_next_track",
                   "previous": "media_player/media_previous_track"}[action]
    elif domain == "media_player" and action == "volume":
        v = as_int(0, 100)
        if v is None:
            return bad
        data["volume_level"] = v / 100
        service = "media_player/volume_set"
    elif domain == "media_player" and action == "mute":
        if value not in ("0", "1"):
            return bad
        data["is_volume_muted"] = value == "1"
        service = "media_player/volume_mute"
    elif domain == "button" and action == "press":
        service = "button/press"
    elif domain == "scene" and action == "activate":
        service = "scene/turn_on"
    elif domain == "script" and action == "activate":
        service = "script/turn_on"
    if not service:
        return 400, {"error": "bad_action"}
    status, _ = ha("POST", "/services/" + service, data)
    return (200, {"ok": True}) if 200 <= status < 300 else (502, {"error": error_name(status)})


def play_favorite(lay, entity, name):
    item = next((m for m in lay.get("music") or [] if m.get("name") == name and m.get("player") in (None, entity)), None)
    if not item:
        return 403, {"error": "not_allowed"}
    title = (item.get("favorite") or name).lower()
    status, data = ha("GET", "/states/sensor.sonos_favorites")
    items = ((data or {}).get("attributes") or {}).get("items") if status == 200 else None
    if not isinstance(items, dict):
        return 502, {"error": "no_favorites" if status == 200 else error_name(status)}
    fav = next((k for k, v in items.items() if str(v).lower() == title), None)
    if not fav:
        return 404, {"error": "favorite_missing"}
    status, _ = ha("POST", "/services/media_player/play_media",
                   {"entity_id": entity, "media_content_id": fav, "media_content_type": "favorite_item_id"})
    return (200, {"ok": True}) if 200 <= status < 300 else (502, {"error": error_name(status)})


def todo_items(entity):
    if not entity.startswith("todo.") or entity not in allowed():
        return 403, {"error": "not_allowed"}
    status, data = ha("POST", "/services/todo/get_items?return_response",
                      {"entity_id": entity, "status": ["needs_action", "completed"]})
    try:
        items = data["service_response"][entity]["items"]
    except (TypeError, KeyError):
        return 502, {"error": error_name(status)}
    return 200, {"items": [{"uid": i.get("uid", ""), "summary": i.get("summary", ""),
                            "done": i.get("status") == "completed"} for i in items]}


def todo_change(entity, action, value):
    if not entity.startswith("todo.") or entity not in allowed():
        return 403, {"error": "not_allowed"}
    text = (value or "").strip()
    if not text or len(text) > 120:
        return 400, {"error": "bad_value"}
    if action == "add":
        service, body = "todo/add_item", {"entity_id": entity, "item": text}
    elif action in ("done", "undo"):
        service = "todo/update_item"
        body = {"entity_id": entity, "item": text, "status": "completed" if action == "done" else "needs_action"}
    else:
        return 400, {"error": "bad_action"}
    status, _ = ha("POST", "/services/" + service, body)
    return (200, {"ok": True}) if 200 <= status < 300 else (502, {"error": error_name(status)})


_forecast = {"at": 0, "entity": "", "data": None}
_forecast_lock = threading.Lock()


def forecast(entity):
    if not entity.startswith("weather.") or entity not in allowed():
        return 403, {"error": "not_allowed"}
    with _forecast_lock:
        if _forecast["entity"] == entity and time.time() - _forecast["at"] < 900 and _forecast["data"]:
            return 200, _forecast["data"]
    status, data = ha("POST", "/services/weather/get_forecasts?return_response", {"entity_id": entity, "type": "daily"})
    try:
        days = data["service_response"][entity]["forecast"]
    except (TypeError, KeyError):
        return 502, {"error": error_name(status)}
    out = {"days": [{"date": f.get("datetime", ""), "cond": f.get("condition", ""), "max": f.get("temperature"),
                     "min": f.get("templow"), "rain": f.get("precipitation")} for f in days[:6]]}
    for d in out["days"]:
        for k in [k for k, v in d.items() if v is None]:
            del d[k]
    with _forecast_lock:
        _forecast.update(at=time.time(), entity=entity, data=out)
    return 200, out


def image(entity):
    if not entity.startswith("image.") or entity not in allowed():
        return None
    status, data = ha("GET", "/image_proxy/" + entity, raw=True)
    return data if status == 200 else None


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "iPadDeckWall/1.0"

    def log_message(self, fmt, *args):
        pass

    def send(self, status, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def params(self):
        url = urllib.parse.urlparse(self.path)
        p = {k: v[0] for k, v in urllib.parse.parse_qs(url.query, keep_blank_values=True).items()}
        if self.command == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            if 0 < n <= 64 * 1024:
                body = self.rfile.read(n).decode("utf-8", "replace")
                p.update({k: v[0] for k, v in urllib.parse.parse_qs(body, keep_blank_values=True).items()})
        return url.path, p

    def static(self, name, ctype):
        try:
            with open(os.path.join(WEB, name), "rb") as f:
                self.send(200, f.read(), ctype)
        except OSError:
            self.send(404, "not found", "text/plain")

    def handle_any(self):
        path, p = self.params()
        if path in ("/", "/index.html"):
            return self.static("index.html", "text/html; charset=utf-8")
        if path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
            return self.static("apple-touch-icon.png", "image/png")
        if p.get("k") != CODE:
            return self.send(403, "bad token", "text/plain")
        m = self.command
        if m == "GET" and path == "/info":
            return self.send(200, {"role": "wall", "mac": "Home Assistant"})
        if m == "GET" and path == "/ha/config":
            return self.send(200, layout())
        if m == "GET" and path == "/ha/states":
            return self.send(200, states())
        if m == "POST" and path == "/ha/call":
            return self.send(*call(p.get("entity", ""), p.get("action", ""), p.get("value")))
        if m == "GET" and path == "/ha/todo":
            return self.send(*todo_items(p.get("entity", "")))
        if m == "POST" and path == "/ha/todo":
            return self.send(*todo_change(p.get("entity", ""), p.get("action", ""), p.get("value", "")))
        if m == "GET" and path == "/ha/forecast":
            return self.send(*forecast(p.get("entity", "")))
        if m == "GET" and path == "/ha/image":
            data = image(p.get("entity", ""))
            return self.send(200, data, "image/jpeg") if data else self.send(404, "no image", "text/plain")
        return self.send(404 if path.startswith("/ha/") or path == "/info" else 403, "not allowed", "text/plain")

    do_GET = handle_any
    do_POST = handle_any


if __name__ == "__main__":
    log("iPadDeck Wohnzimmer läuft auf Port %d." % PORT)
    log("Link fürs iPad: http://homeassistant.local:%d/#k=%s" % (PORT, CODE))
    src = "App-Konfiguration" if options().get("layout") else next((p for p in LAYOUT_PATHS if os.path.exists(p)), "keins")
    log("Layout: " + src)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
