"""
Unit tests for the RTP packet parser (decoder.py :: parse_rtp).

The parser extracts marker, sequence number, timestamp, SSRC, and payload
from raw UDP datagrams according to RFC 3550.  It must reject malformed
packets (wrong version, truncated headers, CSRC overruns, etc.).
"""

import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "fixtures"))

from decoder_loader import load_decoder
from sample_rtp_packets import build_rtp, build_rtp_raw
from sample_h264_nals import nal_idr

parse_rtp = load_decoder()["parse_rtp"]


# Valid packet parsing

def test_parse_valid_packet_extracts_all_fields():
    """All five returned fields (marker, seq, ts, ssrc, payload) must match."""
    payload = bytes([0x65, 0xAA, 0xBB])
    marker, seq, ts, ssrc, got = parse_rtp(
        build_rtp(payload=payload, seq=42, ts=90000, ssrc=0xDEADBEEF, marker=True)
    )
    assert marker == 1
    assert seq == 42
    assert ts == 90000
    assert ssrc == 0xDEADBEEF
    assert got == payload


def test_parse_non_marker_packet_returns_marker_zero():
    """Marker bit must be 0 when M is not set."""
    marker, *_ = parse_rtp(build_rtp(payload=nal_idr(), marker=False))
    assert marker == 0


def test_parse_skips_csrc_fields():
    """Parser must skip over CSRC entries to reach the payload."""
    payload = b"\x65\x11\x22"
    pkt = build_rtp(payload=payload, csrc_list=[0x11223344])
    result = parse_rtp(pkt)
    assert result is not None
    assert result[-1] == payload


def test_parse_skips_extension_header():
    """Parser must skip past the 4-byte extension header + body."""
    payload = b"\x65\x11\x22"
    pkt = build_rtp(payload=payload, extension=b"\xDE\xAD\xBE\xEF")
    result = parse_rtp(pkt)
    assert result is not None
    assert result[-1] == payload


def test_parse_skips_csrc_and_extension_combined():
    """Parser handles both CSRC list and extension header together."""
    payload = b"\x65\xAA"
    pkt = build_rtp(
        payload=payload,
        csrc_list=[0x11223344],
        extension=b"\xDE\xAD\xBE\xEF",
    )
    result = parse_rtp(pkt)
    assert result is not None
    assert result[-1] == payload


def test_parse_preserves_sequence_wraparound_values():
    """Sequence numbers 0 and 65535 must both be returned correctly."""
    seq_max  = parse_rtp(build_rtp(payload=b"\x65", seq=65535))[1]
    seq_zero = parse_rtp(build_rtp(payload=b"\x65", seq=0))[1]
    assert (seq_max, seq_zero) == (65535, 0)


def test_parse_preserves_maximum_timestamp():
    """32-bit max timestamp 0xFFFFFFFF must survive parsing."""
    _, _, ts, _, _ = parse_rtp(build_rtp(payload=b"\x65", ts=0xFFFFFFFF))
    assert ts == 0xFFFFFFFF


def test_parse_different_ssrc_values():
    """Different SSRC values must be returned correctly."""
    for ssrc_val in [0x00000000, 0xFFFFFFFF, 0xDEADBEEF, 0x12345678]:
        _, _, _, ssrc, _ = parse_rtp(build_rtp(payload=b"\x65", ssrc=ssrc_val))
        assert ssrc == ssrc_val


# Rejection of invalid packets

def test_parse_rejects_packet_shorter_than_12_bytes():
    """Packets smaller than the fixed header must be rejected."""
    assert parse_rtp(b"\x80\x60\x00\x01") is None


def test_parse_rejects_empty_input():
    """Zero-length input must be rejected."""
    assert parse_rtp(b"") is None


def test_parse_rejects_invalid_version_1():
    """RTP version must be 2; version 1 is rejected."""
    assert parse_rtp(build_rtp_raw(version=1)) is None


def test_parse_rejects_invalid_version_0():
    """RTP version 0 is rejected."""
    assert parse_rtp(build_rtp_raw(version=0)) is None


def test_parse_rejects_invalid_version_3():
    """RTP version 3 is rejected."""
    assert parse_rtp(build_rtp_raw(version=3)) is None


def test_parse_rejects_header_only_no_payload():
    """A 12-byte header with no payload bytes must be rejected."""
    header_only = struct.pack("!BBHII", 0x80, 0x60, 1, 1000, 0xABCD)
    assert parse_rtp(header_only) is None


def test_parse_rejects_csrc_count_overrun():
    """CC claims 3 CSRCs but only 1 is present -> offset > len(data)."""
    pkt = build_rtp_raw(payload=b"\x65", cc=3, csrc_bytes=b"\x00\x00\x00\x00")
    assert parse_rtp(pkt) is None


def test_parse_rejects_truncated_extension_header():
    """Extension bit set but fewer than 4 extension header bytes."""
    byte0 = (2 << 6) | (1 << 4)
    hdr = struct.pack("!BBHII", byte0, 96, 1, 1000, 0xABCD)
    assert parse_rtp(hdr + b"\xBE\xDE") is None


