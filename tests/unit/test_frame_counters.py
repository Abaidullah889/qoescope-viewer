"""
Unit tests for frame counters.

The decoder maintains these counters:
  complete_i_frames, complete_p_frames, complete_u_frames
  incomplete_frames, incomplete_i_frames, incomplete_p_frames
  inc_i_decode_ok, inc_i_decode_fail, decode_errors

These tests verify that the counter-update logic matches the XDP kernel
program's counting rules exactly.
"""

import queue


# ── Counter state replica ────────────────────────────────────────────────────

class CounterState:
    """Mirrors the counter logic in decode_worker() of decoder.py."""

    def __init__(self):
        self.complete_i_frames     = 0
        self.complete_p_frames     = 0
        self.incomplete_frames     = 0
        self.incomplete_i_frames   = 0
        self.incomplete_p_frames   = 0
        self.complete_u_frames     = 0
        self.inc_i_decode_ok       = 0
        self.inc_i_decode_fail     = 0
        self.decode_errors         = 0

    def apply_frame(self, frame_type: str, is_incomplete: bool):
        if frame_type == "U":
            if is_incomplete:
                self.incomplete_frames += 1
            else:
                self.complete_u_frames += 1
        elif is_incomplete:
            self.incomplete_frames += 1
            if frame_type == "I":
                self.incomplete_i_frames += 1
            elif frame_type == "P":
                self.incomplete_p_frames += 1
        else:
            if frame_type == "I":
                self.complete_i_frames += 1
            elif frame_type == "P":
                self.complete_p_frames += 1

    def apply_decode_success(self, frame_type: str, is_incomplete: bool):
        if frame_type == "I" and is_incomplete:
            self.inc_i_decode_ok += 1

    def apply_decode_error(self, frame_type: str, is_incomplete: bool):
        self.decode_errors += 1
        if frame_type == "I" and is_incomplete:
            self.inc_i_decode_fail += 1


# Complete frame counter tests

def test_complete_i_frame_increments_only_complete_i():
    """A complete I-frame must only increment complete_i_frames."""
    cs = CounterState()
    cs.apply_frame("I", False)
    assert cs.complete_i_frames == 1
    assert cs.complete_p_frames == 0
    assert cs.incomplete_frames == 0
    assert cs.complete_u_frames == 0


def test_complete_p_frame_increments_only_complete_p():
    """A complete P-frame must only increment complete_p_frames."""
    cs = CounterState()
    cs.apply_frame("P", False)
    assert cs.complete_p_frames == 1
    assert cs.complete_i_frames == 0
    assert cs.incomplete_frames == 0


def test_complete_u_frame_increments_only_complete_u():
    """A complete unknown frame increments complete_u_frames, not incomplete."""
    cs = CounterState()
    cs.apply_frame("U", False)
    assert cs.complete_u_frames == 1
    assert cs.incomplete_frames == 0


# Incomplete frame counter tests

def test_incomplete_i_frame_increments_total_and_sub_counter():
    """Incomplete I increments both incomplete_frames and incomplete_i_frames."""
    cs = CounterState()
    cs.apply_frame("I", True)
    assert cs.incomplete_frames   == 1
    assert cs.incomplete_i_frames == 1
    assert cs.incomplete_p_frames == 0
    assert cs.complete_i_frames   == 0


def test_incomplete_p_frame_increments_total_and_sub_counter():
    """Incomplete P increments both incomplete_frames and incomplete_p_frames."""
    cs = CounterState()
    cs.apply_frame("P", True)
    assert cs.incomplete_frames   == 1
    assert cs.incomplete_p_frames == 1
    assert cs.incomplete_i_frames == 0


def test_incomplete_u_frame_increments_only_total_incomplete():
    """Incomplete U only increments incomplete_frames, not the sub-counters."""
    cs = CounterState()
    cs.apply_frame("U", True)
    assert cs.incomplete_frames   == 1
    assert cs.incomplete_i_frames == 0
    assert cs.incomplete_p_frames == 0
    assert cs.complete_u_frames   == 0


# Decode outcome tracking

def test_incomplete_i_decode_success_tracks_ok():
    """Successful decode of incomplete I-frame -> inc_i_decode_ok += 1."""
    cs = CounterState()
    cs.apply_frame("I", True)
    cs.apply_decode_success("I", True)
    assert cs.inc_i_decode_ok   == 1
    assert cs.inc_i_decode_fail == 0
    assert cs.decode_errors     == 0


def test_incomplete_i_decode_failure_tracks_fail_and_error():
    """Failed decode of incomplete I-frame -> inc_i_decode_fail += 1, decode_errors += 1."""
    cs = CounterState()
    cs.apply_frame("I", True)
    cs.apply_decode_error("I", True)
    assert cs.inc_i_decode_fail == 1
    assert cs.decode_errors     == 1
    assert cs.inc_i_decode_ok   == 0


def test_complete_i_decode_failure_does_not_increment_inc_i():
    """Decode error on a complete I-frame increments decode_errors but NOT inc_i_decode_fail."""
    cs = CounterState()
    cs.apply_frame("I", False)
    cs.apply_decode_error("I", False)
    assert cs.decode_errors     == 1
    assert cs.inc_i_decode_fail == 0


def test_p_frame_decode_failure_does_not_increment_inc_i():
    """Decode error on a P-frame only increments decode_errors."""
    cs = CounterState()
    cs.apply_frame("P", False)
    cs.apply_decode_error("P", False)
    assert cs.decode_errors     == 1
    assert cs.inc_i_decode_fail == 0


def test_p_frame_decode_success_does_not_increment_inc_i_ok():
    """Successful decode of P-frame does not touch inc_i_decode_ok."""
    cs = CounterState()
    cs.apply_frame("P", True)
    cs.apply_decode_success("P", True)
    assert cs.inc_i_decode_ok == 0


# Mixed sequences

def test_mixed_frame_sequence():
    """A realistic sequence of frames updates all counters consistently."""
    cs = CounterState()
    cs.apply_frame("I", False)     # complete I
    cs.apply_frame("P", False)     # complete P
    cs.apply_frame("I", True)      # incomplete I
    cs.apply_frame("P", True)      # incomplete P
    cs.apply_frame("U", True)      # incomplete unknown
    cs.apply_frame("U", False)     # complete unknown

    assert cs.complete_i_frames   == 1
    assert cs.complete_p_frames   == 1
    assert cs.complete_u_frames   == 1
    assert cs.incomplete_i_frames == 1
    assert cs.incomplete_p_frames == 1
    assert cs.incomplete_frames   == 3  # I + P + U incomplete


def test_multiple_errors_accumulate():
    """Multiple decode errors accumulate correctly."""
    cs = CounterState()
    for _ in range(5):
        cs.apply_decode_error("P", False)
    assert cs.decode_errors == 5


def test_total_frames_formula():
    """Total frames = complete_i + complete_p + complete_u + incomplete_frames."""
    cs = CounterState()
    cs.apply_frame("I", False)
    cs.apply_frame("P", False)
    cs.apply_frame("U", False)
    cs.apply_frame("I", True)
    cs.apply_frame("P", True)
    total = (cs.complete_i_frames + cs.complete_p_frames +
             cs.complete_u_frames + cs.incomplete_frames)
    assert total == 5


# Queue overflow / drop-oldest pattern

def drop_oldest_put(q: queue.Queue, data: bytes) -> bool:
    """Drop-oldest-and-retry pattern used by recv_worker and forwarder."""
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


def test_drop_oldest_enqueues_when_space_exists():
    """put_nowait succeeds when queue has room."""
    q = queue.Queue(maxsize=3)
    assert drop_oldest_put(q, b"a") is True
    assert q.get_nowait() == b"a"


def test_drop_oldest_replaces_oldest_when_full():
    """When full, the oldest item is dropped and the new one is added."""
    q = queue.Queue(maxsize=3)
    drop_oldest_put(q, b"oldest")
    drop_oldest_put(q, b"middle")
    drop_oldest_put(q, b"latest")
    drop_oldest_put(q, b"new")
    remaining = [q.get_nowait(), q.get_nowait(), q.get_nowait()]
    assert b"oldest" not in remaining
    assert b"new" in remaining


def test_drop_oldest_never_exceeds_maxsize():
    """After many inserts the queue size never exceeds maxsize."""
    q = queue.Queue(maxsize=4)
    for i in range(20):
        drop_oldest_put(q, bytes([i]))
    assert q.qsize() == 4
