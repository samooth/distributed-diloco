FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps (Node.js optional for bitbeam/identity sidecar)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Optional: install Node.js LTS for bitbeam tunnels/identity sidecar
# Uncomment if you need --p2p-topic / identity tunnels inside the container.
# RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
#     && apt-get install -y --no-install-recommends nodejs \
#     && rm -rf /var/lib/apt/lists/*

# Python dependencies (CPU torch + safetensors + numpy for tensor hashing/utils).
# For GPU/HF, install extras in a derived image.
RUN pip install --upgrade pip \
    && pip install "torch>=2.8,<3" "safetensors>=0.5" "numpy>=1.26"

# Copy project
COPY . /app

# If you enabled Node above and need sidecar deps:
# RUN cd /app/network/holepunch-sidecar && npm install --omit=dev

# Default ports (coordinator 8095, agent 8765)
EXPOSE 8095 8765

# Default command: print help. Override in docker-compose.
CMD ["python", "scripts/run_coordinator.py", "--help"]
