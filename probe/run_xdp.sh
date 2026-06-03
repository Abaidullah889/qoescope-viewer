#!/bin/bash
set -e

IFACE="eth0"
BPF_DIR="/sys/fs/bpf"
PIN_DIR="$BPF_DIR/stream"
DATA_DIR="/data"

echo "=== Mounting BPF filesystem (if not mounted) ==="
mountpoint -q $BPF_DIR || mount -t bpf bpf $BPF_DIR

echo "=== Creating pin directory ==="
mkdir -p $PIN_DIR

echo "=== Compiling XDP program ==="
cd $DATA_DIR
clang -O2 -g -target bpf \
  -I/usr/include/x86_64-linux-gnu \
  -c xdp_rtp_count.c -o xdp_rtp_count.o

echo "=== Compiling loader (xdp_pin) ==="
gcc -O2 -g -Wall xdp_pin.c -o xdp_pin -lbpf -lelf -lz

echo "=== Removing existing XDP programs (if any) ==="
ip link set dev $IFACE xdp off 2>/dev/null || true
ip link set dev $IFACE xdpgeneric off 2>/dev/null || true

echo "=== Loading and pinning XDP program ==="
./xdp_pin

echo "=== Compiling reader program ==="
gcc -O2 -g -Wall read_pinned.c -o read_pinned -lbpf -lelf -lz

# Cleanup handler
cleanup() {
    echo "=== Caught signal, cleaning up ==="
    kill $READER_PID 2>/dev/null || true
    wait $READER_PID 2>/dev/null || true   # ← reaps the child, prevents defunct
    ip link set dev $IFACE xdp off 2>/dev/null || true
    echo "=== Done ==="
    exit 0
}

# Trap SIGINT, SIGTERM, EXIT to ensure cleanup runs on Ctrl+C, kill, or normal exit
trap cleanup SIGINT SIGTERM EXIT

echo "=== Running stats reader ==="
./read_pinned $PIN_DIR/network_stats $PIN_DIR/stream_stats &
READER_PID=$!

echo "=== XDP setup complete ==="
echo "Reader running with PID $READER_PID"

# Wait for child so script stays alive and can catch signals
wait $READER_PID