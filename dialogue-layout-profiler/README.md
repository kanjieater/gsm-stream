# Dialogue Layout Profiler

Pure Rust starting point for learning dialogue OCR layouts from geometric frame
observations.

The crate is intentionally independent of OCR engines, capture systems, GSM, and
application code. Callers send frame observations and receive OCR focus/ignore
predictions.

## Shape

```text
rust/dialogue-layout-profiler      core library
rust/dialogue-layout-profiler-cli  JSONL CLI/server
wiki/DialogueLayoutProfiler.md     design/API spec
```

## JSONL Server

The CLI is designed as the first black-box integration point:

```bash
dialogue-layout-profiler serve \
  --profile profiles/game.layout.json \
  --profile-id game-id \
  --flush-every 100 \
  --debug-log logs/game.profiler.debug.jsonl \
  --debug-log-flush-every 100
```

Each line on stdin is one `FrameObservation` JSON object. Each line on stdout is
one response JSON object. The process is long-lived: it loads the profile once,
keeps it in memory, observes many frames, and flushes according to policy or on
shutdown.

Example:

```bash
cargo run -p dialogue-layout-profiler-cli -- serve --profile profiles/example.layout.json --profile-id example < examples/frame.jsonl
```

Command-envelope protocol:

```json
{"type":"observe_frame","frame":{"frame_id":"frame-001","frame":{"width":1920,"height":1080},"regions":[]}}
{"type":"flush"}
{"type":"export_profile"}
{"type":"shutdown"}
```

Responses:

```json
{"type":"prediction","prediction":{}}
{"type":"flushed"}
{"type":"profile","profile":{}}
{"type":"shutdown_ack"}
```

Predictions include pixel bbox coordinates for the current dialogue estimate and
learned non-dialogue regions:

```json
{
  "type": "prediction",
  "prediction": {
    "ocr_regions": [{"bbox": [372, 733, 1558, 967], "purpose": "dialogue"}],
    "ignore_regions": [{"bbox": [430, 695, 690, 745], "reason": "stable_speaker_candidate"}],
    "speaker_regions": [{"bbox": [430, 695, 690, 745]}],
    "classified_regions": {
      "dialogue": [],
      "names": [],
      "ui": [],
      "non_dialogue": []
    }
  }
}
```

For convenience, bare `FrameObservation` lines are also accepted and treated as
`observe_frame` requests.

`--debug-log` writes replayable request/response JSONL entries through a buffered
writer. It is separate from the durable profile and is intended for live-data
analysis.

The core keeps the active profile hot in memory and persists it through a dirty
write-through cache using atomic file replacement. It only writes the profile
after observations changed it, according to the flush policy or shutdown. On
restart, the same `--profile` path is loaded so learning resumes from the
previous saved profile.

See `wiki/DialogueLayoutProfiler.md` for the design/API contract.
