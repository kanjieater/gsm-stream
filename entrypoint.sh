#!/bin/bash
set -e

# owocr runs glens for the second pass — always glens regardless of OCR_ENGINE
echo "Starting owocr (glens) for second-pass OCR on :7331"

python -m GameSentenceMiner.owocr.owocr \
  --read_from websocket \
  --write_to websocket \
  --engine glens &

echo "Waiting for owocr WebSocket on :7331..."
until nc -z 127.0.0.1 7331 2>/dev/null; do
  sleep 1
done
echo "owocr ready — starting bridge"

exec python bridge.py
