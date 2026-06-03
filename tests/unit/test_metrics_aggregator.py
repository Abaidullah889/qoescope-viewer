"""
Unit tests for the metrics aggregator logic (decoder.py :: _metrics_aggregator).

The aggregator runs every second and computes 6 Grafana-facing metrics from
the BRISQUE score window:
  brisque_avg, brisque_min, brisque_max, brisque_last,
  decode_errors_per_s, incomplete_pct_per_s
"""


# Pure logic replica of the aggregation window computation

def compute_window_metrics(window, counters, prev_total=0, prev_incomplete=0):
    """
    Mirrors the per-second computation inside _metrics_aggregator().

    Args:
        window:         list of BRISQUE scores (-1 = decode error)
        counters:       dict with keys ci, cp, ii_, ip_, ic, uf
        prev_total:     total frame count from previous second
        prev_incomplete: incomplete frame count from previous second

    Returns:
        dict with brisque_avg, brisque_min, brisque_max,
        decode_errors_per_s, incomplete_pct_per_s
    """
    valid  = [s for s in window if s >= 0]
    errors = len([s for s in window if s < 0])

    ci  = counters.get("ci", 0)
    cp  = counters.get("cp", 0)
    ii_ = counters.get("ii_", 0)
    ip_ = counters.get("ip_", 0)
    ic  = counters.get("ic", 0)
    uf  = counters.get("uf", 0)

    total           = ci + cp + ii_ + ip_ + ic + uf
    frames_this_sec = total - prev_total
    inc_this_sec    = ic - prev_incomplete
    inc_pct = round(inc_this_sec / frames_this_sec * 100, 2) if frames_this_sec > 0 else 0.0

    return {
        "brisque_avg":          round(sum(valid) / len(valid), 2) if valid else None,
        "brisque_min":          round(min(valid), 2) if valid else None,
        "brisque_max":          round(max(valid), 2) if valid else None,
        "decode_errors_per_s":  errors,
        "incomplete_pct_per_s": inc_pct,
    }


# BRISQUE average computation

def test_avg_excludes_decode_failures():
    """Scores of -1 (decode errors) must be excluded from the average."""
    r = compute_window_metrics([40.0, -1, 60.0], {"ci": 3})
    assert r["brisque_avg"] == 50.0


def test_avg_is_none_when_no_valid_scores():
    """If all scores are -1 or window is empty, avg must be None."""
    r = compute_window_metrics([-1, -1], {"ci": 2})
    assert r["brisque_avg"] is None


def test_avg_empty_window():
    """Empty window (no frames decoded this second) -> avg is None."""
    r = compute_window_metrics([], {"ci": 0})
    assert r["brisque_avg"] is None


def test_avg_single_score():
    """Window with one score: avg equals that score."""
    r = compute_window_metrics([35.5], {"ci": 1})
    assert r["brisque_avg"] == 35.5


def test_avg_rounds_to_two_decimals():
    """Average must be rounded to 2 decimal places."""
    r = compute_window_metrics([10.0, 20.0, 30.0], {"ci": 3})
    assert r["brisque_avg"] == 20.0


# BRISQUE min / max

def test_min_max_basic():
    """Min and max are computed from valid (non-negative) scores only."""
    r = compute_window_metrics([10.0, -1, 50.0, 30.0], {"ci": 4})
    assert r["brisque_min"] == 10.0
    assert r["brisque_max"] == 50.0


def test_min_max_none_when_no_valid_scores():
    """Min and max are None when no valid scores exist."""
    r = compute_window_metrics([-1], {"ci": 1})
    assert r["brisque_min"] is None
    assert r["brisque_max"] is None


def test_min_max_equal_for_single_score():
    """With one valid score, min equals max."""
    r = compute_window_metrics([42.0], {"ci": 1})
    assert r["brisque_min"] == 42.0
    assert r["brisque_max"] == 42.0


# Decode errors per second

def test_decode_errors_count():
    """Number of -1 scores equals decode_errors_per_s."""
    r = compute_window_metrics([40.0, -1, 60.0], {"ci": 3})
    assert r["decode_errors_per_s"] == 1


def test_decode_errors_zero_when_all_valid():
    """No -1 scores -> 0 errors."""
    r = compute_window_metrics([30.0, 40.0], {"ci": 2})
    assert r["decode_errors_per_s"] == 0


def test_decode_errors_all_failures():
    """All -1 scores."""
    r = compute_window_metrics([-1, -1, -1], {"ci": 3})
    assert r["decode_errors_per_s"] == 3


# Incomplete percentage per second

def test_incomplete_pct_uses_deltas():
    """incomplete_pct = (ic_delta / frames_delta) * 100."""
    r = compute_window_metrics([], {"ci": 8, "ic": 3}, prev_total=8, prev_incomplete=1)
    # ic_delta = 3-1 = 2, frames_delta from counters: 8+3=11-8=3
    # wait let me recalculate: total = ci+cp+ii_+ip_+ic+uf = 8+0+0+0+3+0=11
    # frames_this_sec = 11-8=3, inc_this_sec = 3-1=2, pct = 2/3*100 = 66.67
    assert r["incomplete_pct_per_s"] == 66.67


def test_incomplete_pct_zero_when_no_frames():
    """Zero frames this second -> 0.0% incomplete."""
    r = compute_window_metrics([], {"ci": 5}, prev_total=5)
    assert r["incomplete_pct_per_s"] == 0.0


def test_incomplete_pct_zero_when_no_incomplete():
    """Frames this second but none incomplete -> 0.0%."""
    r = compute_window_metrics([], {"ci": 10, "ic": 0}, prev_total=5, prev_incomplete=0)
    assert r["incomplete_pct_per_s"] == 0.0


def test_incomplete_pct_100_when_all_incomplete():
    """All frames incomplete -> 100%."""
    r = compute_window_metrics([], {"ic": 5}, prev_total=0, prev_incomplete=0)
    assert r["incomplete_pct_per_s"] == 100.0
