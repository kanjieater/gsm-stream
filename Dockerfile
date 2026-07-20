FROM python:3.11-slim

ENV QT_QPA_PLATFORM=offscreen
ENV QT_LOGGING_RULES="*.debug=false"
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg netcat-openbsd \
    libglib2.0-0 libgl1 libegl1 libdbus-1-3 libxkbcommon0 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir gamesentenceminer==2026.7.1 rapidfuzz

WORKDIR /app
COPY . .
RUN chmod +x entrypoint.sh

EXPOSE 7275

CMD ["./entrypoint.sh"]
