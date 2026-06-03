# QoEScope - Real-Time Video QoE Monitoring

## Project Abstract

QoEScope is a passive network probe that monitors the Quality of Experience (QoE) of live RTP/H.264 video streams in real time. It captures packets at the kernel level using eBPF/XDP, decodes the H.264 bitstream, scores each frame with the no-reference BRISQUE metric, and streams all measurements into InfluxDB for live Grafana dashboards, all with zero modification to the sender or network.

## Thesis Information

| Field | Value |
|---|---|
| Title | A Framework for Measuring Quality of Experience (QoE) in Live Video Streams |
| Author | Abaidullah Asif |
| University | Eötvös Loránd University |
| Year | 2026 |

## Project Structure

```
qoescope/
├── probe/          # eBPF/XDP kernel program + FastAPI metrics endpoint
├── analyzer/       # RTP → H.264 decoder + BRISQUE scoring API
├── aggregator/     # Metrics poller → InfluxDB writer
├── sender/         # Test video sender (RTP stream generator)
├── grafana/        # Provisioned Grafana dashboards and datasources
├── tests/          # Unit and integration test suite
│   ├── unit/
│   ├── integration/
│   └── fixtures/
└── docker-compose.yml
```

## Architecture Overview

QoEScope follows a pipeline architecture with six containerised services:

```
[Sender] ──RTP/UDP──► [Probe (eBPF/XDP)] ──metrics──► [Aggregator] ──► [InfluxDB] ──► [Grafana]
                               |                             ▲
                               ▼                             | 
                       [Analyzer (BRISQUE)] ──scores─────────┘
```

- **Probe** - attaches an XDP program to the network interface, counts RTP packets/bytes, parses sequence numbers, and exposes a FastAPI metrics endpoint.
- **Analyzer** - receives the RTP stream, reassembles NAL units, decodes H.264 frames with libavcodec (PyAV), and scores each frame with BRISQUE via OpenCV. Exposes a FastAPI metrics endpoint.
- **Aggregator** - polls both endpoints every second, merges the metrics, and writes them to InfluxDB.
- **InfluxDB** - time-series storage for all QoE measurements.
- **Grafana** - live dashboard provisioned automatically on startup.
- **Sender** - feeds a test video file as an RTP stream for development and testing.

## Technologies

### Probe
- C (eBPF/XDP — Linux kernel 5.15+)
- Python 3.11+
- FastAPI 0.135 · Uvicorn · Pydantic

### Analyzer
- Python 3.11+
- PyAV 17 (libavcodec/FFmpeg) — H.264 decoding
- OpenCV 4.13 (opencv-contrib) — BRISQUE scoring
- NumPy 2.2
- FastAPI 0.135 · Uvicorn

### Aggregator
- Python 3.11+
- influxdb-client 1.50
- httpx 0.28
- FastAPI · Uvicorn

### Infrastructure
- Docker Compose
- InfluxDB 2.7
- Grafana 12.4.1
- NVIDIA Container Runtime

## Prerequisites

- Docker Engine 24+ and Docker Compose v2
- Linux host with kernel **5.15+** (required for XDP)
- NVIDIA GPU + [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) (required for the analyzer)
- A `.env` file in the project root (see below)

## Environment Setup

Create a `.env` file in the project root:

```env
INFLUXDB_TOKEN=your_influxdb_token_here
INFLUXDB_PASSWORD=your_influxdb_password_here
GRAFANA_PASSWORD=your_grafana_password_here
```

## Running the Stack

Place a video file in the `./videos/` folder, then start all services:

```bash
docker compose up --build
```

| Service | URL |
|---|---|
| Probe metrics API | http://localhost:8000/metrics |
| Analyzer metrics API | http://localhost:9101/metrics |
| Aggregator API | http://localhost:9000 |
| InfluxDB | http://localhost:8086 |
| Grafana dashboard | http://localhost:3000 |

Grafana credentials: `admin` / value of `GRAFANA_PASSWORD` in `.env`.

## Testing

The test suite covers both unit and integration scenarios.

### Unit Tests

Unit tests validate individual components in isolation:

- RTP packet parsing and sequence-number tracking
- NAL unit assembly from RTP payloads
- H.264 frame type detection (I/P/B frames)
- Frame counter logic
- Metrics aggregator calculations
- Probe FastAPI endpoint responses

### Integration Tests

Integration tests verify end-to-end data flows:

- Full RTP-stream-to-decoded-frames pipeline
- Forwarder-to-decoder handoff
- Metrics API response under simulated load

### Running the Tests

Install test dependencies and run from the project root:

```bash
pip install pytest pytest-asyncio fastapi httpx
pytest tests/
```
## Research Methodology

This project implements findings from research into passive QoE monitoring for live video delivery. The probe design is based on literature covering eBPF/XDP packet processing, no-reference video quality metrics (BRISQUE), and RTP/H.264 stream analysis. All design decisions are documented in the accompanying thesis.

## License

This project is part of a thesis research work.
