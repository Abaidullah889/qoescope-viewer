"""
Unit tests for the Probe API (probe/app.py) and BRISQUE API (analyzer/brisque_api.py).

The Probe API reads a JSON metrics file and exposes it via GET /metrics.
The BRISQUE API reads brisque_metrics.json and adds staleness detection.
"""

import importlib.util
import os
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]


def _load_app(module_name, file_path):
    """Load a FastAPI app module with cache invalidation."""
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Probe API: /health

def test_probe_health_returns_ok(probe_client):
    """GET /health must return {ok: True}."""
    client = probe_client()
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# Probe API: /metrics

def test_probe_metrics_returns_503_when_file_missing(probe_client):
    """If the metrics file does not exist, return 503 (service unavailable)."""
    client = probe_client()
    assert client.get("/metrics").status_code == 503


def test_probe_metrics_returns_file_payload(probe_client):
    """If the file exists, its JSON content is returned verbatim."""
    payload = {"throughput_mbps": 2.5, "frames_per_s": 30}
    client = probe_client(payload)
    assert client.get("/metrics").json() == payload


def test_probe_metrics_returns_500_on_corrupt_json(tmp_path):
    """Corrupt JSON in the file must return 500."""
    f = tmp_path / "metrics.json"
    f.write_text("{ bad json", encoding="utf-8")
    os.environ["METRICS_FILE"] = str(f)
    mod = _load_app("qoescope_probe_corrupt", ROOT / "probe" / "app.py")
    client = TestClient(mod.app)
    assert client.get("/metrics").status_code == 500


def test_probe_metrics_returns_empty_object(probe_client):
    """An empty JSON object {} is valid and should be returned as-is."""
    client = probe_client({})
    assert client.get("/metrics").json() == {}


def test_probe_metrics_returns_all_fields(probe_client):
    """A full probe payload with all expected fields is returned correctly."""
    payload = {
        "throughput_bps": 5000000, "throughput_mbps": 5.0,
        "pkts_per_s": 200, "bytes_per_s": 625000,
        "frames_total": 1000, "frames_per_s": 30,
        "i_frames_total": 50, "i_frames_per_s": 1,
        "p_frames_total": 950, "p_frames_per_s": 29,
        "incomplete_frames_total": 10, "incomplete_frames_per_s": 0,
        "timestamp_sec": 1700000000,
    }
    client = probe_client(payload)
    data = client.get("/metrics").json()
    assert data["throughput_mbps"] == 5.0
    assert data["frames_per_s"] == 30


# BRISQUE API: /health

def test_brisque_health_returns_ok(brisque_client):
    """GET /health must return {status: ok}."""
    client = brisque_client()
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# BRISQUE API: /metrics

def test_brisque_metrics_returns_503_when_file_missing(brisque_client):
    """If the metrics JSON file does not exist, return 503."""
    client = brisque_client()
    assert client.get("/metrics").status_code == 503


def test_brisque_metrics_returns_503_on_corrupt_json(tmp_path):
    """Corrupt JSON must return 503."""
    f = tmp_path / "brisque.json"
    f.write_text("{ bad json", encoding="utf-8")
    mod = _load_app("qoescope_brisque_corrupt", ROOT / "analyzer" / "brisque_api.py")
    mod.METRICS_JSON = str(f)
    client = TestClient(mod.app)
    assert client.get("/metrics").status_code == 503


def test_brisque_metrics_marks_stale_file(brisque_client):
    """If timestamp_sec is older than 5 seconds, stale flag is added."""
    payload = {
        "timestamp_sec": int(time.time()) - 10,
        "brisque_avg": 35.5, "brisque_min": 20.0, "brisque_max": 50.0,
        "brisque_last": 35.5, "decode_errors_per_s": 0,
        "incomplete_pct_per_s": 0.0,
    }
    client = brisque_client(payload)
    data = client.get("/metrics").json()
    assert data.get("stale") is True
    assert data.get("stale_seconds", 0) >= 5


def test_brisque_metrics_fresh_file_not_stale(brisque_client):
    """If timestamp_sec is within 5 seconds, no stale flag."""
    payload = {
        "timestamp_sec": int(time.time()),
        "brisque_avg": 35.5, "brisque_min": 20.0, "brisque_max": 50.0,
        "brisque_last": 35.5, "decode_errors_per_s": 0,
        "incomplete_pct_per_s": 0.0,
    }
    client = brisque_client(payload)
    data = client.get("/metrics").json()
    assert data.get("stale") is not True


def test_brisque_metrics_returns_all_score_fields(brisque_client):
    """All BRISQUE score fields must be present in the response."""
    payload = {
        "timestamp_sec": int(time.time()),
        "brisque_avg": 40.0, "brisque_min": 20.0, "brisque_max": 60.0,
        "brisque_last": 55.0, "decode_errors_per_s": 1,
        "incomplete_pct_per_s": 5.5,
    }
    client = brisque_client(payload)
    data = client.get("/metrics").json()
    assert data["brisque_avg"] == 40.0
    assert data["brisque_min"] == 20.0
    assert data["brisque_max"] == 60.0
    assert data["brisque_last"] == 55.0
    assert data["decode_errors_per_s"] == 1
    assert data["incomplete_pct_per_s"] == 5.5
