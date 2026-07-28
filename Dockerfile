FROM python:3.11-slim

ENV QT_QPA_PLATFORM=offscreen
ENV QT_LOGGING_RULES="*.debug=false"
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg netcat-openbsd \
    libglib2.0-0 libgl1 libegl1 libdbus-1-3 libxkbcommon0 \
    libjemalloc2 \
    && rm -rf /var/lib/apt/lists/*

# LD_PRELOAD is set at runtime by entrypoint.sh using dpkg to find the
# arch-correct path (x86_64 vs aarch64). MALLOC_CONF tells jemalloc to
# run a background thread that returns dirty pages to the OS on a 5s decay.
ENV MALLOC_CONF=background_thread:true,dirty_decay_ms:5000,muzzy_decay_ms:5000

RUN pip install --no-cache-dir gamesentenceminer==2026.7.1 rapidfuzz faster-whisper

WORKDIR /app
COPY . .
RUN chmod +x entrypoint.sh

EXPOSE 7275

CMD ["./entrypoint.sh"]
