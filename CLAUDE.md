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
GSM's intended two-pass design, implemented properly:
- **Pass 1 (fast, local):** MeikiOCR via `SharedMeikiOCRModel.get_model()` — runs every frame for stability detection
- **Pass 2 (accurate, cloud):** Google Lens via owocr WS :7331 — runs ONCE on confirmed-stable frame

First pass runs in a thread executor (`loop.run_in_executor`), never blocking the event loop.
Second pass (`_second_ocr` callback) blocks its executor thread with `run_coroutine_threadsafe(_call_glens(...), loop).result(timeout=12)`.
Controller requires 2+ consecutive frames with ≥80% text similarity before triggering the second pass.

## Watermark Handling
owocr inserts `ＢＬＡＮＫ＿ＬＩＮＥ` between dialogue text and watermark regions.
`_strip_owocr_artifacts()` splits on that marker and drops everything after — applied to BOTH first-pass (meikiocr) and second-pass (glens) results before they reach the controller or `SecondPassResult`.

## Key Engine Notes
- MeikiOCR: local, fast, Linux-compatible, game text — **first pass**
  - API: `SharedMeikiOCRModel.get_model().run_ocr(np.array(pil.convert("RGB")), punct_conf_factor=0.2)`
  - Returns `list[{"text": str, "chars": [...]}]`
- Google Lens: best quality, cloud — **second pass only** (rate-limited; only called on stable frames)
- Do NOT use manga-ocr — manga-specific, not tuned for game text
- OneOCR: Windows-only (WinDLL) — not usable in Docker on Linux

## Code Organization
Keep individual Python source files under 300 lines. Split by responsibility when a file approaches that limit. Current modules:
- `identity.py` — game identity detection (runtime override → GAME_NAME env → GSM profile)
- `text_filter.py` — watermark detection, owocr artifact stripping, speaker name splitting (text-only fallback)
- `noise_filter.py` — frequency-based UI noise suppression (meikiocr + glens history, per-game cache)
- `speaker_filter.py` — positional speaker name classifier; learns speaker regions from bbox position + dialogue adjacency
- `ocr.py` — raw meikiocr and glens OCR calls
- `controller.py` — TwoPassOCRControllerV2 wiring, send/second-pass callbacks
- `stream.py` — RTSP ingestion (ffmpeg), per-frame dispatch, replay buffer, `is_stream_active()`
- `bridge.py` — env config, Flask route injection, GSM web server startup, MJPEG debug server, `main()` entry point
- `Dockerfile` — python:3.11-slim + ffmpeg + gamesentenceminer + rapidfuzz
- `entrypoint.sh` — starts owocr subprocess then bridge.py
- `compose.yml` at `/mnt/srv/gsm-stream/compose.yml`

## Architecture
```
Switch RTSP :6666
  → ffmpeg (subprocess) → JPEG frames
  → loop.run_in_executor → _handle_frame_in_thread (no event-loop blocking)
      → meikiocr (local, fast) → text
      → TwoPassOCRControllerV2.handle_ocr_result() (2+ stable frames → triggers pass 2)
          → _second_ocr callback → run_coroutine_threadsafe(_call_glens) → glens result
          → _send callback → run_coroutine_threadsafe → gametext.handle_new_text_event()
  → GSM texthooker WebSocket clients at :7275 (<your-gsm-domain> via NPM)

owocr subprocess (--engine glens) on :7331 — only used for second-pass glens calls
MJPEG debug stream on :7276
```

## Key Implementation Details
- `GSM_ELECTRON=1` must be set before any GSM imports — skips pystray/OBS
- Import `GameSentenceMiner.gametext` BEFORE calling `start_web_server()` — gametext starts the internal multiplex WS server that the web server proxies to
- `get_config().advanced.localhost_bind_address = "0.0.0.0"` before `start_web_server()` — otherwise binds to 127.0.0.1 only
- Override `/get_websocket_port` Flask route to return 0 when `X-Forwarded-Proto: https` — otherwise Svelte app tries `wss://<your-gsm-domain>:7275` and bypasses NPM
- `_ctrl_lock` guards `ctrl.handle_ocr_result()` — executor threads can overlap, controller is not thread-safe
- `_second_ocr` is a sync callback called from the executor thread — safe to block with `.result(timeout=12)` since it's not the event loop thread
- `_call_glens` opens a fresh WS connection per second-pass call (avoids shared-state issues with the owocr glens subprocess)

## Deploy
Images are built and pushed to GHCR automatically by GitHub Actions on every
push to main. Locally-built images are no longer used.

```bash
# Pull latest from GHCR and redeploy both containers:
cd /mnt/srv/gsm-stream && ./deploy.sh
docker compose logs -f

# Roll back to a specific SHA (no rebuild needed):
# Edit compose.yml image tag to ghcr.io/kanjieater/gsm-stream:<sha>, then:
docker compose pull && docker compose up -d
cd /mnt/cc/srv/gsm-stream-2 && docker compose pull && docker compose up -d
```

### CI/CD
- **Push to main** → builds image, pushes `ghcr.io/kanjieater/gsm-stream:latest` + `:<sha>`
- **Pull request** → builds image, pushes `ghcr.io/kanjieater/gsm-stream:pr-N` (testable before merge)
- **GHCR package** must be set to Public in GitHub → Packages settings (one-time, no auth needed to pull)

## Security
**Never hardcode personal domains, subdomains, or private URLs in source files.**
Use `<your-gsm-domain>` as a placeholder in docs. Real URLs belong in `profiles.yml`
on the host (not committed) or in `.env` files — never in `.py`, `.md`, `.js`, or any
file tracked by git. This applies to any `*.yourdomain.*` pattern.

## GitHub
Private repo: `kanjieater/gsm-stream`
