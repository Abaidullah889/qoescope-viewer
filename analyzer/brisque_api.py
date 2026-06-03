import json
import os
import time
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse

METRICS_JSON = "/app/metrics/brisque_metrics.json"
HLS_DIR = "/app/metrics/hls"

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


@app.get("/hls/{filename}")
def hls_file(filename: str):
    if ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = os.path.join(HLS_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Segment not ready yet — stream may still be starting")
    if filename.endswith(".m3u8"):
        media_type = "application/vnd.apple.mpegurl"
    elif filename.endswith(".ts"):
        media_type = "video/MP2T"
    else:
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type=media_type)


_VIEWER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>QoEScope — Live Stream</title>
  <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
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
    main {
      padding: 24px;
      max-width: 1280px;
      margin: 0 auto;
    }
    video {
      width: 100%;
      background: #000;
      border: 1px solid #2a2a4a;
      border-radius: 6px;
      display: block;
    }
    #info {
      margin-top: 10px;
      font-size: 0.8rem;
      color: #666;
    }
    #error-msg {
      margin-top: 10px;
      color: #ef5350;
      font-size: 0.85rem;
      display: none;
    }
  </style>
</head>
<body>
  <header>
    <div id="dot"></div>
    <h1>Qoe<span>Scope</span> &mdash; Live Received Video</h1>
  </header>
  <main>
    <video id="video" controls autoplay muted playsinline></video>
    <div id="info">Waiting for stream to start&hellip;</div>
    <div id="error-msg"></div>
  </main>

  <script>
    const video   = document.getElementById('video');
    const dot     = document.getElementById('dot');
    const info    = document.getElementById('info');
    const errDiv  = document.getElementById('error-msg');
    const src     = '/hls/stream.m3u8';

    function setLive(on) {
      dot.className = on ? 'live' : '';
      info.textContent = on ? 'Stream live' : 'Reconnecting…';
    }

    function tryLoad() {
      errDiv.style.display = 'none';

      if (Hls.isSupported()) {
        const hls = new Hls({
          lowLatencyMode: true,
          liveSyncDurationCount: 1,
          liveMaxLatencyDurationCount: 2,
          maxBufferLength: 2,
        });
        hls.loadSource(src);
        hls.attachMedia(video);

        hls.on(Hls.Events.MANIFEST_PARSED, () => {
          setLive(true);
          video.play().catch(() => {});
        });

        hls.on(Hls.Events.ERROR, (_, data) => {
          if (data.fatal) {
            setLive(false);
            setTimeout(() => { hls.destroy(); tryLoad(); }, 3000);
          }
        });

      } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = src;
        video.play().catch(() => {});
        setLive(true);
        info.textContent += ' (native HLS)';

      } else {
        errDiv.textContent = 'HLS playback is not supported in this browser.';
        errDiv.style.display = 'block';
      }
    }

    tryLoad();
  </script>
</body>
</html>"""


@app.get("/viewer", response_class=HTMLResponse)
def viewer():
    return _VIEWER_HTML
