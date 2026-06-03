"""
Unit tests for H.264 frame type detection
(decoder.py :: get_frame_type_from_payload).

The detector classifies RTP payloads as I-frame, P-frame, or unknown (None)
by inspecting the NAL unit type and, for non-IDR slices, reading the
slice_type field in the RBSP.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "fixtures"))

from decoder_loader import load_decoder
from sample_h264_nals import (
    nal_idr, nal_non_idr_i, nal_non_idr_p, nal_sps, nal_pps,
    nal_unknown_type, nal_empty_rbsp,
)
from sample_rtp_packets import stap_a

_ns = load_decoder()
get_frame_type_from_payload = _ns["get_frame_type_from_payload"]


# FU-A payload helper

def fua_payload(orig_type: int, is_start: bool,
                rbsp: bytes = b"\xC0\x00\x00\x00") -> bytes:
    """Build a minimal FU-A RTP payload for type-detection tests."""
    fu_indicator = (3 << 5) | 28
    fu_header = ((1 if is_start else 0) << 7) | orig_type
    return bytes([fu_indicator, fu_header]) + rbsp


# Single NAL unit type detection

def test_idr_nal_is_i_frame():
    """NAL type 5 (IDR) always means I-frame."""
    assert get_frame_type_from_payload(nal_idr()) == "I"


def test_non_idr_p_slice_is_p_frame():
    """NAL type 1 with slice_type 0 -> P-frame."""
    assert get_frame_type_from_payload(nal_non_idr_p()) == "P"


def test_non_idr_i_slice_is_i_frame():
    """NAL type 1 with slice_type 2 -> I-frame."""
    assert get_frame_type_from_payload(nal_non_idr_i()) == "I"


def test_sps_returns_none():
    """SPS (type 7) is not a coded slice -> None."""
    assert get_frame_type_from_payload(nal_sps()) is None


def test_pps_returns_none():
    """PPS (type 8) is not a coded slice -> None."""
    assert get_frame_type_from_payload(nal_pps()) is None


def test_unknown_nal_type_returns_none():
    """NAL types outside {1,5,24,28} return None."""
    assert get_frame_type_from_payload(nal_unknown_type(6)) is None


def test_empty_rbsp_non_idr_returns_none():
    """NAL type 1 with empty RBSP cannot determine slice type -> None."""
    assert get_frame_type_from_payload(nal_empty_rbsp()) is None


# FU-A type detection

def test_fua_idr_reports_i_frame():
    """FU-A with orig_type=5 always means I-frame, regardless of start bit."""
    assert get_frame_type_from_payload(fua_payload(orig_type=5, is_start=False)) == "I"


def test_fua_non_idr_start_with_p_slice():
    """FU-A start fragment with orig_type=1 and P-slice RBSP -> P-frame."""
    assert get_frame_type_from_payload(fua_payload(orig_type=1, is_start=True)) == "P"


def test_fua_non_idr_continuation_returns_none():
    """FU-A continuation (no start bit, type 1) cannot determine type -> None."""
    assert get_frame_type_from_payload(fua_payload(orig_type=1, is_start=False)) is None


def test_fua_too_short_returns_none():
    """FU-A indicator byte alone (missing FU header) -> None."""
    assert get_frame_type_from_payload(bytes([0x7C])) is None


# STAP-A type detection

def test_stap_a_first_known_type_wins():
    """STAP-A returns the first classifiable inner NAL type."""
    payload = stap_a([nal_non_idr_p(), nal_idr()])
    assert get_frame_type_from_payload(payload) == "P"


def test_stap_a_sps_then_idr_reports_i():
    """SPS is skipped (returns None), so IDR is first classifiable -> I."""
    payload = stap_a([nal_sps(), nal_idr()])
    assert get_frame_type_from_payload(payload) == "I"


def test_stap_a_with_only_sps_pps_returns_none():
    """STAP-A containing only parameter sets -> None (no coded slices)."""
    payload = stap_a([nal_sps(), nal_pps()])
    assert get_frame_type_from_payload(payload) is None


def test_stap_a_empty_body_returns_none():
    """STAP-A with only the type byte and no inner NALs -> None."""
    assert get_frame_type_from_payload(bytes([0x78])) is None
