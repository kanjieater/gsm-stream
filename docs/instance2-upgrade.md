# Instance 2: GSM 2026.9.2 upgrade (promoted to default)

## Status: promoted

GSM 2026.9.2 is now the Dockerfile's default `GSM_VERSION`, so the CI-published
`:latest` image (and both live instances, once redeployed from it) run 2026.9.2.
This section below was written while 2026.9.2 was still an instance-2-only
opt-in build; it's kept as the record of what was and wasn't validated before
promotion. Formally executed: the six offline contract tests, the Anki-thread
compat fix, a DB/config migration integrity check, and extended real-world use
on the instance-2 candidate across multiple live game sessions (including
reconnect/TextFeed behavior). **Not** formally executed as separate gated
steps before promotion: a dedicated benchmark of representative frames against
the old image, and a scripted disposable-Anki-profile card-creation check (list
item 5/6 below). Treat those as outstanding follow-up, not as blocking this
promotion.

## Historical scope and safety (pre-promotion)

Only the second instance was a candidate. Do not restart, redeploy, retag, or
change the primary service. Do not use the existing deploy-both script.
The default Docker build remained GSM 2026.7.1; the candidate was opt-in:

```sh
docker build --build-arg GSM_VERSION=2026.9.2 -t gsm-stream-candidate:2026.9.2 .
docker run --rm --network none --entrypoint python \
  -e EXPECT_GSM_NATIVE=1 -e GSM_TEST_ISOLATED=1 gsm-stream-candidate:2026.9.2 \
  -m unittest discover -s tests -v
```

Ordinary unittest discovery skips the GSM integration module before importing
bridge/GSM. `GSM_TEST_ISOLATED=1` is an explicit attestation, not a sandbox:
use it only in a disposable container without real GSM state mounted. Do not
set it on the host. `PROFILES_CONFIG` alone does not isolate GSM's config/DB.

The ordinary PR image remained on 2026.7.1 at the time this was written. The
upgrade-contracts workflow still builds both releases in CI as regression
coverage for the older release.

## Confirmed compatibility fix

GSM 2026.7.1's `anki.start_monitoring_anki()` starts its own daemon thread.
In 2026.9.2 it directly executes the blocking monitor loop; GSM's runtime puts
it in a worker thread. Our direct call stalled bridge startup before VAD and
RTSP ingestion. The bridge now starts a named daemon thread, compatible with
both releases. The upstream mining pipeline and existing patches are retained.

## Local experiment (2026-09-16)

- Both running instances used the same local GSM 2026.7.1 image, despite their
  Compose declarations referring to GHCR.
- The second instance's **actual mounts share the primary's database/config and
  profiles**. Its similarly named local data directory is not the active data.
- Created an offline snapshot of active config/profiles; used SQLite online
  backup with a read-only source connection, not a raw live DB/WAL copy.
  Excluded transient media, logs and old backups. No live mounts in probes.
- Built an experimental image by upgrading the existing image to 2026.9.2;
  `pip check` passed. This is not a clean-build reproducibility claim.
- With network disabled, the unmodified bridge stalled at Anki startup.
  With the thread fix, it reached VAD initialization and the MJPEG/RTSP loop.
  Network-dependent model initialization/recognition was not validated.
- Copied DB integrity passed before/after, retaining 59,729 game lines and
  15 games. Config migration adds settings and removes retired keys, including
  `overlay.use_ocr_area_config_v2` and `default_config_change_decisions`.
  Counts and integrity checks are not proof of full semantic preservation.
- Six offline contracts passed against 2026.9.2; five passed against 2026.7.1
  with the native-extension test skipped. They cover blocking Anki startup,
  stream/recording guards, custom routes, text matching, and stable-frame
  Lens handoff with duplicate suppression. Lens and text intake are mocked.
- Both live container image IDs and start times remained unchanged.
- The second Switch's RTSP endpoint timed out on a bounded host TCP check;
  its existing container already logged connection failures.

## Rust and OCR performance

2026.9.2 includes GSM's Rust native extension and defaults native OCR processing
on. The test verifies importability, not execution of every native path.
The bridge still uses upstream MeikiOCR for first-pass recognition and Lens via
owocr for the stable second pass. MeikiOCR is still 0.3.4, matching the existing
image. **No measured local-recognition speedup or accuracy improvement is claimed.**
Do not replace recognition with detector-only OCR without separate tests for
speaker/noise filtering, duplicate suppression and cloud-request frequency.

## Required before an instance-2 rollout

1. Preserve the actual running legacy image by immutable ID and record its
   configuration; do not rely on the mutable Compose tag for rollback.
2. Snapshot current active data again using SQLite backup. Keep an untouched
   pre-migration copy and give instance 2 **separate writable config, DB,
   profiles and cache**. Resolve all mounts and reject any primary paths,
   symlinks or shared volumes. Never copy migrated data back to the primary.
3. Inspect copied config/plugin jobs and disable external integrations in the
   rehearsal. Use no host networking, Docker socket, production mounts, or
   Anki endpoint during offline validation. Fixture tests need no live Switch.
4. Run a clean candidate build and CI. Rehearse config/profile sync and DB
   migration against disposable copies; compare retained data and settings.
5. Test actual owocr startup, Meiki recognition, Lens results, texthooker HTTP
   and WebSocket delivery, custom browser UI, profile switching, replay timing,
   screenshots, audio/VAD, transcript verification, and a card in a disposable
   Anki profile. Supply an approved recording and expected output for repeatable
   integration testing. Mocked tests do not substitute for these checks.
6. Benchmark representative frames against the old image under identical CPU
   limits; check latency, accuracy, memory stability and Lens request count.
7. Only after those gates pass, replace **instance 2 only** with the pinned
   candidate and its isolated volumes. Do not run the shared deployment script.
   Monitor it and verify the primary image ID/start time remain unchanged.
8. Roll back instance 2 with the preserved old image and untouched pre-upgrade
   instance-2 snapshot, never by downgrading the migrated database in place.
   Retain post-upgrade writes separately for reconciliation.

This PR is an upgrade attempt and regression fix, not an assertion that all
custom mining behavior is verified. Neither live service was changed.
