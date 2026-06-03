#!/bin/bash
# analyzer/entrypoint.sh
# Starts everything automatically:
#   1. decoder.py    assembles frames, decodes, runs BRISQUE, writes metrics JSON
#   2. uvicorn       serves metrics API on port 9101

set -e

echo "=== [analyzer] Starting QoEScope Quality Analyzer ==="

# Step 1: Start decoder in background
echo "=== [analyzer] Step 1: Starting decoder ==="
python3 /app/decoder.py &
DECODER_PID=$!
echo "=== [analyzer] Decoder running with PID $DECODER_PID ==="

# Give decoder a moment to initialize before API starts
sleep 2

# Step 2: Start BRISQUE metrics API
echo "=== [analyzer] Step 2: Starting metrics API on port 9101 ==="
uvicorn brisque_api:app --host 0.0.0.0 --port 9101 &
UVICORN_PID=$!
echo "=== [analyzer] Metrics API running with PID $UVICORN_PID ==="

echo "=== [analyzer] All services running ==="

# Cleanup handler
cleanup() {
    echo "=== [analyzer] Shutting down ==="
    kill $DECODER_PID $UVICORN_PID 2>/dev/null || true
    wait $DECODER_PID $UVICORN_PID 2>/dev/null || true
    exit 0
}

trap cleanup SIGINT SIGTERM

# Monitor both processes
while true; do
    if ! kill -0 $DECODER_PID 2>/dev/null; then echo "=== [analyzer] ERROR: Decoder died ==="; cleanup; fi
    if ! kill -0 $UVICORN_PID 2>/dev/null; then echo "=== [analyzer] ERROR: API died ==="; cleanup; fi
    sleep 5
done