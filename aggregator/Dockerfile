# aggregator/Dockerfile
# Mirrors your original setup exactly:
#   Base image : python:3.11-slim
#   Port       : 9000
#   Runs       : uvicorn backend:app --host 0.0.0.0 --port 9000

FROM python:3.11-slim

# ── Python packages ───────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Copy source files ─────────────────────────────────────────
WORKDIR /app
COPY . .

RUN chmod +x /app/entrypoint.sh

# ── One line to run everything ────────────────────────────────
ENTRYPOINT ["/app/entrypoint.sh"]