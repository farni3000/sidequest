#!/usr/bin/env bash
#
# Downloads BRouter segment tiles (.rd5) into ./segments.
#
# Usage:
#   ./setup_segments.sh                # default region (Ardennes / Belgium)
#   ./setup_segments.sh ardennes       # named region
#   ./setup_segments.sh E0_N45 E5_N45  # explicit tile names
#
# About tiles: BRouter splits the planet into 5x5 degree tiles. The name is the
# south-west corner, e.g. E5_N50 covers lon 5..10 E and lat 50..55 N. Use
# https://brouter.de/brouter/segments4/ as the source of truth and check the
# tile list for the exact spelling of new tiles.
#
# To add a new region: extend region_tiles() below and run the script.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SEGMENT_DIR="${SEGMENT_DIR:-$SCRIPT_DIR/segments}"
BASE_URL="${BROUTER_SEGMENT_URL:-https://brouter.de/brouter/segments4}"

region_tiles() {
  case "$1" in
    ardennes)     echo "E0_N45 E0_N50 E5_N45 E5_N50" ;;
    eifel)        echo "E5_N50 E0_N50" ;;
    benelux)      echo "E0_N45 E0_N50 E5_N45 E5_N50 E0_N55 E5_N55" ;;
    germany-west) echo "E5_N45 E5_N50 E0_N50 E10_N45 E10_N50" ;;
    *)            return 1 ;;
  esac
}

known_regions() {
  echo "ardennes eifel benelux germany-west"
}

usage() {
  echo "Usage: $0 [region|TILE ...]" >&2
  echo "Named regions: $(known_regions)" >&2
  exit 1
}

if [ "$#" -eq 0 ]; then
  set -- ardennes
fi

TILES=""
for arg in "$@"; do
  if resolved="$(region_tiles "$arg" 2>/dev/null)"; then
    TILES="$TILES $resolved"
  else
    TILES="$TILES $arg"
  fi
done

mkdir -p "$SEGMENT_DIR"
count=0
for tile in $TILES; do
  count=$((count + 1))
done
echo "Downloading $count tile(s) into $SEGMENT_DIR"

for tile in $TILES; do
  target="$SEGMENT_DIR/${tile}.rd5"
  if [ -s "$target" ]; then
    echo "  = ${tile}.rd5 already present, skipping"
    continue
  fi
  echo "  + ${tile}.rd5"
  curl -fSL --retry 3 -o "${target}.tmp" "$BASE_URL/${tile}.rd5"
  mv "${target}.tmp" "$target"
done

echo "Done. Restart the brouter container so it picks up the segments:"
echo "  docker compose restart brouter"
