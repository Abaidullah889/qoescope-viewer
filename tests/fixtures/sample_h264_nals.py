# Single NAL units

def nal_idr(extra: bytes = b"\x00" * 8) -> bytes:
    return bytes([0x65]) + extra


def nal_non_idr_p() -> bytes:
    """
    Non-IDR NAL unit, type 1, slice_type = 0 (P-slice).

    RBSP encoding (Exp-Golomb):
      first_mb_in_slice = 0  ->  ue(0) = '1'         (1 bit)
      slice_type        = 0  ->  ue(0) = '1'         (1 bit)
      Packed MSB-first: 11xxxxxx = 0xC0

    NAL header byte: forbidden=0, NRI=3, type=1 -> 0x61.
    """
    return bytes([0x61, 0xC0, 0x00, 0x00, 0x00])


def nal_non_idr_i() -> bytes:
    """
    Non-IDR NAL unit, type 1, slice_type = 2 (I-slice).

    RBSP encoding (Exp-Golomb):
      first_mb_in_slice = 0  ->  ue(0) = '1'        (1 bit)
      slice_type        = 2  ->  ue(2) = '011'       (3 bits)
      Packed MSB-first: 1_011_xxxx = 0xB0

    NAL header byte: 0x61.
    """
    return bytes([0x61, 0xB0, 0x00, 0x00, 0x00])


def nal_sps() -> bytes:
    """SPS NAL unit, type 7 (forbidden=0, NRI=3, type=7 -> 0x67)."""
    return bytes([0x67, 0x42, 0x00, 0x1E, 0xAB, 0xCD])


def nal_pps() -> bytes:
    """PPS NAL unit, type 8 (forbidden=0, NRI=3, type=8 -> 0x68)."""
    return bytes([0x68, 0xCE, 0x38, 0x80])


# Helpers for edge-case NALs

def nal_unknown_type(nal_type: int = 6, body: bytes = b"\x00\x00") -> bytes:
    """
    NAL unit with a type that the decoder does NOT classify as I or P.
    Default type 6 = SEI (Supplemental Enhancement Information).
    """
    header = (0x03 << 5) | (nal_type & 0x1F)  # NRI=3
    return bytes([header]) + body


def nal_empty_rbsp() -> bytes:
    """Non-IDR NAL type 1 with empty RBSP (zero bytes after header)."""
    return bytes([0x61])


def nal_idr_large(size: int = 1200) -> bytes:
    """Large IDR NAL unit for fragmentation tests."""
    return bytes([0x65]) + b"\xAB" * size
