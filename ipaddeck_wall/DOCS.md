# iPadDeck Wohnzimmer

Kiosk-Dashboard für ein iPad. Läuft direkt auf Home Assistant und spricht über die Supervisor-API mit Home Assistant.
Es gibt keinen langlebigen Token: Die App nutzt ihren eigenen, internen Zugang.

- iPad-Link: `http://homeassistant.local:8099/#k=<code>` (der Code steht im Log der App oder in der Option `code`).
- Layout (Räume, Kacheln, Szenen): Option `layout` (JSON) im Reiter „Konfiguration“, alternativ die Datei
  `/config/dashboards/ipaddeck_wall.json`. Nur dort genannte Entitäten sind erreichbar.
- Ohne eigene Layout-Datei wird `wall.default.json` aus der App benutzt.
