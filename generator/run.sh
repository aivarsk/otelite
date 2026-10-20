#!/bin/bash
# Start otelite and fill it with realistic telemetry from the generator.
# Usage: ./generator/run.sh [--seconds 60] [--rps 20] [--seed 7]
# Ctrl+C stops otelite. Env overrides: OTELITE_PORT, OTELITE_DATA_DIR.
set -e
cd "$(dirname "$0")/.."

PORT="${OTELITE_PORT:-4318}"
DATA="${OTELITE_DATA_DIR:-/tmp/otelite-generator}"

if curl -sf "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
  echo "port ${PORT} is already serving an otelite; stop it first (or set OTELITE_PORT)"
  exit 1
fi

if [ ! -x generator/.venv/bin/python ]; then
  echo "creating generator venv and installing opentelemetry-sdk..."
  python3 -m venv generator/.venv
  generator/.venv/bin/pip install -q opentelemetry-sdk
fi

PYTHONPATH=src python3 -m otelite --port "${PORT}" --data-dir "${DATA}" &
OTELITE_PID=$!
trap 'kill "${OTELITE_PID}" 2>/dev/null' EXIT

for _ in $(seq 1 50); do
  curl -sf "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1 && break
  sleep 0.1
done

generator/.venv/bin/python generator/gen.py --url "http://127.0.0.1:${PORT}" "$@"

echo ""
echo "otelite is still running with the generated data:"
echo "  page:  http://localhost:${PORT}/"
echo "  data:  ${DATA}"
echo "  stop:  kill ${OTELITE_PID}"
wait "${OTELITE_PID}"
