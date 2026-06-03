"""
Integration test: forwarder queue -> decoder pipeline.

Tests the packet forwarding queue behavior: FIFO ordering, overflow handling
(drop-oldest), and that surviving packets remain parseable by the decoder.
"""

import os
import queue
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "fixtures"))

from decoder_loader import load_decoder
from sample_rtp_packets import build_rtp
from sample_h264_nals import nal_idr, nal_non_idr_p

parse_rtp = load_decoder()["parse_rtp"]


def receiver_put(q: queue.Queue, data: bytes) -> bool:
    """Drop-oldest-and-retry pattern used by forwarder's receiver thread."""
    try:
        q.put_nowait(data)
        return True
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(data)
            return True
        except queue.Full:
            return False


# Basic queue operations

def test_queue_preserves_packet_bytes():
    """A packet enqueued and dequeued must be byte-identical."""
    q = queue.Queue(maxsize=10)
    pkt = build_rtp(payload=nal_idr(), seq=1, ts=1000, marker=True)
    receiver_put(q, pkt)
    assert q.get_nowait() == pkt


def test_queue_fifo_order_when_not_overflowing():
    """Packets must come out in FIFO order when queue has room."""
    q = queue.Queue(maxsize=10)
    pkts = [build_rtp(payload=nal_idr(), seq=i, ts=i*1000, marker=(i==3))
            for i in range(1, 4)]
    for pkt in pkts:
        receiver_put(q, pkt)
    drained = [q.get_nowait() for _ in range(3)]
    assert drained == pkts


# Overflow handling

def test_overflow_drops_oldest_keeps_newest():
    """When queue is full, the oldest packet is dropped to make room."""
    q = queue.Queue(maxsize=3)
    first  = build_rtp(payload=nal_idr(), seq=1, ts=1000)
    second = build_rtp(payload=nal_idr(), seq=2, ts=1000)
    third  = build_rtp(payload=nal_idr(), seq=3, ts=1000)
    newest = build_rtp(payload=nal_non_idr_p(), seq=4, ts=1000, marker=True)

    receiver_put(q, first)
    receiver_put(q, second)
    receiver_put(q, third)
    receiver_put(q, newest)

    remaining = [q.get_nowait() for _ in range(3)]
    assert first not in remaining
    assert newest in remaining


def test_overflow_queue_never_exceeds_maxsize():
    """After many overflow inserts, queue size stays at maxsize."""
    q = queue.Queue(maxsize=4)
    for i in range(50):
        receiver_put(q, build_rtp(payload=nal_idr(), seq=i, ts=i*1000))
    assert q.qsize() == 4


# Post-overflow packet validity

def test_overflow_survivor_is_parseable():
    """Packets surviving overflow must still be valid RTP for the decoder."""
    q = queue.Queue(maxsize=2)
    receiver_put(q, build_rtp(payload=nal_idr(), seq=10, ts=1000))
    receiver_put(q, build_rtp(payload=nal_idr(), seq=11, ts=1000))
    survivor = build_rtp(payload=nal_non_idr_p(), seq=99, ts=2000, marker=True)
    receiver_put(q, survivor)

    found = False
    while not q.empty():
        pkt = q.get_nowait()
        parsed = parse_rtp(pkt)
        assert parsed is not None
        if parsed[1] == 99:
            found = True
            assert parsed[4] == nal_non_idr_p()
    assert found is True


def test_multiple_overflows_all_survivors_valid():
    """
    Simulate rapid bursts causing multiple overflows.
    Every surviving packet must remain a valid RTP packet.
    """
    q = queue.Queue(maxsize=5)
    for i in range(100):
        receiver_put(q, build_rtp(payload=nal_idr(), seq=i, ts=i*1000))

    while not q.empty():
        pkt = q.get_nowait()
        parsed = parse_rtp(pkt)
        assert parsed is not None
        assert len(parsed) == 5


# Forwarder -> decoder integration

def test_forwarded_packet_type_preserved():
    """
    After forwarding through the queue, the frame type detected by the
    decoder must match the original NAL type.
    """
    ns = load_decoder()
    gft = ns["get_frame_type_from_payload"]

    q = queue.Queue(maxsize=10)
    idr_pkt = build_rtp(payload=nal_idr(), seq=1, ts=1000, marker=True)
    p_pkt   = build_rtp(payload=nal_non_idr_p(), seq=2, ts=2000, marker=True)

    receiver_put(q, idr_pkt)
    receiver_put(q, p_pkt)

    pkt1 = q.get_nowait()
    pkt2 = q.get_nowait()

    _, _, _, _, payload1 = parse_rtp(pkt1)
    _, _, _, _, payload2 = parse_rtp(pkt2)

    assert gft(payload1) == "I"
    assert gft(payload2) == "P"
