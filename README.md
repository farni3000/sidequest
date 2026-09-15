# sidequest – KI-gestützter Radrouten-Generator

Erzeugt auf Anfrage individuelle, fahrbare Radrouten anhand von Fahrradart,
Distanz, einem „Heat“-Regler und Freitext-Wünschen. Die App sucht selbstständig
interessante Abschnitte (Aussichtspunkte, Gipfel, MTB-Trails, historische Orte)
in der Nähe der Route, lässt eine KI die lohnendsten auswählen und berechnet
daraus eine reale Strecke. Ergebnis ist eine **GPX-Datei** zum Import in Komoot
oder jede andere Karten-App.

> Es gibt keine offene Komoot-API zum Anlegen von Touren – daher ist der
> GPX-Export der Zielpfad. Strava-Heatmap-Daten sind bewusst **nicht** Teil von
> Phase 1.

## Funktionsumfang

- Formular: Fahrradart (Rennrad / Gravel / MTB mit Härtegrad XC / All Mountain /
  Enduro / Trekking-Wandern), Start/Ziel oder Rundtour, Distanz, Heat 0–100,
  Freitext.
- Routing über austauschbare Provider mit automatischem Fallback:
  - **BRouter** (self-hosted, Docker) – primär für MTB/Trekking
  - **OpenRouteService** (Cloud, Free Tier) – primär für Rennrad/Gravel
  - **GraphHopper** – optionaler dritter Adapter
- POI-Suche über die **Overpass API** im Korridor; Korridorbreite skaliert mit
  dem Heat-Wert (500 m – 8 km).
- **deepseek** (OpenAI-kompatibel) wählt per Tool-Call die Highlights aus und
  ordnet sie; ohne Key/bei Fehler greift eine distanzbasierte Auswahl.
- Ergebnisansicht mit Leaflet-Karte, Höhenprofil (Chart.js), Highlight-Liste mit
  Begründung und GPX-Download.
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
│   │   ├── main.py                 # FastAPI-Routen
│   │   ├── config.py               # pydantic-settings
│   │   ├── models.py               # SQLModel (RoutePlan, Highlight)
│   │   ├── schemas.py
│   │   ├── geocoding.py            # ORS-Geocoding + Nominatim-Fallback
│   │   ├── corridor.py             # Heat -> Korridorbreite/Radius
│   │   ├── gpx_export.py           # GPX 1.1
│   │   ├── routing/                # base, brouter, openrouteservice, graphhopper, selector
│   │   ├── poi/                    # overpass, ranking (deepseek)
│   │   ├── services/planner.py     # Orchestrierung + Fallbacks
│   │   ├── templates/              # Jinja2 + HTMX/Alpine
│   │   └── static/                 # app.js (Leaflet + Chart.js)
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
- API-Keys: OpenRouteService (kostenlos) und deepseek (optional, für KI-Ranking)

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
# Backend:  http://localhost:8000
# BRouter:  http://localhost:17777
```

### 2b. Variante lokale Entwicklung

```bash
docker compose up -d brouter          # nur die Routing-Engine
cd backend
uv sync --extra dev
uv run uvicorn app.main:app --reload
```

## Konfiguration

Alle Werte kommen aus `.env` (siehe `.env.example`). Wichtig:

| Variable | Bedeutung |
|---|---|
| `DEEPSEEK_API_KEY` | KI-Highlight-Auswahl. Leer = regelbasierter Fallback. |
| `ORS_API_KEY` | OpenRouteService Routing + Geocoding. |
| `BROUTER_URL` | BRouter-Endpoint (lokal `http://localhost:17777`, Compose `http://brouter:17777`). |
| `OVERPASS_URL` / `OVERPASS_FALLBACK_URL` | POI-Suche; Mirror wird bei Überlast automatisch versucht. |
| `CORRIDOR_MIN_M` / `CORRIDOR_MAX_M` | Korridorbreite bei Heat 0 / 100. |
| `MAX_HIGHLIGHTS` | Obergrenze eingebauter Highlights. |

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
3. Die Route enthält alle Highlights zusätzlich als Wegpunkte (`<wpt>`).

## Fehlerverhalten / Fallbacks

- Routing-Provider nicht erreichbar → nächster Provider in der Prioritätskette;
  die UI zeigt an, welcher Provider genutzt wurde.
- Nicht anfahrbare Highlights (z. B. „target island“) → das Highlight wird
  ausgelassen und die Route neu berechnet.
- Overpass nicht erreichbar → Route wird ohne zusätzliche Highlights berechnet,
  mit Hinweis in der UI.
- deepseek-Fehler/kein Key → nächstgelegene POIs nach Umweg, mit Hinweis.

## Nicht-Ziele Phase 1

- Kein direkter Komoot-Write-Zugriff (keine offene API)
- Keine Strava-Heatmap
- Kein Login/Multi-User (Schema ist vorbereitet)
- GraphHopper nur optionaler Adapter

## Stack

Python 3.12, FastAPI, Pydantic v2, SQLModel + SQLite, httpx, gpxpy, Jinja2,
HTMX, Alpine.js, Tailwind (CDN), Leaflet.js, Chart.js, Docker Compose, BRouter,
OpenRouteService, Overpass, deepseek.
