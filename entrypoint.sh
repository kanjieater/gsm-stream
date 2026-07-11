#!/bin/bash
set -e

OCR_ENGINE="${OCR_ENGINE:-glens}"
echo "Starting owocr with engine: $OCR_ENGINE"

python -m GameSentenceMiner.owocr.owocr \
  --read_from websocket \
  --write_to websocket \
  --engine "$OCR_ENGINE" &

echo "Waiting for owocr WebSocket on :7331..."
until nc -z 127.0.0.1 7331 2>/dev/null; do
  sleep 1
done
echo "owocr ready — starting bridge"

exec python bridge.py
