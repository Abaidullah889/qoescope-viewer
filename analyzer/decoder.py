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
import subprocess
import time
import threading
import queue
import heapq
import av
import cv2

RTP_PORT = 5004
BUFFER_SIZE = 26214400
LOG_FILE = "decoder.log"
BRISQUE_EVERY = 1
BRISQUE_WORKERS = 4

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
        try:
            annexb = build_annexb(nal_units)
            packet = av.Packet(annexb)
            frames = codec.decode(packet)
            for frame in frames:
                if should_measure:
                    decoded_img = frame.to_ndarray(format="bgr24")
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
            _hls_fwd_sock.sendto(data, ("127.0.0.1", HLS_RTP_PORT))
        except Exception:
            pass
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
HLS_DIR = "/app/metrics/hls"
SDP_FILE = "/app/metrics/stream.sdp"
HLS_RTP_PORT = 5005

os.makedirs(os.path.dirname(METRICS_JSON), exist_ok=True)
os.makedirs(HLS_DIR, exist_ok=True)

with open(SDP_FILE, "w") as _sdp:
    _sdp.write(
        "v=0\r\n"
        "o=- 0 0 IN IP4 127.0.0.1\r\n"
        "s=QoEScope\r\n"
        "c=IN IP4 127.0.0.1\r\n"
        "t=0 0\r\n"
        f"m=video {HLS_RTP_PORT} RTP/AVP 96\r\n"
        "a=rtpmap:96 H264/90000\r\n"
        "a=fmtp:96 packetization-mode=1\r\n"
    )

_hls_fwd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

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


def _clear_hls_segments():
    for f in os.listdir(HLS_DIR):
        if f.endswith(".ts") or f.endswith(".m3u8"):
            try:
                os.remove(os.path.join(HLS_DIR, f))
            except Exception:
                pass


def _write_stream_id():
    try:
        with open(os.path.join(HLS_DIR, "stream_id.txt"), "w") as f:
            f.write(str(int(time.time())))
    except Exception:
        pass


def hls_writer():
    cmd = [
        "ffmpeg", "-y",
        "-protocol_whitelist", "file,udp,rtp",
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        "-i", SDP_FILE,
        "-c:v", "copy",
        "-f", "hls",
        "-hls_time", "1",
        "-hls_list_size", "0",
        "-hls_flags", "append_list+omit_endlist",
        "-hls_init_time", "0",
        f"{HLS_DIR}/stream.m3u8",
    ]
    log(f"HLS writer started: RTP stream copy → {HLS_DIR}/stream.m3u8")
    while True:
        _clear_hls_segments()
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.wait()
        log("[WARN] HLS FFmpeg exited, clearing segments and restarting in 2s...")
        _clear_hls_segments()
        time.sleep(2)


threading.Thread(target=recv_worker, daemon=True).start()
threading.Thread(target=decode_worker, daemon=True).start()
threading.Thread(target=log_worker, daemon=True).start()
threading.Thread(target=_metrics_aggregator, daemon=True).start()
threading.Thread(target=hls_writer, daemon=True).start()
for _ in range(BRISQUE_WORKERS):
    threading.Thread(target=brisque_worker, daemon=True).start()

log(f"Started: 1 recv + 1 decode + 1 log-ordering + 1 metrics + {BRISQUE_WORKERS} BRISQUE workers")
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
        _write_stream_id()
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
        _write_stream_id()

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
