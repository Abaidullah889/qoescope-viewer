import socket
import queue
import threading
import time

RTP_LISTEN_PORT = 5004
CONTENT_HOST = "analyzer"
CONTENT_PORT = 5004
QUEUE_SIZE = 50000
BUFFER_SIZE = 26214400  # 25MB

packet_queue = queue.Queue(maxsize=QUEUE_SIZE)

def receiver():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, BUFFER_SIZE)
    sock.bind(("0.0.0.0", RTP_LISTEN_PORT))
    print(f"[forwarder] Listening on 0.0.0.0:{RTP_LISTEN_PORT}")

    while True:
        data, _ = sock.recvfrom(65535)
        try:
            packet_queue.put_nowait(data)
        except queue.Full:
            try:
                packet_queue.get_nowait()
            except queue.Empty:
                continue
            try:
                packet_queue.put_nowait(data)
            except queue.Full:
                continue

def forwarder():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, BUFFER_SIZE)
    print(f"[forwarder] Forwarding to {CONTENT_HOST}:{CONTENT_PORT}")

    while True:
        data = packet_queue.get()
        try:
            sock.sendto(data, (CONTENT_HOST, CONTENT_PORT))
        except OSError as e:
            print(f"[forwarder] Send error: {e}")

if __name__ == "__main__":
    print("[forwarder] Starting...")

    threading.Thread(target=receiver, daemon=True, name="rtp-receiver").start()
    threading.Thread(target=forwarder, daemon=True, name="rtp-forwarder").start()

    while True:
        time.sleep(1)
