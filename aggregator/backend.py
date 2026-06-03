import os
import asyncio
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from influxdb_client import InfluxDBClient, Point, WritePrecision

PROBE_URL = os.environ.get("PROBE_URL", "http://probe:8000/metrics")
BRISQUE_URL = os.environ.get("BRISQUE_URL", "http://analyzer:9101/metrics")
POLL_INTERVAL_S = float(os.environ.get("POLL_INTERVAL_S", "1.0"))

INFLUX_URL = os.environ.get("INFLUX_URL", "http://influxdb:8086")
INFLUX_TOKEN = os.environ.get("INFLUX_TOKEN", "")
INFLUX_ORG = os.environ.get("INFLUX_ORG", "qoe-org")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET", "qoe")

MEASUREMENT = os.environ.get("MEASUREMENT", "qoe_metrics")
BRISQUE_MEAS = os.environ.get("BRISQUE_MEAS", "brisque_metrics")
STREAM_ID = os.environ.get("STREAM_ID", "stream1")
SOURCE = os.environ.get("SOURCE", "probe")

influx_client = None
write_api = None


@asynccontextmanager
async def lifespan(application):
    global influx_client, write_api

    if not INFLUX_TOKEN:
        raise RuntimeError("INFLUX_TOKEN is empty. Set INFLUX_TOKEN env var.")

    influx_client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    write_api = influx_client.write_api()

    asyncio.create_task(poll_probe())
    asyncio.create_task(poll_brisque())

    yield

    if influx_client:
        try:
            influx_client.close()
        except Exception as e:
            print(f"[{_now()}] [backend] failed to close InfluxDB client: {e}")


app = FastAPI(title="Backend: Poll Probe + BRISQUE -> InfluxDB", lifespan=lifespan)


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def build_probe_point(data: dict) -> Point:
    ts_sec = data.get("timestamp_sec")
    if ts_sec is None:
        ts_sec = int(time.time())
    ts_ns = int(ts_sec) * 1_000_000_000

    bps  = float(data.get("throughput_bps",  0.0))
    mbps = float(data.get("throughput_mbps", 0.0))
    if mbps == 0.0 and bps != 0.0:
        mbps = bps / 1_000_000.0
    if bps == 0.0 and mbps != 0.0:
        bps = mbps * 1_000_000.0

    return (
        Point(MEASUREMENT)
        .tag("stream_id", STREAM_ID)
        .tag("source", SOURCE)
        .field("throughput_bps",  bps)
        .field("throughput_mbps", mbps)
        .field("pkts_per_s",      int(data.get("pkts_per_s",  0)))
        .field("bytes_per_s",     int(data.get("bytes_per_s", 0)))
        .field("frames_total",    int(data.get("frames_total",   0)))
        .field("frames_per_s",    int(data.get("frames_per_s",   0)))
        .field("i_frames_total",  int(data.get("i_frames_total", 0)))
        .field("i_frames_per_s",  int(data.get("i_frames_per_s", 0)))
        .field("p_frames_total",  int(data.get("p_frames_total", 0)))
        .field("p_frames_per_s",  int(data.get("p_frames_per_s", 0)))
        .field("incomplete_frames_total",   int(data.get("incomplete_frames_total",   0)))
        .field("incomplete_frames_per_s",   int(data.get("incomplete_frames_per_s",   0)))
        .field("incomplete_i_frames_total", int(data.get("incomplete_i_frames_total", 0)))
        .field("incomplete_i_frames_per_s", int(data.get("incomplete_i_frames_per_s", 0)))
        .field("incomplete_p_frames_total", int(data.get("incomplete_p_frames_total", 0)))
        .field("incomplete_p_frames_per_s", int(data.get("incomplete_p_frames_per_s", 0)))
        .field("received_packets_total", int(data.get("received_packets_total", 0)))
        .field("expected_packets_total", int(data.get("expected_packets_total", 0)))
        .field("lost_packets_total",     int(data.get("lost_packets_total",     0)))
        .field("pkt_loss_pct",           float(data.get("pkt_loss_pct",         0.0)))
        .field("pkt_loss_pct_per_s",     float(data.get("pkt_loss_pct_per_s",   0.0)))
        .time(ts_ns, WritePrecision.NS)
    )


def build_brisque_point(data: dict) -> Point | None:
    errors = int(data.get("decode_errors_per_s", 0))
    has_scores = data.get("brisque_avg") is not None
    inc_pct = float(data.get("incomplete_pct_per_s", 0.0))

    if not has_scores and errors == 0 and inc_pct == 0.0:
        return None

    ts_ns = int(data.get("timestamp_sec", time.time())) * 1_000_000_000

    p = (
        Point(BRISQUE_MEAS)
        .tag("stream_id", STREAM_ID)
        .tag("source", "content")
        .field("decode_errors_per_s",  errors)
        .field("incomplete_pct_per_s", inc_pct)
        .field("brisque_last",         float(data.get("brisque_last", 0.0)))
        .time(ts_ns, WritePrecision.NS)
    )

    if has_scores:
        p = (p
             .field("brisque_avg", float(data["brisque_avg"]))
             .field("brisque_min", float(data["brisque_min"]))
             .field("brisque_max", float(data["brisque_max"]))
        )

    return p


async def poll_probe():
    timeout = httpx.Timeout(0.6)
    async with httpx.AsyncClient(timeout=timeout) as client:
        backoff = 1.0
        first_ok = False

        print(f"[backend] Polling probe {PROBE_URL} every {POLL_INTERVAL_S}s")

        while True:
            start = time.time()
            try:
                r = await client.get(PROBE_URL)

                if r.status_code >= 400:
                    try:
                        msg = r.json()
                    except ValueError:
                        msg = r.text.strip()
                    print(f"[{_now()}] [probe] not ready (HTTP {r.status_code}). {msg}")
                    backoff = min(backoff * 1.5, 5.0)

                else:
                    data = r.json()
                    point = build_probe_point(data)
                    write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=point)

                    print(
                        f"[{_now()}] [probe] ok "
                        f"mbps={data.get('throughput_mbps', 0):.3f} "
                        f"frames/s={data.get('frames_per_s')} "
                        f"loss%={data.get('pkt_loss_pct', 0):.2f}"
                    )

                    if not first_ok:
                        print(f"[backend] Probe reachable: {PROBE_URL}")
                        first_ok = True
                    backoff = 1.0

            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
                print(f"[{_now()}] [probe] unreachable, retrying...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 5.0)

            except Exception as e:
                print(f"[{_now()}] [probe] error: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 5.0)

            await asyncio.sleep(max(0.0, POLL_INTERVAL_S - (time.time() - start)))


async def poll_brisque():
    timeout = httpx.Timeout(0.6)
    async with httpx.AsyncClient(timeout=timeout) as client:
        backoff = 1.0
        first_ok = False

        print(f"[backend] Polling BRISQUE {BRISQUE_URL} every {POLL_INTERVAL_S}s")

        while True:
            start = time.time()
            try:
                r = await client.get(BRISQUE_URL)

                if r.status_code == 503:
                    # Decoder not yet started — silent retry, no noise
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 1.5, 5.0)
                    continue

                if r.status_code >= 400:
                    print(f"[{_now()}] [brisque] HTTP {r.status_code}")
                    backoff = min(backoff * 1.5, 5.0)

                else:
                    data = r.json()
                    point = build_brisque_point(data)

                    if point is not None:
                        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=point)
                        print(
                            f"[{_now()}] [brisque] ok "
                            f"avg={data.get('brisque_avg')} "
                            f"min={data.get('brisque_min')} "
                            f"max={data.get('brisque_max')} "
                            f"incomplete={data.get('incomplete_pct_per_s', 0):.1f}% "
                            f"errors={data.get('decode_errors_per_s', 0)}"
                        )
                    if not first_ok:
                        print(f"[backend] BRISQUE API reachable: {BRISQUE_URL}")
                        first_ok = True
                    backoff = 1.0

            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
                if first_ok:
                    print(f"[{_now()}] [brisque] unreachable, retrying...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 5.0)

            except Exception as e:
                print(f"[{_now()}] [brisque] error: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 5.0)

            await asyncio.sleep(max(0.0, POLL_INTERVAL_S - (time.time() - start)))


@app.get("/health")
def health():
    return {
        "ok":              True,
        "probe_url":       PROBE_URL,
        "brisque_url":     BRISQUE_URL,
        "poll_interval_s": POLL_INTERVAL_S,
        "influx_url":      INFLUX_URL,
        "bucket":          INFLUX_BUCKET,
        "org":             INFLUX_ORG,
        "measurement":     MEASUREMENT,
        "brisque_meas":    BRISQUE_MEAS,
        "stream_id":       STREAM_ID,
    }
