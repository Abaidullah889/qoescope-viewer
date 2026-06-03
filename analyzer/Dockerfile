# analyzer/Dockerfile
# Mirrors your original setup exactly:
#   Base image : nvidia/cuda:12.1.0-cudnn8-runtime-ubuntu22.04
#   GPU        : --gpus all --runtime=nvidia
#   apt        : python3, python3-pip, wget, ffmpeg, libgl1, libglib2.0-0
#   pip        : fastapi, uvicorn, av, opencv-contrib-python-headless, brisque
#   BRISQUE    : model files copied from local folder (no download needed)
#   Runs       : decoder.py in background + uvicorn brisque_api:app on port 9101

FROM nvidia/cuda:12.1.0-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

# ── NVIDIA environment variables ──────────────────────────────
ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=compute,video,utility

# ── System dependencies ───────────────────────────────────────
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# ── Python packages ───────────────────────────────────────────
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# ── Copy all source files + BRISQUE model files ───────────────
# Put brisque_model_live.yml and brisque_range_live.yml
# inside your analyzer/ folder alongside decoder.py
# They will be copied to /app automatically by this line
WORKDIR /app
COPY . .

RUN chmod +x /app/entrypoint.sh

# ── One line to run everything ────────────────────────────────
ENTRYPOINT ["/app/entrypoint.sh"]