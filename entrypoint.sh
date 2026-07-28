#!/bin/bash
set -e

# Set jemalloc as the allocator using the arch-safe path from dpkg.
_JEMALLOC=$(dpkg -L libjemalloc2 2>/dev/null | grep '\.so\.2$' | head -1)
if [ -n "$_JEMALLOC" ]; then
    export LD_PRELOAD="$_JEMALLOC"
    echo "jemalloc: $_JEMALLOC"
else
    echo "warning: libjemalloc2 not found, running with default allocator"
fi

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
