# gsm-stream Agent Reference

## Purpose
Docker container that bridges Nintendo Switch gameplay (SysDVR RTSP) → OCR → GSM texthooker web UI for Japanese immersion/Anki mining.

## Core Principle: Do Things the GSM Way
**Never reimplement GSM's pipeline.** Always hook into GSM's own APIs and infrastructure:
- Use `TwoPassOCRControllerV2` from `GameSentenceMiner.ocr.gsm_ocr` for stabilization
- Use `gametext.handle_new_text_event()` as the single text intake entry point
- Use `GSM_ELECTRON=1` to run GSM headlessly (skips pystray/OBS)
- If something GSM does natively, find how to call it — don't rewrite it

## Two-Pass OCR Architecture
GSM's intended two-pass design:
- **Pass 1 (fast, local):** oneocr or screenai — stability detection across frames
- **Pass 2 (accurate, cloud):** Google Lens — runs once on stable frame for final output

**Linux/Docker constraint:** OneOCR is Windows-only (WinDLL). On Linux the recommended first-pass engine is not available. Current workaround: single-pass Google Lens with the controller's stability gate (2 matching frames required before emit). This is suboptimal — investigate ScreenAI or other Linux-compatible local engines as a first-pass option.

## Key Engine Notes
- Google Lens: best quality, cloud, works everywhere
- OneOCR: Windows-only, fast, recommended first-pass — **not usable in Docker on Linux**
- ScreenAI: local, may work on Linux (unconfirmed), alternative first-pass candidate
- Do NOT use manga-ocr — it's manga-specific, not tuned for game text

## Files
- `bridge.py` — main bridge: RTSP → owocr WebSocket → controller → gametext
- `Dockerfile` — python:3.11-slim + ffmpeg + gamesentenceminer
- `entrypoint.sh` — starts owocr subprocess then bridge.py
- `compose.yml` at `/mnt/srv/gsm-stream/compose.yml`

## Architecture
```
Switch RTSP :6666
  → ffmpeg (subprocess) → JPEG frames
  → _ocr_busy gate (1 frame in-flight at a time, prevents 30s backlog)
  → owocr WebSocket :7331 (--engine glens subprocess)
  → TwoPassOCRControllerV2.handle_ocr_result() (2 stable frames → flush)
  → _send callback → asyncio.run_coroutine_threadsafe → gametext.handle_new_text_event()
  → GSM texthooker WebSocket clients at :7275 (gsm.<your-domain> via NPM)
```

## Key Implementation Details
- `GSM_ELECTRON=1` must be set before any GSM imports — skips pystray/OBS
- Import `GameSentenceMiner.gametext` BEFORE calling `start_web_server()` — gametext starts the internal multiplex WS server that the web server proxies to
- `get_config().advanced.localhost_bind_address = "0.0.0.0"` before `start_web_server()` — otherwise binds to 127.0.0.1 only
- Override `/get_websocket_port` Flask route to return 0 when `X-Forwarded-Proto: https` — otherwise Svelte app tries `wss://gsm.<your-domain>:7275` and bypasses NPM
- `_ocr_busy` flag: set True before `ws.send(jpeg)`, cleared to False in `receive_loop` when OCR text result arrives (not on True/False ACKs)
- Controller `_send` callback uses `asyncio.run_coroutine_threadsafe(coro, _bridge_loop)` to schedule `handle_new_text_event` onto the bridge's event loop

## Deploy
```bash
cd /mnt/srv/gsm-stream
docker compose build && docker compose up -d
docker compose logs -f
```

## GitHub
Private repo: `kanjieater/gsm-stream`
