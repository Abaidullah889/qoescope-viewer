"""
Tests for NAL unit handling in decoder.py covering:
  - extract_nal_units()   FU-A reassembly
  - build_annexb()        Annex B start codes
"""

import struct
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "fixtures"))

from decoder_loader import load_decoder
from sample_h264_nals import nal_idr, nal_sps

_ns = load_decoder()
extract_nal_units = _ns["extract_nal_units"]
build_annexb      = _ns["build_annexb"]


# Helpers for building frags tuples

def single_frag(seq, nal_bytes):
    nal_type = nal_bytes[0] & 0x1F
    return (seq, nal_type, 1, nal_bytes)


def fua_start(seq, orig_type, nri, body):
    recon = bytes([(nri << 5) | orig_type])
    return (seq, orig_type, 1, recon + body)


def fua_cont(seq, orig_type, body):
    return (seq, orig_type, 0, body)


# NAL assembly: extract_nal_units

def test_single_nal_extracted_correctly():
    """Single IDR NAL in frags -> one unit out, bytes unchanged."""
    idr = nal_idr()
    units = extract_nal_units([single_frag(1, idr)])
    assert len(units) == 1
    assert units[0][0] == 5
    assert units[0][1] == idr


def test_fua_start_and_continuation_joined_into_one_unit():
    """FU-A start + continuation -> single NAL unit with both bodies joined."""
    body1 = b"\xAA\xBB"
    body2 = b"\xCC\xDD"
    frags = [
        fua_start(1, orig_type=5, nri=3, body=body1),
        fua_cont(2, orig_type=5, body=body2),
    ]
    units = extract_nal_units(frags)
    assert len(units) == 1
    _, joined = units[0]
    assert joined[0] == 0x65
    assert joined[1:3] == body1
    assert joined[3:5] == body2


def test_fua_start_dropped_continuations_discarded():
    """
    Start packet dropped -> only continuations arrive.
    extract_nal_units must discard them silently - no corrupted NAL output.
    """
    frags = [
        fua_cont(1, orig_type=1, body=b"\x01\x02"),
        fua_cont(2, orig_type=1, body=b"\x03\x04"),
    ]
    assert extract_nal_units(frags) == []


def test_sps_arrival_flushes_pending_fua():
    """
    FU-A start is buffered, then SPS (type 7) arrives.
    SPS must flush the FU-A buffer -> both units appear in output.
    """
    frags = [
        fua_start(1, orig_type=1, nri=3, body=b"\xC0\x00\x00\x00"),
        single_frag(2, nal_sps()),
    ]
    units = extract_nal_units(frags)
    assert len(units) == 2
    types = {u[0] for u in units}
    assert 1 in types
    assert 7 in types


def test_empty_frags_returns_empty():
    assert extract_nal_units([]) == []


# build_annexb

def test_annexb_start_code_prepended():
    """Every NAL must be prefixed with the 4-byte Annex B start code."""
    nal = b"\x65\xAA\xBB"
    out = build_annexb([(5, nal)])
    assert out[:4] == b"\x00\x00\x00\x01"
    assert out[4:] == nal


def test_annexb_two_nals_both_get_start_codes():
    nal1 = b"\x67\x42"
    nal2 = b"\x65\xAA"
    out = build_annexb([(7, nal1), (5, nal2)])
    assert out[0:4] == b"\x00\x00\x00\x01"
    assert out[4:6] == nal1
    assert out[6:10] == b"\x00\x00\x00\x01"
    assert out[10:] == nal2


def test_annexb_empty_input_returns_empty():
    assert build_annexb([]) == b""
