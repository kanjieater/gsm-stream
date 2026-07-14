# gsm-stream

Docker container that bridges a Nintendo Switch RTSP stream (via [SysDVR](https://github.com/exelix11/SysDVR)) to the [GameSentenceMiner](https://github.com/bpwhelan/GameSentenceMiner) texthooker web UI for Japanese immersion and Anki mining.

## What it does

1. Pulls a live RTSP stream from the Switch over the local network
2. Runs two-pass OCR on every frame:
   - **Pass 1 — MeikiOCR** (local, fast): runs on every frame to detect when text has stabilized
   - **Pass 2 — Google Lens** (cloud, accurate): fires once on a confirmed-stable frame
3. Feeds the recognized text into GSM's pipeline — stats tracking, Anki card creation, audio/screenshot capture all work normally
4. Serves GSM's texthooker web UI on port 7275

## Requirements

- Nintendo Switch with [SysDVR](https://github.com/exelix11/SysDVR) installed (TCP or RTSP mode)
- [GameSentenceMiner](https://github.com/bpwhelan/GameSentenceMiner) installed as a Python package (pulled automatically by the Dockerfile)
- AnkiConnect running if you want Anki card creation
- A reverse proxy (e.g. Nginx Proxy Manager) if you want HTTPS / remote access
- Google account accessible via the host machine for Google Lens OCR

## Setup

### 1. Clone and configure

```bash
git clone https://github.com/kanjieater/gsm-stream
cd gsm-stream
cp profiles.yml profiles.yml.local   # keep your real config out of git
```

Edit `profiles.yml.local` (or create a `compose.yml` volume override) with your actual values:
- `anki.url` — your AnkiConnect address
- `defaults.ui.websocketUrl` — WebSocket URL the browser texthooker will connect to
- `profiles` — list of game titles (auto-populated as you switch presets in the UI)

### 2. `compose.yml`

Create a `compose.yml` alongside the repo (or copy and adapt the example):

```yaml
services:
  gsm-stream:
    build:
      context: .
    image: gsm-stream:latest
    container_name: gsm-stream
    restart: unless-stopped
    environment:
      TZ: America/Chicago
      SWITCH_HOST: 192.168.1.x          # Switch IP
      SWITCH_STREAM: rtsp://192.168.1.x:6666
      TEXTHOOKER_WS_URL: wss://<your-gsm-domain>/ws/texthooker
    volumes:
      - ./cache:/root/.cache
      - ./gsm_data:/root/.config/GameSentenceMiner
      - ./profiles.yml.local:/profiles.yml   # your real config
    ports:
      - "7275:7275"   # texthooker web UI
      - "7276:7276"   # MJPEG debug stream (optional)
    networks:
      - selfhost

networks:
  selfhost:
    external: true
```

### 3. Build and run

```bash
docker compose build
docker compose up -d
docker compose logs -f
```

Open the texthooker at `http://<host>:7275` (or via your reverse proxy).

## profiles.yml reference

`profiles.yml` is a live-mounted config file — changes take effect without rebuilding the container. The `audio`, `vad`, and `anki` sections are synced into every GSM profile on startup.

| Section | Purpose |
|---|---|
| `audio` | Audio clip timing offsets |
| `vad` | Voice activity detection (Whisper model, trim settings) |
| `anki` | AnkiConnect URL, note type, field mapping |
| `defaults.text_processing` | GSM text processing applied to all profiles (e.g. `remove_non_japanese`) |
| `defaults.ui` | Texthooker UI settings seeded into new profiles (theme, font size, CSS, etc.) |
| `profiles` | List of game profile names — managed automatically by the UI |
| `game_overrides` | Per-game server-side overrides (see below) |

### Per-game OCR noise filtering (`game_overrides.ocr_strip`)

Some games produce consistent OCR noise (UI overlays, watermarks, skip indicators). Add an `ocr_strip` regex per game to strip it server-side before the text reaches GSM's stat counter:

```yaml
game_overrides:
  'My Game Title':
    ocr_strip: "noise pattern at end$"
```

The pattern is a Python regex applied with `re.UNICODE`. The match is replaced with `""` and the result is stripped. This runs after both OCR passes so stats are never inflated by noise.

## Ports

| Port | Description |
|---|---|
| `7275` | GSM texthooker web UI and WebSocket |
| `7276` | MJPEG debug stream (live view of what the OCR is seeing) |

## Architecture

```
Switch RTSP :6666
  └─ ffmpeg → JPEG frames at 2fps
       └─ thread executor → handle_frame_in_thread
            ├─ MeikiOCR (pass 1, every frame, stability detection)
            └─ TwoPassOCRControllerV2
                 ├─ stable? → Google Lens via owocr WS :7331 (pass 2)
                 └─ _send() → game ocr_strip filter → gametext.handle_new_text_event()
                                                            └─ GSM pipeline (stats, Anki, WS broadcast)
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `SWITCH_STREAM` | — | RTSP URL of the Switch stream |
| `TEXTHOOKER_WS_URL` | — | WebSocket URL injected into the texthooker for reconnection |
| `FPS` | `2` | Frames per second pulled from RTSP for OCR |
| `STABLE_FRAMES` | `3` | Consecutive similar frames required before triggering pass 2 |
| `REPLAY_BUFFER_SECS` | `300` | Seconds of audio/video kept in memory for Anki card timing |
| `PROFILES_CONFIG` | `/profiles.yml` | Path to the profiles config file inside the container |
| `LAYOUT_PROFILER_ENABLED` | `0` | Enable experimental layout profiler (requires separate build) |
