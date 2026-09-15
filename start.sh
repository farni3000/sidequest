#!/usr/bin/env bash
#
# One-command start: bring up the Docker Compose stack and open the app.
#
# Usage: ./start.sh [--no-browser] [--region <name>]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

APP_URL="${APP_URL:-http://localhost:8000}"
REGION="ardennes"
OPEN_BROWSER=1

while [ "$#" -gt 0 ]; do
  case "$1" in
    --no-browser) OPEN_BROWSER=0 ;;
    --region) shift; REGION="${1:-ardennes}" ;;
    -h|--help)
      echo "Usage: $0 [--no-browser] [--region <name>]"
      exit 0
      ;;
    *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
  esac
  shift
done

info() { printf '\033[0;32m>\033[0m %s\n' "$1"; }
warn() { printf '\033[0;33m!\033[0m %s\n' "$1"; }
fail() { printf '\033[0;31mx\033[0m %s\n' "$1" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "Docker nicht gefunden. Installation: brew install colima docker docker-compose"

# macOS: make sure the Colima VM (and its Docker daemon) is running.
if command -v colima >/dev/null 2>&1; then
  if ! colima status >/dev/null 2>&1; then
    info "Starte Colima VM (kann beim ersten Mal etwas dauern) ..."
    colima start --cpu 4 --memory 6 --disk 60
  fi
fi

docker info >/dev/null 2>&1 || fail "Docker-Daemon nicht erreichbar. Läuft Colima/Docker Desktop?"

# Ensure BRouter has at least one segment tile.
if ! ls brouter/segments/*.rd5 >/dev/null 2>&1; then
  info "Keine BRouter-Segmente gefunden – lade Region '$REGION' herunter ..."
  ./brouter/setup_segments.sh "$REGION"
fi

info "Baue und starte Container ..."
docker compose up -d --build

info "Warte auf Backend unter $APP_URL ..."
ready=0
for _ in $(seq 1 40); do
  if curl -fsS -m 3 "$APP_URL/health" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done

if [ "$ready" -ne 1 ]; then
  warn "Backend antwortet noch nicht. Logs:"
  docker compose logs backend | tail -20
  exit 1
fi

info "Läuft. Backend: $APP_URL  |  BRouter: http://localhost:17777"
info "Stop mit: docker compose down"

if [ "$OPEN_BROWSER" -eq 1 ]; then
  if command -v open >/dev/null 2>&1; then
    open "$APP_URL"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$APP_URL" >/dev/null 2>&1 &
  else
    warn "Konnte keinen Browser automatisch öffnen – bitte $APP_URL manuell öffnen."
  fi
fi
