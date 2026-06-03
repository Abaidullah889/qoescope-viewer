import importlib.util
import json
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
PROBE_APP_PATH = ROOT / "probe" / "app.py"
BRISQUE_APP_PATH = ROOT / "analyzer" / "brisque_api.py"


def _load_module(module_name: str, file_path: Path):
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def probe_client(tmp_path, monkeypatch):
    def _factory(payload: dict | None = None) -> TestClient:
        metrics_file = tmp_path / "probe_metrics.json"
        if payload is None:
            if metrics_file.exists():
                metrics_file.unlink()
        else:
            metrics_file.write_text(json.dumps(payload), encoding="utf-8")

        monkeypatch.setenv("METRICS_FILE", str(metrics_file))
        module_name = f"qoescope_probe_test_{uuid.uuid4().hex}"
        module = _load_module(module_name, PROBE_APP_PATH)
        return TestClient(module.app)

    return _factory


@pytest.fixture
def brisque_client(tmp_path):
    def _factory(payload: dict | None = None) -> TestClient:
        metrics_file = tmp_path / "brisque_metrics.json"
        if payload is None:
            if metrics_file.exists():
                metrics_file.unlink()
        else:
            metrics_file.write_text(json.dumps(payload), encoding="utf-8")

        module_name = f"qoescope_brisque_test_{uuid.uuid4().hex}"
        module = _load_module(module_name, BRISQUE_APP_PATH)
        module.METRICS_JSON = str(metrics_file)
        return TestClient(module.app)

    return _factory
