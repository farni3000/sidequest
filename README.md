# sidequest – KI-gestützter Radrouten-Generator

Erzeugt auf Anfrage individuelle, fahrbare Radrouten anhand von Fahrradart,
Distanz, einem „Heat“-Regler und Freitext-Wünschen. Die App sucht selbstständig
interessante Abschnitte (Aussichtspunkte, Gipfel, MTB-Trails, historische Orte)
in der Nähe der Route, lässt eine KI die lohnendsten auswählen und berechnet
daraus eine reale Strecke. Ein Vision-Modell entwirft die Streckenführung anhand
einer Karte mit, prüft die fertige Route und korrigiert sie bei Bedarf. Ergebnis
ist eine **GPX-Datei** mit ausgedünnten, wichtigen Wegpunkten zum Import in
Komoot oder jede andere Karten-App.

> Es gibt keine offene Komoot-API zum Anlegen von Touren – daher ist der
> GPX-Export der Zielpfad. Strava-Heatmap-Daten sind bewusst **nicht** Teil von
> Phase 1.

## Funktionsumfang

- Direkt bedienbares Formular ohne Dropdowns: Fahrradart als Klick-Karten
  (Rennrad / Gravel / MTB mit Härtegrad XC / All Mountain / Enduro /
  Trekking-Wandern), Start/Ziel oder Rundtour als Segmente, Distanz- und
  Heat-Slider (0–100) sowie Freitext mit anklickbaren Wunsch-Vorschlägen.
- Routing über austauschbare Provider mit automatischem Fallback:
  - **BRouter** (self-hosted, Docker) – primär für MTB/Trekking
  - **OpenRouteService** (Cloud, Free Tier) – primär für Rennrad/Gravel
  - **GraphHopper** – optionaler dritter Adapter
- POI-Suche über die **Overpass API** im Korridor; Korridorbreite skaliert mit
  dem Heat-Wert (500 m – 8 km).
- **deepseek** (OpenAI-kompatibel) wählt per JSON-Modus die Highlights aus und
  ordnet sie; ohne Key/bei Fehler greift eine distanzbasierte Auswahl.
- **Vision-Routenentwurf**: Vor dem Routing bekommt das Bildmodell eine
  Übersichtskarte mit Korridor und POI-Kandidaten und schlägt geordnete
  Via-Punkte auf schönen Abschnitten vor. Diese fließen direkt ins Routing ein.
- **Automatische Streckenprüfung**: Ein Vision-Modell bekommt die
  Originalbeschreibung und ein serverseitig gerendertes Kartenbild der Route.
  Passt die Route nicht, folgt eine Korrekturrunde mit POI-Änderungen und/oder
  freien Zwischenwegpunkten – maximal drei Reviews und zwei Korrekturen.
- **Ausgedünnter GPX-Export**: Statt des kompletten Tracks enthält die GPX-Datei
  nur wichtige Wegpunkte (Start/Ziel, Highlights, markante Kurven, schöne
  Abschnitte, ca. alle 3–5 km). Komoot gleicht beim Import an bekannte Wege an
  und plant die Straßen dazwischen. Die App zeigt weiterhin die vollständige
  berechnete Route.
- **Dynamische Modellwahl**: Modelle und Reasoning-Stufe werden über die
  `/models`-Metadaten des Providers ermittelt (aktuell DeepSeek V4.1 Flash mit
  `reasoning_effort: max`). Neue Modelle werden automatisch berücksichtigt.
- Ergebnisansicht im warmen „Alpine Pine“-Design mit heller Topo-Karte,
  Höhenprofil (Chart.js), Highlight-Timeline mit Begründung und GPX-Download.
  Während der Prüfung zeigt ein Live-Log den aktuellen Stand; am Ende bleiben
  Score, Unsicherheit und verbleibende Hinweise sichtbar.
  Ein Button „Das ist nix. Noch mal von vorne anfangen!“ startet die Planung
  direkt erneut mit den aktuell gesetzten Eingaben (inkl. Hinweis, vorher
  Beschreibung, Distanz oder Heat anzupassen).
  Alle Auswahl- und Eingabeelemente sind auf direkte Klicks ausgelegt.
- SQLModel/SQLite-Persistenz; alle relevanten Tabellen haben von Beginn an ein
  nullable `user_id`-Feld für spätere Multi-User-Erweiterung.

## Projektstruktur

```
sidequest/
├── docker-compose.yml
├── .env.example
├── backend/
│   ├── pyproject.toml
│   ├── app/
│   │   ├── main.py                 # FastAPI-Routen + NDJSON-Statusstream
│   │   ├── config.py               # pydantic-settings
│   │   ├── models.py               # SQLModel (RoutePlan, Highlight, PlanShapingPoint)
│   │   ├── schemas.py
│   │   ├── geocoding.py            # ORS-Geocoding + Nominatim-Fallback
│   │   ├── corridor.py             # Heat -> Korridorbreite/Radius
│   │   ├── map_image.py            # Routenbilder für Entwurf, Review und GPX
│   │   ├── gpx_export.py           # GPX 1.1 (voller Track oder sparse Wegpunkte)
│   │   ├── ai/                     # Modell-Discovery, Routenentwurf, Review/Korrektur
│   │   ├── routing/                # base, brouter, openrouteservice, graphhopper, selector
│   │   ├── poi/                    # overpass, ranking (deepseek)
│   │   ├── services/planner.py     # Orchestrierung inkl. Review-Schleife
│   │   ├── templates/              # Jinja2 + HTMX/Alpine
│   │   └── static/                 # app.js (Leaflet, Chart.js, Statusstream)
│   └── tests/
├── brouter/
│   ├── Dockerfile
│   ├── entrypoint.sh
│   └── setup_segments.sh
└── README.md
```

## Voraussetzungen

- Docker (macOS: `brew install colima docker docker-compose && colima start`)
- Python 3.12 + [uv](https://docs.astral.sh/uv/) (nur für lokale Entwicklung)
- API-Keys: OpenRouteService (kostenlos) und deepseek (optional, für
  KI-Auswahl und Streckenprüfung)

## Schnellstart (ein Befehl)

```bash
./start.sh
```

Das Skript startet bei Bedarf die Colima-VM, lädt fehlende BRouter-Segmente,
baut/startet die Container, wartet auf das Backend und öffnet
<http://localhost:8001> im Browser. Optionen: `--no-browser`, `--region eifel`.

## Setup

```bash
cp .env.example .env
# .env öffnen und DEEPSEEK_API_KEY / ORS_API_KEY eintragen
```

### 1. BRouter-Segmente laden

BRouter braucht vorprozessierte Segmentdateien. Die Startregion ist
**Ardennen/Belgien**:

```bash
./brouter/setup_segments.sh ardennes
```

Weitere Regionen (`eifel`, `benelux`, `germany-west`) oder einzelne Kacheln:

```bash
./brouter/setup_segments.sh germany-west
./brouter/setup_segments.sh E5_N50 E10_N50
```

BRouter-Kacheln sind 5×5-Grad-Quadrate; der Name ist die Südwest-Ecke, z. B.
`E5_N50` = Länge 5–10 °E, Breite 50–55 °N. Neue Regionen lassen sich in
`brouter/setup_segments.sh` in `region_tiles()` ergänzen. Segmentquelle:
<https://brouter.de/brouter/segments4/>.

### 2a. Variante Docker Compose (empfohlen)

```bash
docker compose up --build
# Backend:  http://localhost:8001
# BRouter:  http://localhost:17777
```

### 2b. Variante lokale Entwicklung

```bash
docker compose up -d brouter          # nur die Routing-Engine
cd backend
uv sync --extra dev
uv run uvicorn app.main:app --reload --port 8001
```

## Konfiguration

Alle Werte kommen aus `.env` (siehe `.env.example`). Wichtig:

| Variable | Bedeutung |
|---|---|
| `DEEPSEEK_API_KEY` | KI-Highlight-Auswahl und Streckenprüfung. Leer = regelbasierter Fallback ohne Vision-Review. |
| `DEEPSEEK_MODEL` / `DEEPSEEK_VISION_MODEL` | Nur Fallback-Modelle, falls `/models` nicht erreichbar ist. Leer lassen für automatische Auswahl. |
| `DEEPSEEK_EFFORT` | Optionaler Override der Reasoning-Stufe. Standard: höchste vom Modell unterstützte Stufe (`max`). |
| `VISION_ROUTE_DRAFT_ENABLED` | Bildmodell entwirft vor dem Routing Via-Punkte (Default: `true`). |
| `VISION_ROUTE_DRAFT_MAX_WAYPOINTS` | Maximale freie Entwurfspunkte (Default: `12`). |
| `ROUTE_REVIEW_ENABLED` | Automatische Streckenprüfung an/aus (Default: `true`). |
| `ROUTE_REVIEW_MAX_ROUNDS` | Maximale Anzahl Reviews (auf 3 begrenzt, Default: `3`). |
| `ROUTE_REVIEW_TIMEOUT_S` | Timeout pro Review-/Korrektur-Call (Default: `180`). |
| `ROUTE_REVIEW_MAX_FREE_WAYPOINTS` | Maximale freie Zwischenwegpunkte pro Korrektur (Default: `6`). |
| `ROUTE_REVIEW_SAVE_IMAGES` | Prüf-/Entwurfsbilder unter `ROUTE_REVIEW_DATA_DIR` speichern (Default: `false`). |
| `GPX_SHAPING_ENABLED` | GPX auf wichtige Wegpunkte ausdünnen (Default: `true`). |
| `GPX_POINT_SPACING_KM` | Zielabstand der GPX-Punkte (Default: `4`, Richtung 3–5 km). |
| `GPX_MIN_POINT_SPACING_KM` | Mindestabstand zwischen GPX-Punkten (Default: `2`). |
| `GPX_MAX_POINTS` | Obergrenze der GPX-Wegpunkte (Default: `30`). |
| `ORS_API_KEY` | OpenRouteService Routing + Geocoding. |
| `BROUTER_URL` | BRouter-Endpoint (lokal `http://localhost:17777`, Compose `http://brouter:17777`). |
| `OVERPASS_URL` / `OVERPASS_FALLBACK_URL` | POI-Suche; Mirror wird bei Überlast automatisch versucht. |
| `CORRIDOR_MIN_M` / `CORRIDOR_MAX_M` | Korridorbreite bei Heat 0 / 100. |
| `MAX_HIGHLIGHTS` | Obergrenze eingebauter Highlights. |

## Routenentwurf und GPX-Ausdünnung

1. **Entwurf** – Nach der POI-Auswahl rendert sidequest eine Übersichtskarte
   (Korridor/Rundtour, Start/Ziel, nummerierte POI-Kandidaten). Das Bildmodell
   liefert eine geordnete Liste aus Highlights und freien Via-Punkten auf
   schönen Abschnitten. Die Punkte werden validiert (Korridor, Abstand,
   Fahrtrichtung) und direkt ins Routing gegeben.
2. **Routing + Review** – Danach läuft die unveränderte Review-Schleife.
3. **GPX-Auswahl** – Auf der finalen Route werden Kandidaten im Abstand von
   ca. 3–5 km erzeugt (plus Highlights und markante Kurven). Das Bildmodell
   wählt die wichtigen aus und darf bis zu drei schöne Abschnitte ergänzen.
   Ohne Modell greift eine gleichmäßige deterministische Auswahl.
4. **Export** – Das GPX enthält nur diese Wegpunkte als Track; Highlights
   bleiben zusätzlich als `<wpt>` erhalten. Die volle Route bleibt in der App
   sichtbar und in der Datenbank gespeichert.

## Automatische Streckenprüfung

1. **Pass 1** – POI-Auswahl und initiales Routing wie bisher.
2. **Pass 2** – Ein Vision-Modell bekommt die Originalanfrage und ein
   serverseitig gerendertes Topo-Kartenbild (Route, Start/Ziel, Highlights,
   Distanz, Höhenmeter, Maßstab) und bewertet Score, Unsicherheit und konkrete
   Abweichungen.
3. **Pass 3** – Bei großen Abweichungen schlägt das Modell Korrekturen vor:
   Highlights hinzufügen/entfernen/umsortieren, Rundtour-Radius/Drehung ändern
   oder bis zu sechs freie Zwischenwegpunkte auf schönen Straßen setzen. Freie
   Punkte werden als Bildkoordinaten zurückgegeben und serverseitig in Lat/Lon
   umgerechnet. Danach wird neu geroutet.
4. **Pass 4** – Review der neuen Route. Passt sie, ist die Planung fertig.
5. **Pass 5/6** – Optional eine zweite Korrektur und ein letztes Review. Danach
   wird auf jeden Fall ausgegeben.

Währenddessen zeigt die Oberfläche einen Live-Status („Review 1: 58/100 ·
Unsicherheit 28 % · Korrektur nötig“). Am Ende bleiben Score, Unsicherheit,
Runden und verbleibende Hinweise im Ergebnis sichtbar. Schlägt Karte, Review
oder Korrektur technisch fehl, wird die Route trotzdem ausgegeben.

Die Modellwahl ist dynamisch: sidequest liest `/models`, filtert nach
Bild-Unterstützung (für Reviews) und wählt die höchste Version mit der höchsten
Reasoning-Stufe. Aktuell ist das DeepSeek V4.1 Flash mit `max`; künftige Modelle
werden ohne Codeänderung erkannt.

## Tests, Lint, Typen

```bash
cd backend
uv run pytest
uv run ruff check app tests
uv run mypy app
```

## GPX-Import in Komoot

1. Route planen und „Als GPX exportieren“ klicken.
2. In Komoot unter *Planung → Route importieren* die GPX-Datei öffnen.
3. Beim Import **„Route an bekannte Wege angleichen“** („Match route to known
   ways“) wählen. Komoot plant dann die Straßen zwischen den gelieferten
   Wegpunkten und behält die Highlights als Orientierung.
4. Die Route enthält alle Highlights zusätzlich als Wegpunkte (`<wpt>`).

## Fehlerverhalten / Fallbacks

- Routing-Provider nicht erreichbar → nächster Provider in der Prioritätskette;
  die UI zeigt an, welcher Provider genutzt wurde.
- Nicht anfahrbare Highlights (z. B. „target island“) → das Highlight wird
  ausgelassen und die Route neu berechnet.
- Overpass nicht erreichbar → Route wird ohne zusätzliche Highlights berechnet,
  mit Hinweis in der UI.
- deepseek-Fehler/kein Key → nächstgelegene POIs nach Umweg, mit Hinweis.
- Vision-Routenentwurf fehlgeschlagen → bisherige POI-Auswahl wird verwendet.
- Kartenbild, Vision-Review oder Korrektur nicht verfügbar → die beste bisherige
  Route wird ausgegeben; die UI zeigt einen Hinweis statt die Planung abzubrechen.
- GPX-Punktauswahl per Bildmodell fehlgeschlagen → gleichmäßige 3–5-km-Auswahl.
- Korrektur verschlechtert den Score → die vorherige Route wird behalten.

## Nicht-Ziele Phase 1

- Kein direkter Komoot-Write-Zugriff (keine offene API)
- Keine Strava-Heatmap
- Kein Login/Multi-User (Schema ist vorbereitet)
- GraphHopper nur optionaler Adapter

## Stack

Python 3.12, FastAPI, Pydantic v2, SQLModel + SQLite, httpx, gpxpy, Pillow, Jinja2,
HTMX, Alpine.js, Tailwind (CDN), Leaflet.js, Chart.js, Docker Compose, BRouter,
OpenRouteService, Overpass, DeepSeek (dynamische Modellwahl).
