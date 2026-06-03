import json
import os
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="Probe API")

METRICS_FILE = os.environ.get("METRICS_FILE", "metrics.json")

@app.get("/health")
def health():
    return {"ok": True, "metrics_file": METRICS_FILE}

@app.get("/metrics")
def metrics():
    try:
        with open(METRICS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return JSONResponse(status_code=503, content={"error": "metrics not ready", "metrics_file": METRICS_FILE})
    except json.JSONDecodeError as e:
        return JSONResponse(status_code=500, content={"error": f"invalid metrics JSON: {e}", "metrics_file": METRICS_FILE})
    except OSError as e:
        return JSONResponse(status_code=500, content={"error": str(e), "metrics_file": METRICS_FILE})
