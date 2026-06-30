FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV DOCKER_ENV=1

# Install dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    wget \
    ca-certificates \
    gcc \
    g++ \
    git \
    curl \
    libffi-dev \
    libssl-dev \
    build-essential \
    coreutils \
    procps \
    && rm -rf /var/lib/apt/lists/*

RUN ffmpeg -version && ffprobe -version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

RUN mkdir -p downloads logs temp

# # Copy Widevine device file (ensure it exists)
COPY l3.wvd /app/l3.wvd

RUN test -f /app/l3.wvd && echo "Widevine OK" || echo "Widevine missing"

# Copy and setup start script
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

# Healthcheck
HEALTHCHECK --interval=30m --timeout=10s --start-period=2m --retries=3 \
    CMD pgrep -f "python.*bot.py" || exit 1

# Run the wrapper script
CMD ["/app/start.sh"]
