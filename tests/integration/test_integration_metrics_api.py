"""
Integration test: BRISQUE metrics API end-to-end.

Tests the full flow: decoder writes metrics JSON -> API reads and serves it.
Also tests the Probe API serving metrics from its JSON file.
"""

import time


# BRISQUE API integration

def test_brisque_api_returns_full_metrics_payload(brisque_client):
    """
    Simulate decoder writing brisque_metrics.json, then verify the API
    serves all fields correctly with proper types.
    """
    metrics = {
        "timestamp_sec": int(time.time()),
        "brisque_avg": 32.5,
        "brisque_min": 18.0,
        "brisque_max": 55.0,
        "brisque_last": 40.0,
        "decode_errors_per_s": 1,
        "incomplete_pct_per_s": 3.5,
    }
    client = brisque_client(metrics)

    resp = client.get("/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["brisque_avg"] == 32.5
    assert data["brisque_min"] == 18.0
    assert data["brisque_max"] == 55.0
    assert data["decode_errors_per_s"] == 1
    assert data["incomplete_pct_per_s"] == 3.5
    assert "stale" not in data


def test_brisque_api_detects_stale_metrics(brisque_client):
    """
    When the metrics file timestamp is older than 5s, the API adds
    stale=True and stale_seconds to the response.
    """
    metrics = {
        "timestamp_sec": int(time.time()) - 15,
        "brisque_avg": 30.0, "brisque_min": 25.0, "brisque_max": 35.0,
        "brisque_last": 30.0, "decode_errors_per_s": 0,
        "incomplete_pct_per_s": 0.0,
    }
    client = brisque_client(metrics)

    data = client.get("/metrics").json()
    assert data["stale"] is True
    assert data["stale_seconds"] >= 10


def test_brisque_api_lifecycle_startup_to_ready(brisque_client):
    """
    Simulate the lifecycle: file doesn't exist (503), then appears (200).
    """
    client = brisque_client()

    # Before decoder starts
    assert client.get("/metrics").status_code == 503

    # After decoder writes first metrics, need a new client with data
    metrics = {
        "timestamp_sec": int(time.time()),
        "brisque_avg": 40.0, "brisque_min": 35.0, "brisque_max": 45.0,
        "brisque_last": 40.0, "decode_errors_per_s": 0,
        "incomplete_pct_per_s": 0.0,
    }
    client = brisque_client(metrics)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.json()["brisque_avg"] == 40.0


def test_brisque_api_handles_null_scores(brisque_client):
    """
    When no frames were scored (brisque_avg is null), the API
    still returns a valid response.
    """
    metrics = {
        "timestamp_sec": int(time.time()),
        "brisque_avg": None, "brisque_min": None, "brisque_max": None,
        "brisque_last": 0.0, "decode_errors_per_s": 0,
        "incomplete_pct_per_s": 0.0,
    }
    client = brisque_client(metrics)

    resp = client.get("/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["brisque_avg"] is None


# Probe API integration

def test_probe_api_serves_xdp_metrics(probe_client):
    """
    Simulate the XDP probe writing metrics, then verify the Probe API
    serves them correctly.
    """
    metrics = {
        "timestamp_sec": int(time.time()),
        "throughput_bps": 10_000_000, "throughput_mbps": 10.0,
        "pkts_per_s": 500, "bytes_per_s": 1_250_000,
        "frames_total": 3000, "frames_per_s": 30,
        "i_frames_total": 100, "i_frames_per_s": 1,
        "p_frames_total": 2900, "p_frames_per_s": 29,
        "incomplete_frames_total": 15,
        "pkt_loss_pct": 0.3,
    }
    client = probe_client(metrics)

    resp = client.get("/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["throughput_mbps"] == 10.0
    assert data["frames_per_s"] == 30
    assert data["pkt_loss_pct"] == 0.3


def test_probe_health_includes_metrics_file_path(probe_client):
    """The /health endpoint shows which metrics file is configured."""
    client = probe_client()
    data = client.get("/health").json()
    assert data["ok"] is True
    assert "metrics_file" in data
