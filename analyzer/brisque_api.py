import json
import os
import time
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse

METRICS_JSON = "/app/metrics/brisque_metrics.json"

# Port of the MJPEG server hosted by decoder.py (see DISPLAY_HTTP_PORT there).
DISPLAY_HTTP_PORT = 9102


app = FastAPI(title="BRISQUE Metrics API")


@app.get("/metrics")
def get_metrics():
    if not os.path.exists(METRICS_JSON):
        raise HTTPException(status_code=503, detail="Metrics not yet available")

    try:
        with open(METRICS_JSON, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise HTTPException(status_code=503, detail=f"Could not read metrics: {e}")

    age = int(time.time()) - data.get("timestamp_sec", 0)
    if age > 5:
        data["stale"] = True
        data["stale_seconds"] = age

    return JSONResponse(content=data)


@app.get("/health")
def health():
    return {"status": "ok"}


_VIEWER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>QoEScope — Live Received Video</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #0d0d0d;
      color: #e0e0e0;
      font-family: 'Segoe UI', Consolas, monospace;
      min-height: 100vh;
    }
    header {
      padding: 14px 24px;
      background: #1a1a2e;
      border-bottom: 1px solid #2a2a4a;
      display: flex;
      align-items: center;
      gap: 12px;
    }
    header h1 { font-size: 1.1rem; letter-spacing: 0.08em; font-weight: 600; }
    header h1 span { color: #4fc3f7; }
    #dot {
      width: 10px; height: 10px; border-radius: 50%;
      background: #ef5350; flex-shrink: 0;
      transition: background 0.4s;
    }
    #dot.live { background: #66bb6a; box-shadow: 0 0 6px #66bb6a; }
    main { padding: 24px; max-width: 1280px; margin: 0 auto; }
    #stage {
      position: relative;
      background: #000;
      border: 1px solid #2a2a4a;
      border-radius: 6px;
      overflow: hidden;
      min-height: 240px;
    }
    #video { width: 100%; display: block; }
    #overlay {
      position: absolute;
      top: 12px; left: 12px;
      background: rgba(13, 13, 13, 0.72);
      border: 1px solid #2a2a4a;
      border-radius: 6px;
      padding: 10px 14px;
      font-size: 0.8rem;
      line-height: 1.55;
      pointer-events: none;
      min-width: 190px;
    }
    #overlay .row { display: flex; justify-content: space-between; gap: 18px; }
    #overlay .k { color: #888; }
    #overlay .v { color: #4fc3f7; font-weight: 600; }
    #overlay .v.warn { color: #ffb74d; }
    #overlay .v.bad  { color: #ef5350; }
    #info { margin-top: 10px; font-size: 0.8rem; color: #666; }
  </style>
</head>
<body>
  <header>
    <div id="dot"></div>
    <h1>Qoe<span>Scope</span> &mdash; Live Received Video</h1>
  </header>
  <main>
    <div id="stage">
      <img id="video" alt="received video stream">
      <div id="overlay">
        <div class="row"><span class="k">BRISQUE avg</span><span class="v" id="m-avg">—</span></div>
        <div class="row"><span class="k">BRISQUE last</span><span class="v" id="m-last">—</span></div>
        <div class="row"><span class="k">Incomplete /s</span><span class="v" id="m-inc">—</span></div>
        <div class="row"><span class="k">Decode err /s</span><span class="v" id="m-err">—</span></div>
      </div>
    </div>
    <div id="info">Connecting to received video stream&hellip;</div>
  </main>

  <script>
    const video = document.getElementById('video');
    const dot   = document.getElementById('dot');
    const info  = document.getElementById('info');

    // The MJPEG stream is served by the decoder on a dedicated port, on the
    // same host the page was loaded from.
    const MJPEG_PORT = '__MJPEG_PORT__';
    const streamUrl = () =>
      location.protocol + '//' + location.hostname + ':' + MJPEG_PORT +
      '/video.mjpg?t=' + Date.now();

    function connect() {
      video.src = streamUrl();
    }

    // MJPEG <img> ends/errors if the server restarts or the stream drops;
    // reconnect with a fresh URL.
    video.addEventListener('error', () => {
      setLive(false, 'Reconnecting to stream…');
      setTimeout(connect, 1500);
    });

    function setLive(on, text) {
      dot.className = on ? 'live' : '';
      if (text) info.textContent = text;
    }

    function fmt(x, digits) {
      return (x === null || x === undefined) ? '—' : Number(x).toFixed(digits);
    }

    // Liveness is driven by metric freshness — fresh metrics mean the decoder is
    // actively receiving and processing packets, i.e. the stream is live.
    async function pollMetrics() {
      try {
        const r = await fetch('/metrics', { cache: 'no-store' });
        if (r.ok) {
          const m = await r.json();
          const live = !m.stale;
          setLive(live, live
            ? 'Live — video reconstructed from received packets'
            : 'Stream idle — waiting for packets…');

          document.getElementById('m-avg').textContent  = fmt(m.brisque_avg, 2);
          document.getElementById('m-last').textContent = fmt(m.brisque_last, 2);

          const incEl = document.getElementById('m-inc');
          const inc = m.incomplete_pct_per_s;
          incEl.textContent = fmt(inc, 1) + '%';
          incEl.className = 'v' + (inc >= 20 ? ' bad' : inc >= 5 ? ' warn' : '');

          const errEl = document.getElementById('m-err');
          const err = m.decode_errors_per_s;
          errEl.textContent = (err === null || err === undefined) ? '—' : err;
          errEl.className = 'v' + (err >= 10 ? ' bad' : err >= 1 ? ' warn' : '');
        } else {
          setLive(false, 'Waiting for analyzer metrics…');
        }
      } catch (_) {
        setLive(false);
      }
      setTimeout(pollMetrics, 1000);
    }

    connect();
    pollMetrics();
  </script>
</body>
</html>"""


@app.get("/viewer", response_class=HTMLResponse)
def viewer():
    return _VIEWER_HTML.replace("__MJPEG_PORT__", str(DISPLAY_HTTP_PORT))
