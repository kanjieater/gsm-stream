# Live Data Integration Notes

## Goal

Connect GSM's live OCR stream to the profiler as an observation-only black box.
The first pass should collect real data and predictions. It should not crop OCR
based on profiler predictions yet.

## Profiler Process

Start once per game/session:

```bash
dialogue-layout-profiler serve \
  --profile profiles/<game-id>.layout.json \
  --profile-id <game-id> \
  --flush-every 100 \
  --debug-log logs/<game-id>.profiler.debug.jsonl \
  --debug-log-flush-every 100
```

Keep the process alive. Do not spawn it once per frame.

## Send

For each processed frame, send:

```json
{"type":"observe_frame","frame":{"frame_id":"...","timestamp_ms":0,"frame":{"width":1920,"height":1080},"regions":[],"ui_regions":[]}}
```

Use Meiki's existing fields directly:

- `text`
- `bbox`
- `confidence`
- `is_vertical`
- `chars[].char`
- `chars[].bbox`
- `chars[].confidence`

## Receive

Read one response line per request:

```json
{"type":"prediction","prediction":{}}
```

For now, log the prediction and continue existing OCR behavior.

## Shutdown

On clean exit:

```json
{"type":"shutdown"}
```

Wait briefly for:

```json
{"type":"shutdown_ack"}
```

This flushes the hot in-memory profile so the next run can resume.

## Disk I/O

The profile is loaded once at startup and kept in memory. It is only written when
dirty and when the flush policy triggers, an explicit `flush` arrives, or the
server shuts down.

The debug log is optional and buffered. It flushes every 100 entries by default
and on explicit `flush`, `shutdown`, or process exit. For maximum throughput,
omit `--debug-log`.

## What To Bring Back

The useful feedback artifacts are:

- durable profile: `profiles/<game-id>.layout.json`
- debug replay log: `logs/<game-id>.profiler.debug.jsonl`
- a short capture summary:
  - frames sent
  - average text regions per frame
  - prediction mode counts
  - fallback/error counts
  - sample frames where predicted OCR region looks too broad or too narrow

## Safety

If the profiler process fails, times out, or returns invalid JSON, disable it for
the rest of the session and keep GSM's existing OCR behavior.
