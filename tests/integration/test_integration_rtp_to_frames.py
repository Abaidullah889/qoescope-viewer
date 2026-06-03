"""
Integration test: RTP parsing -> frame assembly pipeline.

Tests the full path from raw RTP bytes through parse_rtp(),
frame type detection, sequence gap checking, and frame push logic.
Uses a MiniFrameAssembler that mirrors decoder.py's main-loop state machine.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "fixtures"))

from decoder_loader import load_decoder
from sample_rtp_packets import build_rtp, fua_packets, stap_a
from sample_h264_nals import nal_idr, nal_non_idr_p, nal_sps

_ns = load_decoder()
parse_rtp                   = _ns["parse_rtp"]
get_frame_type_from_payload = _ns["get_frame_type_from_payload"]


class MiniFrameAssembler:
    """
    Mirrors the main-loop state machine in decoder.py:
    tracks timestamp, SSRC, sequence numbers, marker bits, and frame type.
    """

    def __init__(self):
        self.cur_ts        = None
        self.cur_ssrc      = None
        self.cur_type      = 0
        self.seen_ts       = False
        self.incomplete    = False
        self.saw_marker    = False
        self.last_seq      = None
        self.current_frags = []
        self.pushed_frames = []

    def _frame_label(self):
        if self.cur_type == 2: return "I"
        if self.cur_type == 1: return "P"
        return "U"

    def process_packet(self, seq, ts, ssrc, marker, payload):
        if not self.seen_ts:
            self.cur_ts, self.cur_ssrc = ts, ssrc
            self.cur_type = 0
            self.seen_ts = True
            self.last_seq = seq
            self.incomplete = False
            self.saw_marker = False
        elif ssrc != self.cur_ssrc:
            self.cur_ssrc, self.cur_ts = ssrc, ts
            self.cur_type = 0
            self.saw_marker = False
            self.incomplete = False
            self.last_seq = seq
            self.current_frags = []
        elif ts != self.cur_ts:
            if not self.saw_marker and self.current_frags:
                self.pushed_frames.append(
                    (list(self.current_frags), self._frame_label(), True)
                )
            self.current_frags = []
            self.cur_ts = ts
            self.cur_type = 0
            self.saw_marker = False
            self.incomplete = False
            self.last_seq = seq
        else:
            expected = (self.last_seq + 1) & 0xFFFF
            if seq != expected:
                self.incomplete = True
            self.last_seq = seq

        if self.cur_type != 2:
            ft = get_frame_type_from_payload(payload)
            if ft == "I":
                self.cur_type = 2
            elif ft == "P" and self.cur_type == 0:
                self.cur_type = 1

        self.current_frags.append((seq, payload))

        if marker:
            self.saw_marker = True
            self.pushed_frames.append(
                (list(self.current_frags), self._frame_label(), self.incomplete)
            )
            self.current_frags = []
            self.cur_type = 0
            self.incomplete = False


def feed(asm, packets):
    for raw in packets:
        parsed = parse_rtp(raw)
        assert parsed is not None
        marker, seq, ts, ssrc, payload = parsed
        asm.process_packet(seq=seq, ts=ts, ssrc=ssrc,
                           marker=bool(marker), payload=payload)


# Complete frame assembly

def test_complete_idr_frame_pushed_once():
    """Two IDR packets with same ts, marker on last -> one complete I-frame."""
    asm = MiniFrameAssembler()
    feed(asm, [
        build_rtp(payload=nal_idr(), seq=1, ts=1000, marker=False),
        build_rtp(payload=nal_idr(), seq=2, ts=1000, marker=True),
    ])
    assert len(asm.pushed_frames) == 1
    _, ft, inc = asm.pushed_frames[0]
    assert ft == "I" and inc is False


def test_complete_p_frame():
    """P-frame packets with marker -> one complete P-frame."""
    asm = MiniFrameAssembler()
    feed(asm, [
        build_rtp(payload=nal_non_idr_p(), seq=1, ts=1000, marker=False),
        build_rtp(payload=nal_non_idr_p(), seq=2, ts=1000, marker=True),
    ])
    assert asm.pushed_frames[0][1] == "P"
    assert asm.pushed_frames[0][2] is False


def test_single_packet_frame():
    """A single-packet frame with marker set -> one complete frame."""
    asm = MiniFrameAssembler()
    feed(asm, [build_rtp(payload=nal_idr(), seq=1, ts=1000, marker=True)])
    assert len(asm.pushed_frames) == 1
    assert asm.pushed_frames[0][2] is False


# Incomplete frame detection

def test_sequence_gap_marks_frame_incomplete():
    """Skipping seq 2 (1 -> 3) within same ts -> incomplete flag set."""
    asm = MiniFrameAssembler()
    feed(asm, [
        build_rtp(payload=nal_idr(), seq=1, ts=1000, marker=False),
        build_rtp(payload=nal_idr(), seq=3, ts=1000, marker=True),
    ])
    assert asm.pushed_frames[0][2] is True


def test_missing_marker_flushes_on_timestamp_change():
    """No marker on first ts, then new ts -> first frame flushed as incomplete."""
    asm = MiniFrameAssembler()
    feed(asm, [
        build_rtp(payload=nal_idr(), seq=1, ts=1000, marker=False),
        build_rtp(payload=nal_idr(), seq=2, ts=2000, marker=True),
    ])
    assert len(asm.pushed_frames) == 2
    assert asm.pushed_frames[0][2] is True   # no marker = incomplete


# SSRC handling

def test_ssrc_change_drops_old_unfinished_frame():
    """SSRC change resets state; old incomplete frags are discarded."""
    asm = MiniFrameAssembler()
    feed(asm, [
        build_rtp(payload=nal_idr(), seq=1, ts=1000, ssrc=0xAAAA, marker=False),
        build_rtp(payload=nal_idr(), seq=1, ts=2000, ssrc=0xBBBB, marker=True),
    ])
    assert len(asm.pushed_frames) == 1
    assert asm.pushed_frames[0][1] == "I"


# Sequence wraparound

def test_seq_wraparound_65535_to_0_no_gap():
    """Sequence 65535 -> 0 must NOT be treated as a gap."""
    asm = MiniFrameAssembler()
    feed(asm, [
        build_rtp(payload=nal_idr(), seq=65535, ts=4000, marker=False),
        build_rtp(payload=nal_idr(), seq=0,     ts=4000, marker=True),
    ])
    assert asm.pushed_frames[0][2] is False


# FU-A fragmented frames

def test_fua_fragmented_idr_complete():
    """An IDR NAL fragmented into FU-A packets -> one complete I-frame."""
    asm = MiniFrameAssembler()
    idr = nal_idr()
    packets = fua_packets(nal_payload=idr[1:], nal_hdr_byte=idr[0],
                          seq_start=100, ts=5000, mtu=3)
    feed(asm, packets)
    assert len(asm.pushed_frames) == 1
    _, ft, inc = asm.pushed_frames[0]
    assert ft == "I" and inc is False


# STAP-A aggregated packets

def test_stap_a_with_sps_and_idr_classified_as_i():
    """STAP-A containing SPS + IDR -> I-frame."""
    asm = MiniFrameAssembler()
    payload = stap_a([nal_sps(), nal_idr()])
    feed(asm, [build_rtp(payload=payload, seq=1, ts=6000, marker=True)])
    assert asm.pushed_frames[0][1] == "I"


# No double-flush

def test_marker_closed_frame_not_double_flushed():
    """A marker-closed frame must not be flushed again on next ts change."""
    asm = MiniFrameAssembler()
    feed(asm, [
        build_rtp(payload=nal_idr(), seq=1, ts=7000, marker=True),
        build_rtp(payload=nal_idr(), seq=2, ts=8000, marker=True),
    ])
    assert len(asm.pushed_frames) == 2


# Multi-frame sequence

def test_ippp_gop_structure():
    """Simulate a mini GOP: I, P, P, P -> 4 frames pushed."""
    asm = MiniFrameAssembler()
    feed(asm, [
        build_rtp(payload=nal_idr(),        seq=1, ts=1000, marker=True),
        build_rtp(payload=nal_non_idr_p(),  seq=2, ts=2000, marker=True),
        build_rtp(payload=nal_non_idr_p(),  seq=3, ts=3000, marker=True),
        build_rtp(payload=nal_non_idr_p(),  seq=4, ts=4000, marker=True),
    ])
    types = [f[1] for f in asm.pushed_frames]
    assert types == ["I", "P", "P", "P"]


def test_multiple_packets_per_frame():
    """Frame assembled from 4 packets (no gap) -> complete."""
    asm = MiniFrameAssembler()
    feed(asm, [
        build_rtp(payload=nal_idr(), seq=1, ts=1000, marker=False),
        build_rtp(payload=nal_idr(), seq=2, ts=1000, marker=False),
        build_rtp(payload=nal_idr(), seq=3, ts=1000, marker=False),
        build_rtp(payload=nal_idr(), seq=4, ts=1000, marker=True),
    ])
    assert len(asm.pushed_frames) == 1
    assert len(asm.pushed_frames[0][0]) == 4
    assert asm.pushed_frames[0][2] is False
