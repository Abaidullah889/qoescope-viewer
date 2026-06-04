# QoEScope — Live Received-Video Feature

Complete reference for the live video viewer: what it is, how it works end to end,
every design decision and the reason behind it, configuration, and troubleshooting.

---

## 1. What this feature is

A browser page that shows the **received** video — the picture the analyzer
reconstructs from the RTP/H.264 packets captured off the wire — so a user can watch
the video and the QoE metrics **at the same time**. When the metrics degrade (packet
loss, incomplete frames), the picture visibly degrades too, so the two correlate.

- **Viewer page:** `http://<host>:9101/viewer`
- **Video stream (lossless multipart image stream):** `http://<host>:9102/video.mjpg`
- **Metrics (JSON):** `http://<host>:9101/metrics`

The feature lives entirely in the `analyzer` service. Nothing in `probe`, `sender`,
`aggregator`, or `grafana` was changed for it.

---

## 2. Requirements it was built to satisfy

1. **Start from the beginning** of each stream — no skipped first seconds.
2. **No freeze/stutter** as a player bug.
3. **Stay close to live** so the video lines up in time with the 1-second Grafana metrics.
4. **Show the damage** — when packets are lost / frames are incomplete, the picture must
   visibly degrade, because it is the *same* decoded frame that BRISQUE scores.

---

## 3. Why the old HLS approach was replaced

The previous implementation pushed the received RTP to a local port, ran
`ffmpeg -i stream.sdp -c:v copy -f hls` to cut `.ts`/`.m3u8` segments, and played them
with hls.js. It had three structural problems that map directly to the reported symptoms:

| Symptom | Root cause in HLS approach |
|---|---|
| Skips first 3–4 s | HLS needs several buffered segments before playback; the player jumps to a live edge / seekable start, and segment-clearing on restart races with playback. Fighting this is why the git history has ~10 "start from first frame" commits. |
| Starts then freezes/stutters | `-c:v copy` forwards the **corrupt** bitstream straight to the browser's decoder, which **stalls** on damaged frames instead of showing concealment. |
| Lags behind metrics | HLS has a ~2–4 s latency floor (segment duration × buffered segments), so it can never track 1-second Grafana metrics. |

HLS also could not really "show the damage": a browser video element tends to freeze or
drop on a broken stream rather than render the decoder's concealment.

**Alternatives considered and rejected:**

- **Tune HLS further** — can't beat the latency floor or the copy-mode stall. Rejected.
- **Re-encode to low-latency fragmented MP4 over MSE** — re-encoding *cleans up* the
  concealment artifacts, hiding the damage (against requirement 4), and adds CPU and
  keyframe-startup complexity. Rejected.
- **WebRTC** — lowest latency, but heavy (signalling, ICE, SFU-like plumbing) and still
  re-encodes/cleans the picture. Overkill for a localhost QoE monitor. Rejected.

---

## 4. The chosen approach (and why it fits)

**Stream the analyzer's own decoded frames as a lossless multipart image stream.**

The analyzer **already decodes every received frame** in `decode_worker()` (it needs the
pixels for BRISQUE). We reuse that exact image, encode it **losslessly to PNG at native
resolution** (no resampling, no lossy compression), and stream it to the browser as
`multipart/x-mixed-replace` (an MJPEG-style stream), displayed in a plain `<img>` tag.
The picture shown is therefore pixel-for-pixel the decoded (received) frame — nothing in
the display path drops quality.

Why this satisfies all four requirements:

- **Faithful by construction (req 4):** the displayed frame *is* the frame BRISQUE scores.
  A lost I-frame makes decode fail → no new frame → the `<img>` holds the last good frame
  (a visible freeze that *is* the QoE drop), and concealment artifacts on partial frames
  are shown as-is.
- **Low latency (req 3):** no segmenting, no buffering, no ffmpeg copy — sub-second glass
  to glass, so it tracks the metrics closely.
- **Starts from the first decoded frame (req 1):** no seek/skip logic, no buffered
  player — frames flow from the moment decode produces them.
- **No player-stall freeze (req 2):** the browser only ever paints whole images; it never
  chokes on a corrupt H.264 bitstream.

**Transport: MJPEG vs WebSocket+canvas.** MJPEG was chosen because it is browser-native
(`<img>`, almost no JS), lowest latency, and naturally drop-to-newest. The per-frame
metric overlay is handled separately by an HTML div that polls `/metrics`, keeping the
video transport trivial.

---

## 5. Architecture & data flow

```
sender ──RTP/H.264/UDP──► probe (XDP counts) ──► forwarder.py ──► analyzer:5004
                                                                      │
                                                            decoder.py (recv_worker)
                                                                      │  recv_queue
                                                            RTP parse + frame assembly
                                                                      │  frame_queue
                                                              decode_worker (PyAV)
                                                                      │
                            ┌─────────────────────────────────────────┴───────────────┐
                            │ (same decoded ndarray)                                    │
                            ▼                                                           ▼
                    display_queue (maxsize=2, drop-oldest)                      brisque_queue
                            │                                                           │
                    display_worker                                            brisque_worker ×4
                    (cv2.imencode PNG, lossless, native resolution)           (cv2 BRISQUE compute)
                            │                                                           │
                    LatestFrame holder (bytes + version + Condition)          _brisque_window
                            │                                                           │
                    ThreadingHTTPServer :9102                                _metrics_aggregator (1 Hz)
                    GET /video.mjpg                                                      │
                            │                                            brisque_metrics.json
                            │                                                           │
   browser <img> ◄─────────┘                                  brisque_api.py :9101 GET /metrics
   (viewer :9101/viewer)                                                     │
        └── overlay div polls /metrics every 1 s ◄───────────────────────────┘
```

Two processes run inside the `analyzer` container (unchanged process split):

- **`decoder.py`** — owns the decode pipeline **and** the frames, so it also serves the
  MJPEG stream (port 9102). Started by `entrypoint.sh`.
- **`brisque_api.py`** (uvicorn, port 9101) — serves the viewer page and `/metrics`.

The MJPEG server lives in `decoder.py` (not the API) because that is the process that has
the decoded frames in memory; bridging them to the API process would be needless IPC.

---

## 6. Component details

### 6.1 `decoder.py` — the frame tap

**Configuration (top of file):**

| Constant | Value | Purpose |
|---|---|---|
| `DISPLAY_HTTP_PORT` | `9102` | Port the image server binds (exposed in docker-compose). |
| `DISPLAY_PNG_COMPRESSION` | `1` | PNG zlib level 0–9. **Lossless at every level**; only trades encode speed vs. size (1 = fast). |
| `display_queue` | `maxsize=2` | Holds decoded ndarrays awaiting encode; **drop-oldest**. |

**Frame capture — inside `decode_worker()`:**
The first decoded frame of each packet is converted once to a BGR ndarray and reused for
both display and (when measuring) BRISQUE:

```python
for frame in frames:
    display_img = frame.to_ndarray(format="bgr24")
    if should_measure:
        decoded_img = display_img   # same array → what you see is what is scored
    break
```

After a successful decode, the frame is pushed to `display_queue` with drop-oldest
semantics. **On decode failure we never reach this push**, so the viewer keeps showing
the last good frame — the intended "freeze = damage" behavior. The PNG encoding happens
on a *separate* thread, so decode speed is unaffected.

**`LatestFrame` holder:**
A small thread-safe class holding the most recent encoded-image (PNG) bytes plus:
- a monotonically increasing **version** counter,
- a `threading.Condition` so MJPEG clients block until a newer frame exists
  (`wait_newer(last_version, timeout)`), and
- a **client counter** (`add_client` / `remove_client` / `client_count`) so the encoder
  can skip all work when nobody is watching.

Because clients wait on the version counter, a slow client simply jumps to the newest
frame on its next wake — **no per-client lag accumulates**.

**`display_worker` thread:**
Pulls from `display_queue` and **losslessly PNG-encodes each frame as soon as it arrives**
— native resolution, no resampling, no lossy compression — returning early if
`client_count() == 0`, via
`cv2.imencode(".png", img, [IMWRITE_PNG_COMPRESSION, DISPLAY_PNG_COMPRESSION])`, then stores
the bytes via `latest_frame.set(...)`. The published image is pixel-for-pixel the decoded
frame.

**`_MJPEGHandler` + `display_http_server` (ThreadingHTTPServer on 9102):**
- `GET /video.mjpg` → responds `multipart/x-mixed-replace; boundary=frame`, then loops:
  `wait_newer()` → write `--frame`, `Content-Type: image/png`, `Content-Length`, the PNG
  bytes. Increments the client count on connect, decrements it in a `finally` on
  disconnect (broken pipe / reset are swallowed cleanly).
- `GET /health` → `200 ok`.
- `Access-Control-Allow-Origin: *` and `Cache-Control: no-store` are set.

**Threads started at module load:** `recv` + `decode` + `log-ordering` + `metrics` +
`display` + `display_http_server` + `BRISQUE_WORKERS` (4) BRISQUE workers.

**Removed (the entire HLS subsystem):** `hls_writer()` (ffmpeg), the SDP file write, the
`_hls_fwd_sock` forward in `recv_worker()`, `_clear_hls_segments()`, `_hls_restart_event`,
`_write_stream_id()` and its calls, and the `HLS_DIR` / `SDP_FILE` / `HLS_RTP_PORT`
constants. The `subprocess` import was dropped. The metrics JSON path/dir is unchanged.

### 6.2 `brisque_api.py` — the viewer page

- **`GET /viewer`** returns a self-contained HTML page. The video is a single
  `<img id="video">` whose `src` is built in JS as
  `http://<location.hostname>:9102/video.mjpg?t=<timestamp>`. Using `location.hostname`
  means it works from whatever host the page was opened on; the `?t=` cache-buster forces
  a fresh stream on reconnect.
- An **overlay div** shows `BRISQUE avg`, `BRISQUE last`, `Incomplete /s`, `Decode err /s`,
  polled from `/metrics` every second. Incomplete-% and decode-error values turn amber/red
  past warn/bad thresholds.
- **Liveness (the green dot)** is driven by **metric freshness** (`m.stale`), not by the
  `<img>` load event — because browsers fire `load` inconsistently for MJPEG. Fresh
  metrics mean the decoder is actively processing packets, i.e. the stream is live.
- On `<img>` `error` (server restart / stream drop) it reconnects after 1.5 s with a fresh
  URL.
- **Removed routes:** `/hls/{filename}`, `/hls.js`, `/stream-version`. Kept `/metrics`,
  `/health`, `/viewer`. The `DISPLAY_HTTP_PORT` constant is injected into the page via a
  `__MJPEG_PORT__` placeholder replacement so the port has a single source of truth.

### 6.3 `docker-compose.yml`

The `analyzer` service now exposes **both** ports:

```yaml
ports:
  - "9101:9101"   # metrics API + viewer page
  - "9102:9102"   # MJPEG received-video stream
```

### 6.4 Deleted file

`analyzer/hls.min.js` (the bundled hls.js library) is no longer needed.

---

## 7. BRISQUE values — are they correct?

Yes, and they now describe the exact frame on screen:

- The frame shown and the frame scored are the **same ndarray** (§6.1), so there is no
  "video shows one thing, score measures another" mismatch.
- `brisque_worker()` runs `cv2.quality.QualityBRISQUE.compute(img)` against
  `brisque_model_live.yml` / `brisque_range_live.yml`, rounds to 2 decimals, and that
  feeds the per-second `brisque_metrics.json` the overlay reads.
- That JSON is the **same source** the `aggregator` writes to InfluxDB, so the overlay and
  Grafana show identical numbers. The MJPEG change did **not** touch the scoring math.

How to read them:

- **Lower = better.** BRISQUE is a no-reference distortion score (~0 clean, higher = more
  distortion). Absolute values depend on the custom `_live` model, so treat them as
  relative QoE, not an absolute "out of 100."
- **`BRISQUE avg`** is the clean per-second mean of valid scores — trust this for
  correlation. **`BRISQUE last`** is whichever frame *finished* most recently across the
  4 parallel workers, so it can be slightly out of frame order (a "latest sample").
- **Degraded frames are scored on purpose**, so the score worsens exactly when the picture
  breaks up. Frames that fail to decode are counted in **`decode err/s`**, not as a BRISQUE
  value.

---

## 8. How to run and view

1. **Bring up the stack** (Linux/WSL2 host; needs XDP + NVIDIA):
   ```bash
   docker compose up --build
   ```
   Wait for the analyzer log line `MJPEG display server on 0.0.0.0:9102`.

2. **Start a stream** (the `sender` container has ffmpeg but does not auto-stream):
   ```bash
   docker exec -it sender ffmpeg -re -stream_loop -1 -i /data/input.mp4 \
     -an -c:v libx264 -profile:v baseline -tune zerolatency \
     -f rtp rtp://probe:5004
   ```
   Any RTP/H.264 feed to `probe:5004` works; this is just a convenient default.

3. **Open the viewer:** `http://localhost:9101/viewer`. Watch Grafana at
   `http://localhost:3000` alongside it for the metrics.

---

## 9. Configuration / tuning

All in `analyzer/decoder.py`:

- The display is **lossless by design** — frames are delivered at native resolution as
  PNG. `DISPLAY_PNG_COMPRESSION` only trades encode speed vs. size; raise it (toward 9)
  for smaller frames at higher CPU cost, lower it (toward 0) for faster encoding. Quality
  is unaffected at any level.
- Every decoded frame is encoded as it arrives (no frame-rate cap), so the display
  frame rate equals the received stream's frame rate.
- **Different port:** change `DISPLAY_HTTP_PORT` and the matching `docker-compose.yml`
  ports entry; the viewer reads the port automatically via the `__MJPEG_PORT__` injection.

---

## 10. Troubleshooting

| Check | Command | Healthy result |
|---|---|---|
| Analyzer API up | `curl http://localhost:9101/health` | `{"status":"ok"}` |
| MJPEG server up | `curl http://localhost:9102/health` | `ok` |
| Metrics flowing | `curl http://localhost:9101/metrics` | JSON (not `503`) |

- **Page loads but video is black, overlay shows numbers** → port 9102 not reachable from
  the browser host; confirm the compose port mapping and any firewall.
- **`/metrics` returns 503** → no packets are arriving; recheck the sender (step 2 above).
- **Overlay dot stays red** → metrics are stale (older than 5 s) — the decoder isn't
  receiving/processing packets.

---

## 11. Trade-offs & limitations

- **No audio.** The stream is video-only; irrelevant for video-QoE.
- **High bandwidth** (a lossless PNG per frame, native resolution). This is the cost of
  delivering the picture exactly as received; fine on localhost. The only saving applied
  is "skip encoding when no client is connected." If bandwidth/CPU ever becomes a problem,
  switching the encode to JPEG would shrink it — but that would re-introduce lossy
  compression, so it is deliberately not done.
- **`BRISQUE last`** can be marginally out of frame order due to parallel workers; use
  `BRISQUE avg` for precise correlation.
- **Per-second metric granularity** in the overlay vs. near-live video means correlation
  is accurate to ~1 s — the same granularity as Grafana.

## 12. Possible future improvements

- WebSocket + `<canvas>` transport to stamp each frame with its exact per-frame metric
  (frame type, incomplete flag) for sub-second per-frame correlation.
- A `sender/entrypoint.sh` or `make stream` helper so the ffmpeg command isn't typed by
  hand each run.
- Optional adaptive FPS/quality based on connected-client count or frame rate.
