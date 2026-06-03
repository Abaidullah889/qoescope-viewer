"""
Synthetic RTP packet builders used across all test files.

RTP fixed header layout (RFC 3550, 12 bytes minimum):
  Byte 0:   V(2) P(1) X(1) CC(4)
  Byte 1:   M(1) PT(7)
  Bytes 2-3:  sequence number  (big-endian uint16)
  Bytes 4-7:  timestamp        (big-endian uint32)
  Bytes 8-11: SSRC             (big-endian uint32)
  [CC x 4 bytes: CSRC list]
  [4 + ext_len x 4 bytes: extension header if X=1]
  [payload bytes]
"""

import struct


# RTP packet builder

def build_rtp(
    payload:     bytes,
    seq:         int   = 1,
    ts:          int   = 1000,
    ssrc:        int   = 0xABCD1234,
    marker:      bool  = False,
    csrc_list:   list  = None,
    extension:   bytes = None,
    ext_profile: int   = 0xBEDE,
) -> bytes:
    """
    Build a complete RTP packet as raw bytes.

    Args:
        payload     : NAL payload bytes (placed after all headers)
        seq         : 16-bit sequence number
        ts          : 32-bit RTP timestamp
        ssrc        : 32-bit synchronisation source identifier
        marker      : True sets the marker bit (M=1) in byte 1
        csrc_list   : list of uint32 CSRC values (max 15)
        extension   : if not None, an extension body is added.
                      MUST be padded to a 32-bit (4-byte) boundary.
        ext_profile : 16-bit profile word in the extension header
    """
    csrc_list = csrc_list or []
    cc = len(csrc_list)
    assert cc <= 15, "CC is a 4-bit field"

    x_bit = 1 if extension is not None else 0
    m_bit = 1 if marker else 0

    byte0 = (2 << 6) | (x_bit << 4) | (cc & 0x0F)
    byte1 = (m_bit << 7) | 96

    fixed_header = struct.pack(
        "!BBHII",
        byte0, byte1,
        seq  & 0xFFFF,
        ts   & 0xFFFFFFFF,
        ssrc & 0xFFFFFFFF,
    )

    csrc_bytes = b"".join(struct.pack("!I", c) for c in csrc_list)

    ext_bytes = b""
    if extension is not None:
        assert len(extension) % 4 == 0, "Extension body must be 32-bit aligned"
        ext_len_words = len(extension) // 4
        ext_bytes = struct.pack("!HH", ext_profile, ext_len_words) + extension

    return fixed_header + csrc_bytes + ext_bytes + payload


# FU-A fragmentation builder

def fua_packets(
    nal_payload:  bytes,
    nal_hdr_byte: int,
    seq_start:    int   = 1,
    ts:           int   = 1000,
    ssrc:         int   = 0xABCD1234,
    mtu:          int   = 8,
) -> list:
    """
    Fragment one NAL unit into a list of FU-A RTP packets.

    FU-A layout (RFC 6184 section 5.8):
      payload[0] = FU indicator : (NRI from orig hdr) | 28
      payload[1] = FU header    : S(1) E(1) R(1) orig_type(5)
      payload[2:] = fragment of NAL body

    Marker bit is set ONLY on the last fragment.
    """
    orig_type    = nal_hdr_byte & 0x1F
    nri          = (nal_hdr_byte >> 5) & 0x03
    fu_indicator = (nri << 5) | 28

    chunks = [nal_payload[i:i + mtu] for i in range(0, len(nal_payload), mtu)]
    packets = []

    for idx, chunk in enumerate(chunks):
        is_start = (idx == 0)
        is_end   = (idx == len(chunks) - 1)

        fu_header = (
            ((1 if is_start else 0) << 7) |
            ((1 if is_end   else 0) << 6) |
            orig_type
        )
        rtp_payload = bytes([fu_indicator, fu_header]) + chunk

        pkt = build_rtp(
            payload=rtp_payload,
            seq=seq_start + idx,
            ts=ts,
            ssrc=ssrc,
            marker=is_end,
        )
        packets.append(pkt)

    return packets


# STAP-A aggregation builder

def stap_a(nal_list: list) -> bytes:
    """
    Build a STAP-A RTP payload containing multiple NAL units.

    STAP-A layout (RFC 6184 section 5.7.1):
      payload[0]      = STAP-A type byte (NRI=3, type=24 -> 0x78)
      payload[1:3]    = size of first inner NAL (big-endian uint16)
      payload[3:3+sz] = first inner NAL bytes
      ... repeated
    """
    stap_hdr = bytes([0x78])
    body = b""
    for nal in nal_list:
        body += struct.pack("!H", len(nal)) + nal
    return stap_hdr + body


# Convenience builders for common packet patterns

def build_rtp_raw(
    payload: bytes = b"\x65",
    seq: int = 1,
    ts: int = 1000,
    ssrc: int = 0xABCD,
    version: int = 2,
    marker: bool = False,
    cc: int = 0,
    csrc_bytes: bytes = b"",
    ext_bytes: bytes = b"",
) -> bytes:
    """
    Low-level RTP builder that allows invalid field values for negative testing.
    Unlike build_rtp(), this does not validate inputs.
    """
    byte0 = ((version & 0x03) << 6) | ((1 if ext_bytes else 0) << 4) | (cc & 0x0F)
    byte1 = ((1 if marker else 0) << 7) | 96
    hdr = struct.pack("!BBHII", byte0, byte1, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)
    return hdr + csrc_bytes + ext_bytes + payload
