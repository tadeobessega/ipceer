#!/usr/bin/env bash
# Tarea diaria de IPCEER para macOS / Linux.
# Archiva los zips de SEPA publicados que falten, procesa, calcula y arma el sitio.
#
# Registrar con cron (crontab -e), días hábiles a las 11:00:
#   0 11 * * 1-5 /ruta/a/ipceer/scripts/tarea_diaria.sh
# Para publicar resultados en el repo: agregar --publicar al final.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
export IPCEER_ARCHIVO="${IPCEER_ARCHIVO:-$HOME/SEPA}"
mkdir -p "$REPO/logs" "$IPCEER_ARCHIVO"
LOG="$REPO/logs/diario_$(date +%F).log"
cd "$REPO"

PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || PY=python3
"$PY" -m ipceer.cli diario >>"$LOG" 2>&1

if [ "${1:-}" = "--publicar" ]; then
  git add resultados sitio >>"$LOG" 2>&1
  if ! git diff --cached --quiet; then
    git commit -m "Datos $(date +%F)" >>"$LOG" 2>&1
    git push >>"$LOG" 2>&1
  fi
fi
