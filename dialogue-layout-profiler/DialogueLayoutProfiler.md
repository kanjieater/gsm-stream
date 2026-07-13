# Dialogue Layout Profiler

## Purpose

`dialogue-layout-profiler` learns geometric dialogue layout patterns from OCR
observations and predicts where future OCR should focus. It is independent of OCR
engines, capture devices, GSM, Switch, RTSP, and application-specific behavior.

The current implementation is pure Rust. GSM or any other caller should treat it
as a black box and integrate through the JSONL CLI/server protocol or by linking
the Rust crate directly.

## Black-Box Contract

The caller sends:

- frame id
- timestamp
- frame dimensions
- OCR text regions
- optional UI regions
- optional metadata

The profiler returns:

- OCR regions to focus
- regions to ignore
- speaker region candidates
- prediction confidence
- prediction mode
- structured debug data

## JSONL Server API

```bash
dialogue-layout-profiler serve --profile profiles/game.layout.json --profile-id game-id
```

The server is long-lived. It loads the profile once, keeps it hot in memory,
handles many frame observations, and flushes according to policy or explicit
commands. This avoids reading and writing the profile for every frame.

Each stdin line is one request. Each stdout line is one response.

Development example:

```bash
cargo run -p dialogue-layout-profiler-cli -- serve --profile profiles/example.layout.json --profile-id example < examples/frame.jsonl
```

Request protocol:

```json
{"type":"observe_frame","frame":{"frame_id":"frame-001","frame":{"width":1920,"height":1080},"regions":[]}}
{"type":"flush"}
{"type":"export_profile"}
{"type":"shutdown"}
```

Bare `FrameObservation` JSON lines are also accepted as legacy/convenience
`observe_frame` requests.

Response protocol:

```json
{"type":"prediction","prediction":{}}
{"type":"flushed"}
{"type":"profile","profile":{}}
{"type":"shutdown_ack"}
{"type":"error","error":"invalid_frame_observation","message":"..."}
```

## Input Region Shape

```json
{
  "id": "r1",
  "kind_hint": "text",
  "bbox": [420, 760, 1510, 940],
  "text": "I suppose we should go now.",
  "confidence": 0.94,
  "is_vertical": false,
  "chars": [
    {
      "char": "I",
      "bbox": [426, 772, 436, 794],
      "confidence": 0.98
    }
  ]
}
```

Hints are evidence, not commands.

## Prediction Shape

```json
{
  "frame_id": "frame-001",
  "mode": "tentative",
  "active_layout_id": "stub_layout_recent_union",
  "confidence": 0.63,
  "ocr_regions": [
    {
      "id": "ocr_primary",
      "bbox": [372, 733, 1548, 967],
      "confidence": 0.63,
      "purpose": "dialogue",
      "padding": 0.025
    }
  ],
  "ignore_regions": [],
  "speaker_regions": [],
  "debug": {
    "layout_confidence": 0.63,
    "matched_observation_ids": ["r1"],
    "events": ["stub_prediction_from_recent_text_union"],
    "fallback_reason": null
  }
}
```

## Prediction Modes

- `established`: trusted learned layout.
- `tentative`: likely layout, still maturing.
- `exploratory`: early evidence only.
- `fallback`: no reliable prediction.

## Write-Through Cache

The profiler owns the game profile as hot in-memory state and persists it to disk
using a write-through profile cache.

Supported flush policies:

- `every_observation`
- `every_n_observations`
- `manual`

Writes are atomic: the profile is written to a temporary file, then renamed over
the target profile path.

## Resumability

Profiles are durable JSON files. On startup, `serve --profile <path>` loads the
existing profile if it exists. On controlled shutdown, the server flushes the hot
in-memory profile before returning `shutdown_ack`.

Expected lifecycle:

1. GSM starts the profiler process for a game/session.
2. Profiler loads the saved profile once.
3. GSM sends many `observe_frame` requests.
4. Profiler mutates the in-memory profile and occasionally flushes.
5. GSM sends `shutdown`, or the process flushes when stdin closes.
6. A later restart resumes from the same profile path.

Recommended early integration setting:

```python
flush_policy={"kind": "every_n_observations", "n": 25}
```

For tests and debugging:

```python
flush_policy={"kind": "every_observation"}
```

## Current V1 Behavior

The initial Rust implementation currently:

- normalizes boxes internally
- filters very low-confidence regions
- predicts an expanded union of current usable text boxes
- tracks recurring region memories
- promotes repeated low-volatility small text regions as speaker candidates
- promotes repeated stable UI regions as ignore candidates
- persists durable profile JSON
- returns structured debug data

This is intentionally conservative. It is meant to unblock GSM integration before
the full layout-learning model exists.
