#!/bin/bash
# probe/entrypoint.sh

# Starts everything in the right order.

set -e

echo "=== [probe] Step 1: Running XDP setup ==="
bash /data/run_xdp.sh &
XDP_PID=$!
sleep 2

echo "=== [probe] Step 2: Starting forwarder ==="
python3 /data/forwarder.py &
FORWARDER_PID=$!

echo "=== [probe] Step 3: Starting metrics API ==="
uvicorn app:app --host 0.0.0.0 --port 8000 &
UVICORN_PID=$!

echo "=== [probe] All services running ==="

cleanup() {
    echo "=== [probe] Shutting down ==="
    kill $XDP_PID $FORWARDER_PID $UVICORN_PID 2>/dev/null || true
    wait $XDP_PID $FORWARDER_PID $UVICORN_PID 2>/dev/null || true
    ip link set dev $IFACE xdp off 2>/dev/null || true
    exit 0
}

trap cleanup SIGINT SIGTERM

while true; do
    if ! kill -0 $XDP_PID 2>/dev/null; then echo "=== [probe] ERROR: XDP died ==="; cleanup; fi
    if ! kill -0 $FORWARDER_PID 2>/dev/null; then echo "=== [probe] ERROR: Forwarder died ==="; cleanup; fi
    if ! kill -0 $UVICORN_PID 2>/dev/null; then echo "=== [probe] ERROR: API died ==="; cleanup; fi
    sleep 5
done