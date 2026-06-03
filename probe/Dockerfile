FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# System dependencies
RUN apt-get update && apt-get install -y \
    clang \
    llvm \
    gcc \
    make \
    pkg-config \
    libc6-dev \
    linux-libc-dev \
    libbpf-dev \
    libelf-dev \
    zlib1g-dev \
    iproute2 \
    tcpdump \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Python packages
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy everything into /data and set permissions
WORKDIR /data
COPY . .

RUN chmod +x /data/run_xdp.sh /data/entrypoint.sh

# One line to run everything
ENTRYPOINT ["/data/entrypoint.sh"]