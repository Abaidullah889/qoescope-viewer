#!/bin/bash
# aggregator/entrypoint.sh
# Starts the metrics aggregator API server automatically.
# Mirrors your original command:
#   uvicorn backend:app --host 0.0.0.0 --port 9000

set -e

echo "=== [aggregator] Starting QoEScope Metrics Aggregator ==="
echo "=== [aggregator] Probe URL    : ${PROBE_URL} ==="
echo "=== [aggregator] Analyzer URL : ${BRISQUE_URL} ==="
echo "=== [aggregator] InfluxDB URL : ${INFLUX_URL} ==="

exec uvicorn backend:app --host 0.0.0.0 --port 9000