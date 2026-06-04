"""
decoder.py

Runs inside the analyzer container. Receives RTP packets, assembles
H.264 frames, decodes them, and runs BRISQUE quality scoring.

Thread architecture:
  Thread 1: recv_worker     recvfrom() -> recv_queue
  Thread 2: main loop       parse RTP + assemble -> frame_queue
  Thread 3: decode_worker   single codec, sequential decode -> brisque_queue
  Thread 4+: brisque pool   stateless BRISQUE on pixel arrays -> log
  Thread N: log_worker      orders log lines by frame sequence number

Frame tracking mirrors xdp_rtp_count.c exactly.
"""

import json
import os
import socket
import struct
import time
import threading
import queue
import heapq
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import av
import cv2

RTP_PORT = 5004
BUFFER_SIZE = 26214400
LOG_FILE = "decoder.log"
BRISQUE_EVERY = 1
BRISQUE_WORKERS = 4

# ── Live received-video display (MJPEG) ───────────────────────
# Frames are sent at native resolution (no resampling). They are JPEG-encoded at
# high quality with full chroma (4:4:4, no subsampling), which is visually
# indistinguishable from the decoded frame but small/fast enough for smooth
# real-time playback — lossless PNG is too heavy for HD in real time.
DISPLAY_HTTP_PORT = 9102      # MJPEG server port (exposed in docker-compose)
DISPLAY_JPEG_QUALITY = 95     # cv2 JPEG quality (0-100); high = near-lossless

BRISQUE_MODEL = "/app/brisque_model_live.yml"
BRISQUE_RANGE = "/app/brisque_range_live.yml"

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, BUFFER_SIZE)
sock.bind(("0.0.0.0", RTP_PORT))

codec = av.CodecContext.create("h264", "r")

recv_queue = queue.Queue(maxsize=10000)
frame_queue = queue.Queue(maxsize=500)
brisque_queue = queue.Queue(maxsize=500)
result_queue = queue.Queue(maxsize=500)
display_queue = queue.Queue(maxsize=2)   # decoded ndarrays awaiting JPEG encode (drop-oldest)


class LatestFrame:
    """Holds the most recent JPEG frame and wakes MJPEG clients when it changes.

    Slow clients always jump to the newest frame (they wait on a version
    counter), so no per-client lag accumulates. Tracks connected clients so the
    encoder can skip work when nobody is watching.
    """

    def __init__(self):
        self._cond = threading.Condition()
        self._data = None
        self._version = 0
        self._clients = 0

    def set(self, data):
        with self._cond:
            self._data = data
            self._version += 1
            self._cond.notify_all()

    def wait_newer(self, last_version, timeout):
        """Block until a frame newer than last_version exists, or timeout.

        Returns (version, data); data is None if it timed out with nothing new.
        """
        with self._cond:
            if self._version == last_version:
                self._cond.wait(timeout)
            if self._version == last_version:
                return last_version, None
            return self._version, self._data

    def add_client(self):
        with self._cond:
            self._clients += 1

    def remove_client(self):
        with self._cond:
            self._clients = max(0, self._clients - 1)

    def client_count(self):
        with self._cond:
            return self._clients


latest_frame = LatestFrame()

_frame_seq = 0
_frame_seq_lock = threading.Lock()
try:
    _test = cv2.quality.QualityBRISQUE_create(BRISQUE_MODEL, BRISQUE_RANGE)
    brisque_available = True
except Exception as e:
    brisque_available = False
    print(f"[WARN] BRISQUE not available: {e}")

_lock = threading.Lock()
complete_i_frames = 0
complete_p_frames = 0
incomplete_frames = 0
incomplete_i_frames = 0
incomplete_p_frames = 0
complete_u_frames = 0
inc_i_decode_ok = 0
inc_i_decode_fail = 0
decode_errors = 0
last_brisque = 0.0

cur_ts = None
cur_type = 0
seen_ts = False
incomplete = False
saw_marker = False
last_seq = None
current_frags = []
cur_ssrc = None

log_file = open(LOG_FILE, "w", buffering=1)
log_lock = threading.Lock()

def log(msg):
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with log_lock:
        log_file.write(line + "\n")


def frame_label(frame_type: str, is_incomplete: bool) -> str:
    prefix = "Incomplete " if is_incomplete else ""
    if frame_type == "I":
        return f"{prefix}I-Frame"
    if frame_type == "P":
        return f"{prefix}P-Frame"
    return f"{prefix}Unknown-Frame"


def frame_log_line(
    label: str,
    complete_i: int,
    total_i: int,
    complete_p: int,
    total_p: int,
    complete_unknown: int,
    brisque_repr: str,
    incomplete_total: int,
    incomplete_i: int,
    incomplete_p: int,
    incomplete_i_decode_ok: int,
    incomplete_i_decode_fail: int,
    error_count: int,
) -> str:
    return (
        f"{label} | "
        f"i={complete_i}({total_i}) p={complete_p}({total_p}) unknown={complete_unknown} | "
        f"brisque={brisque_repr} | "
        f"incomplete={incomplete_total}(i={incomplete_i} p={incomplete_p}) | "
        f"inc_i_dec=ok:{incomplete_i_decode_ok} fail:{incomplete_i_decode_fail} | "
        f"errors={error_count}"
    )

log(f"Listening on 0.0.0.0:{RTP_PORT}")
log(f"Logging to {LOG_FILE}")
log(f"BRISQUE available: {brisque_available}")
log(f"BRISQUE every {BRISQUE_EVERY} frame(s), {BRISQUE_WORKERS} BRISQUE workers")


def parse_rtp(data):
    if len(data) < 12:
        return None
    vpxcc = data[0]
    if ((vpxcc >> 6) & 0x03) != 2:
        return None
    x = (vpxcc >> 4) & 0x01
    cc = vpxcc & 0x0F
    marker = (data[1] >> 7) & 0x01
    seq = struct.unpack("!H", data[2:4])[0]
    ts = struct.unpack("!I", data[4:8])[0]
    ssrc = struct.unpack("!I", data[8:12])[0]

    offset = 12 + cc * 4
    if offset > len(data):
        return None
    if x:
        if offset + 4 > len(data):
            return None
        ext_len_words = struct.unpack("!H", data[offset + 2: offset + 4])[0]
        offset += 4 + ext_len_words * 4
        if offset > len(data):
            return None
    payload = data[offset:]
    if len(payload) < 1:
        return None
    return marker, seq, ts, ssrc, payload


def ue_decode(data, bit_pos):
    zeros = 0
    while True:
        byte_idx = (bit_pos + zeros) // 8
        bit_idx = 7 - ((bit_pos + zeros) % 8)
        if byte_idx >= len(data):
            return None, bit_pos
        bit = (data[byte_idx] >> bit_idx) & 1
        if bit == 1:
            break
        zeros += 1
        if zeros > 15:
            return None, bit_pos
    bit_pos += zeros + 1
    info = 0
    for _ in range(zeros):
        byte_idx = bit_pos // 8
        bit_idx = 7 - (bit_pos % 8)
        if byte_idx >= len(data):
            return None, bit_pos
        bit = (data[byte_idx] >> bit_idx) & 1
        info = (info << 1) | bit
        bit_pos += 1
    base = (1 << zeros) - 1 if zeros > 0 else 0
    return base + info, bit_pos


def get_slice_type(rbsp):
    if len(rbsp) < 1:
        return None
    bit_pos = 0
    first_mb_in_slice, bit_pos = ue_decode(rbsp, bit_pos)
    if first_mb_in_slice is None:
        return None
    slice_type, _next_bit = ue_decode(rbsp, bit_pos)
    return slice_type


def get_frame_type_from_payload(payload):
    if len(payload) < 1:
        return None
    nal_type = payload[0] & 0x1F
    if nal_type == 5:
        return "I"
    if nal_type == 1:
        st = get_slice_type(payload[1:])
        if st in (2, 7): return "I"
        if st in (0, 5): return "P"
        return None
    if nal_type == 28:
        if len(payload) < 2: return None
        fu_hdr = payload[1]
        is_start = (fu_hdr >> 7) & 0x01
        orig = fu_hdr & 0x1F
        if orig == 5: return "I"
        if orig == 1 and is_start:
            st = get_slice_type(payload[2:])
            if st in (2, 7): return "I"
            if st in (0, 5): return "P"
        return None
    if nal_type == 24:
        remaining = payload[1:]
        for _ in range(8):
            if len(remaining) < 2:
                break
            size = struct.unpack("!H", remaining[:2])[0]
            remaining = remaining[2:]
            if size == 0 or size > 1400 or size > len(remaining):
                break
            inner = remaining[0] & 0x1F
            if inner == 5: return "I"
            if inner == 1:
                st = get_slice_type(remaining[1:size])
                if st in (2, 7): return "I"
                if st in (0, 5): return "P"
            remaining = remaining[size:]
    return None


def extract_nal_units(frags):
    nal_units = []
    fua_buffer = []
    fua_type = None
    fua_started = False

    for _, nal_type, is_start, fragment in frags:
        if nal_type in (1, 5):
            if is_start:
                # flush previous FU-A only if it had a proper start
                if fua_buffer and fua_started:
                    nal_units.append((fua_type, b"".join(fua_buffer)))
                fua_buffer = [fragment]
                fua_type = nal_type
                fua_started = True
            else:
                # only accumulate if we saw the start packet,
                # otherwise we have no NAL header so discard
                if fua_started:
                    fua_buffer.append(fragment)
        elif nal_type in (7, 8):
            if fua_buffer and fua_started:
                nal_units.append((fua_type, b"".join(fua_buffer)))
            fua_buffer = []
            fua_started = False
            nal_units.append((nal_type, fragment))

    if fua_buffer and fua_started:
        nal_units.append((fua_type, b"".join(fua_buffer)))

    return nal_units


def build_annexb(nal_units):
    out = b""
    for _, nal_bytes in nal_units:
        out += b"\x00\x00\x00\x01" + nal_bytes
    return out


def decode_worker():

    global decode_errors, complete_i_frames, complete_p_frames
    global incomplete_frames, incomplete_i_frames, incomplete_p_frames, complete_u_frames
    global inc_i_decode_ok, inc_i_decode_fail

    while True:
        try:
            frags, frame_type, is_incomplete, _p_snapshot = frame_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        frags.sort(key=lambda x: x[0])
        nal_units = extract_nal_units(frags)

        with _lock:
            if frame_type == "U":
                if is_incomplete:
                    incomplete_frames += 1
                else:
                    complete_u_frames += 1
            elif is_incomplete:
                incomplete_frames += 1
                if frame_type == "I":
                    incomplete_i_frames += 1
                elif frame_type == "P":
                    incomplete_p_frames += 1
            else:
                if frame_type == "I":
                    complete_i_frames += 1
                elif frame_type == "P":
                    complete_p_frames += 1

            complete_i = complete_i_frames
            complete_p = complete_p_frames
            total_i = complete_i + incomplete_i_frames
            total_p = complete_p + incomplete_p_frames
            incomplete_total = incomplete_frames
            incomplete_i = incomplete_i_frames
            incomplete_p = incomplete_p_frames
            complete_unknown = complete_u_frames
            incomplete_i_decode_ok = inc_i_decode_ok
            incomplete_i_decode_fail = inc_i_decode_fail
            error_count = decode_errors

        global _frame_seq
        with _frame_seq_lock:
            _frame_seq += 1
            fseq = _frame_seq

        label = frame_label(frame_type, is_incomplete)

        if not nal_units:
            try:
                result_queue.put((
                    fseq,
                    frame_log_line(
                        label=label,
                        complete_i=complete_i,
                        total_i=total_i,
                        complete_p=complete_p,
                        total_p=total_p,
                        complete_unknown=complete_unknown,
                        brisque_repr=f"{last_brisque:.2f}(last)",
                        incomplete_total=incomplete_total,
                        incomplete_i=incomplete_i,
                        incomplete_p=incomplete_p,
                        incomplete_i_decode_ok=incomplete_i_decode_ok,
                        incomplete_i_decode_fail=incomplete_i_decode_fail,
                        error_count=error_count,
                    ),
                ), timeout=5.0)
            except queue.Full:
                pass
            frame_queue.task_done()
            continue

        should_measure = (
            frame_type == "I" or
            (frame_type == "P" and total_p % BRISQUE_EVERY == 0)
        )

        decoded_img = None
        display_img = None
        try:
            annexb = build_annexb(nal_units)
            packet = av.Packet(annexb)
            frames = codec.decode(packet)
            for frame in frames:
                # Convert the first decoded frame once; reuse it for both the
                # live display and (when measuring) BRISQUE so the picture shown
                # is the exact frame that gets scored.
                display_img = frame.to_ndarray(format="bgr24")
                if should_measure:
                    decoded_img = display_img
                break
        except Exception as e:
            with _lock:
                decode_errors += 1
                error_count = decode_errors
                if frame_type == "I" and is_incomplete:
                    inc_i_decode_fail += 1
                    incomplete_i_decode_fail = inc_i_decode_fail
                    incomplete_i_decode_ok = inc_i_decode_ok
            log(f"Decode error ({frame_type}): {e}")
            with _brisque_window_lock:
                _brisque_window.append(-1)
            try:
                result_queue.put(
                    (
                        fseq,
                        frame_log_line(
                            label=label,
                            complete_i=complete_i,
                            total_i=total_i,
                            complete_p=complete_p,
                            total_p=total_p,
                            complete_unknown=complete_unknown,
                            brisque_repr="-1",
                            incomplete_total=incomplete_total,
                            incomplete_i=incomplete_i,
                            incomplete_p=incomplete_p,
                            incomplete_i_decode_ok=incomplete_i_decode_ok,
                            incomplete_i_decode_fail=incomplete_i_decode_fail,
                            error_count=error_count,
                        ),
                    ),
                    timeout=5.0
                )
            except queue.Full:
                pass
            try:
                codec.flush_buffers()
            except Exception:
                pass
            frame_queue.task_done()
            continue

        with _lock:
            error_count = decode_errors
            if frame_type == "I" and is_incomplete:
                inc_i_decode_ok += 1
            incomplete_i_decode_ok = inc_i_decode_ok
            incomplete_i_decode_fail = inc_i_decode_fail

        # Hand the decoded frame to the display encoder (drop-oldest). On decode
        # failure we never get here, so the viewer holds the last good frame —
        # a visible freeze that mirrors the QoE drop.
        if display_img is not None:
            try:
                display_queue.put_nowait(display_img)
            except queue.Full:
                try:
                    display_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    display_queue.put_nowait(display_img)
                except queue.Full:
                    pass

        if decoded_img is not None:
            try:
                brisque_queue.put_nowait(
                    (
                        fseq,
                        decoded_img,
                        frame_type,
                        is_incomplete,
                        complete_i,
                        total_i,
                        complete_p,
                        total_p,
                        incomplete_total,
                        incomplete_i,
                        incomplete_p,
                        complete_unknown,
                        incomplete_i_decode_ok,
                        incomplete_i_decode_fail,
                        error_count,
                    )
                )
            except queue.Full:
                try:
                    result_queue.put((
                        fseq,
                        frame_log_line(
                            label=label,
                            complete_i=complete_i,
                            total_i=total_i,
                            complete_p=complete_p,
                            total_p=total_p,
                            complete_unknown=complete_unknown,
                            brisque_repr=f"{last_brisque:.2f}(last)",
                            incomplete_total=incomplete_total,
                            incomplete_i=incomplete_i,
                            incomplete_p=incomplete_p,
                            incomplete_i_decode_ok=incomplete_i_decode_ok,
                            incomplete_i_decode_fail=incomplete_i_decode_fail,
                            error_count=error_count,
                        ),
                    ), timeout=5.0)
                except queue.Full:
                    log(f"[WARN] result_queue full, dropping log entry for fseq={fseq}")
        else:
            try:
                result_queue.put((
                    fseq,
                    frame_log_line(
                        label=label,
                        complete_i=complete_i,
                        total_i=total_i,
                        complete_p=complete_p,
                        total_p=total_p,
                        complete_unknown=complete_unknown,
                        brisque_repr=f"{last_brisque:.2f}(last)",
                        incomplete_total=incomplete_total,
                        incomplete_i=incomplete_i,
                        incomplete_p=incomplete_p,
                        incomplete_i_decode_ok=incomplete_i_decode_ok,
                        incomplete_i_decode_fail=incomplete_i_decode_fail,
                        error_count=error_count,
                    ),
                ), timeout=5.0)
            except queue.Full:
                log(f"[WARN] result_queue full, dropping log entry for fseq={fseq}")

        frame_queue.task_done()


def brisque_worker():
    global last_brisque

    if brisque_available:
        local_brisque = cv2.quality.QualityBRISQUE_create(BRISQUE_MODEL, BRISQUE_RANGE)
    else:
        local_brisque = None

    while True:
        try:
            (
                fseq,
                img,
                frame_type,
                is_incomplete,
                complete_i,
                total_i,
                complete_p,
                total_p,
                incomplete_total,
                incomplete_i,
                incomplete_p,
                complete_unknown,
                incomplete_i_decode_ok,
                incomplete_i_decode_fail,
                error_count,
            ) = brisque_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            score = last_brisque
            if local_brisque is not None:
                try:
                    score = round(float(local_brisque.compute(img)[0]), 2)
                    with _lock:
                        last_brisque = score
                except Exception:
                    pass

            with _brisque_window_lock:
                _brisque_window.append(score)

            label = frame_label(frame_type, is_incomplete)
            result_queue.put((
                fseq,
                frame_log_line(
                    label=label,
                    complete_i=complete_i,
                    total_i=total_i,
                    complete_p=complete_p,
                    total_p=total_p,
                    complete_unknown=complete_unknown,
                    brisque_repr=f"{score:.2f}",
                    incomplete_total=incomplete_total,
                    incomplete_i=incomplete_i,
                    incomplete_p=incomplete_p,
                    incomplete_i_decode_ok=incomplete_i_decode_ok,
                    incomplete_i_decode_fail=incomplete_i_decode_fail,
                    error_count=error_count,
                ),
            ), )
        except Exception as ex:
            try:
                result_queue.put((fseq, f"BRISQUE worker error: {ex}"))
            except Exception:
                pass

        brisque_queue.task_done()


def log_worker():
    heap = []
    next_seq = 1

    while True:
        try:
            item = result_queue.get(timeout=0.1)
            heapq.heappush(heap, item)
        except queue.Empty:
            pass

        while heap and heap[0][0] == next_seq:
            _, line = heapq.heappop(heap)
            log(line)
            next_seq += 1


def recv_worker():
    while True:
        try:
            result = sock.recvfrom(65535)
            if not result or len(result) < 2:
                continue
            data, _ = result
        except (ValueError, OSError):
            continue
        try:
            recv_queue.put_nowait(data)
        except queue.Full:
            try:
                recv_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                recv_queue.put_nowait(data)
            except queue.Full:
                pass


METRICS_JSON = "/app/metrics/brisque_metrics.json"

os.makedirs(os.path.dirname(METRICS_JSON), exist_ok=True)

_brisque_window = []
_brisque_window_lock = threading.Lock()


def _metrics_aggregator():
    prev_total = 0
    prev_incomplete = 0

    while True:
        time.sleep(1.0)

        with _brisque_window_lock:
            window = _brisque_window[:]
            _brisque_window.clear()

        valid = [s for s in window if s >= 0]
        errors = len([s for s in window if s < 0])

        with _lock:
            ic = incomplete_frames
            ci = complete_i_frames
            cp = complete_p_frames
            ii_ = incomplete_i_frames
            ip_ = incomplete_p_frames
            uf = complete_u_frames
            lb = last_brisque

        total = ci + cp + ii_ + ip_ + ic + uf
        frames_this_sec = total - prev_total
        inc_this_sec = ic - prev_incomplete
        inc_pct = round(inc_this_sec / frames_this_sec * 100, 2) if frames_this_sec > 0 else 0.0

        metrics = {
            "timestamp_sec": int(time.time()),
            "brisque_avg": round(sum(valid) / len(valid), 2) if valid else None,
            "brisque_min": round(min(valid), 2) if valid else None,
            "brisque_max": round(max(valid), 2) if valid else None,
            "brisque_last": round(lb, 2),
            "decode_errors_per_s": errors,
            "incomplete_pct_per_s": inc_pct,
        }

        tmp = METRICS_JSON + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(metrics, f)
            os.replace(tmp, METRICS_JSON)
        except Exception as e:
            log(f"[WARN] metrics write failed: {e}")

        prev_total = total
        prev_incomplete = ic


def display_worker():
    """Encode decoded frames to high-quality JPEG as they arrive and publish them.

    Pulls decoded ndarrays from display_queue and JPEG-encodes each one as soon as
    it arrives — native resolution, no resampling, high quality with full chroma
    (4:4:4) — then stores the bytes in latest_frame. Skips all work when no client
    is connected.
    """
    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), DISPLAY_JPEG_QUALITY]
    # Disable chroma subsampling (4:4:4) so color is preserved at full resolution,
    # when this OpenCV build supports it.
    _sf = getattr(cv2, "IMWRITE_JPEG_SAMPLING_FACTOR", None)
    _sf444 = getattr(cv2, "IMWRITE_JPEG_SAMPLING_FACTOR_444", None)
    if _sf is not None and _sf444 is not None:
        encode_params += [int(_sf), _sf444]

    while True:
        try:
            img = display_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        if latest_frame.client_count() == 0:
            continue

        try:
            ok, buf = cv2.imencode(".jpg", img, encode_params)
            if ok:
                latest_frame.set(buf.tobytes())
        except Exception as e:
            log(f"[WARN] display encode failed: {e}")


class _MJPEGHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # silence default stderr access logging

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        if path != "/video.mjpg":
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()

        latest_frame.add_client()
        version = 0
        try:
            while True:
                version, data = latest_frame.wait_newer(version, timeout=5.0)
                if data is None:
                    continue
                self.wfile.write(b"--frame\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii"))
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            latest_frame.remove_client()


def display_http_server():
    server = ThreadingHTTPServer(("0.0.0.0", DISPLAY_HTTP_PORT), _MJPEGHandler)
    server.daemon_threads = True
    log(f"MJPEG display server on 0.0.0.0:{DISPLAY_HTTP_PORT} (GET /video.mjpg)")
    server.serve_forever()


threading.Thread(target=recv_worker, daemon=True).start()
threading.Thread(target=decode_worker, daemon=True).start()
threading.Thread(target=log_worker, daemon=True).start()
threading.Thread(target=_metrics_aggregator, daemon=True).start()
threading.Thread(target=display_worker, daemon=True).start()
threading.Thread(target=display_http_server, daemon=True).start()
for _ in range(BRISQUE_WORKERS):
    threading.Thread(target=brisque_worker, daemon=True).start()

log(f"Started: 1 recv + 1 decode + 1 log-ordering + 1 metrics + 1 display + {BRISQUE_WORKERS} BRISQUE workers")
log(f"BRISQUE metrics JSON: {METRICS_JSON}")


while True:
    try:
        data = recv_queue.get(timeout=1.0)
    except queue.Empty:
        continue

    result = parse_rtp(data)
    if result is None:
        continue

    marker, seq, ts, ssrc, payload = result

    if not seen_ts:
        cur_ts = ts
        cur_ssrc = ssrc
        cur_type = 0
        seen_ts = True
        last_seq = seq
        incomplete = False
        saw_marker = False
        log(f"New stream started: SSRC={ssrc:#010x}")

    elif ssrc != cur_ssrc:
        log(f"New stream detected: SSRC changed {cur_ssrc:#010x} → {ssrc:#010x}")
        cur_ssrc = ssrc
        cur_ts = ts
        cur_type = 0
        saw_marker = False
        incomplete = False
        last_seq = seq
        current_frags = []

    elif ts != cur_ts:
        # no marker means frame is incomplete
        if not saw_marker and current_frags:
            _nm_type = "I" if cur_type == 2 else ("P" if cur_type == 1 else "U")
            frame_queue.put((
                current_frags,
                _nm_type,
                True,
                complete_p_frames
            ))
        current_frags = []
        cur_ts = ts
        cur_type = 0
        saw_marker = False
        incomplete = False
        last_seq = seq

    else:
        expected = (last_seq + 1) & 0xFFFF
        if seq != expected:
            incomplete = True
        last_seq = seq

    if cur_type != 2:
        ft = get_frame_type_from_payload(payload)
        if ft == "I":
            cur_type = 2
        elif ft == "P" and cur_type == 0:
            cur_type = 1

    nal_type = payload[0] & 0x1F
    if nal_type == 28 and len(payload) >= 2:
        fu_hdr = payload[1]
        is_start = (fu_hdr >> 7) & 0x01
        orig_type = fu_hdr & 0x1F
        fragment = payload[2:]
        if is_start:
            fragment = bytes([(payload[0] & 0xE0) | orig_type]) + fragment
        current_frags.append((seq, orig_type, is_start, fragment))
    elif nal_type == 24:
        p = payload[1:]
        for _ in range(8):
            if len(p) < 2: break
            size = struct.unpack("!H", p[:2])[0]
            p = p[2:]
            if size == 0 or size > 1400 or size > len(p): break
            t = p[0] & 0x1F
            current_frags.append((seq, t, 1, p[:size]))
            p = p[size:]
    else:
        current_frags.append((seq, nal_type, 1, payload))

    if marker:
        saw_marker = True
        _ftype = "I" if cur_type == 2 else ("P" if cur_type == 1 else "U")
        frame_queue.put((
            current_frags,
            _ftype,
            incomplete,
            complete_p_frames
        ))
        current_frags = []
        cur_type = 0
        incomplete = False
