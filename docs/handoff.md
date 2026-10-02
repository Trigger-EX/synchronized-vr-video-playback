# Handoff

## Goal
Replace the Unity headset app with a native Android player for Oculus Go (Kotlin + C++ VrApi + Media3 ExoPlayer), then the server and fleet phases, as laid out in docs/EXECUTION_PLAN.md.

## Decisions and constraints
- Follow docs/EXECUTION_PLAN.md in order: 1A skeleton → 3.1 content checks → 1B full player → 2 → 3.2 → 4 → 5. Phase 0 (Unity testing) skipped by the user.
- Native app lives in `player-android/` (modules `core` = plain Kotlin, `app` = Android). Package `com.syncvr.player` (replaces the Unity app; same video folder `/sdcard/Android/data/com.syncvr.player/files/videos/`). Min API 25, ARMv7.
- Oculus Mobile SDK 19.0 (VrApi 1.36), last with Go support; fetched in CI from a public mirror pinned by commit + checksum, never committed.
- APK is built only in GitHub Actions (this container can't reach dl.google.com; maven.google.com and Maven Central work, so `core` JVM tests run locally).
- Signing: user is adding GitHub secrets `SYNCVR_KEYSTORE_BASE64`, `SYNCVR_KEYSTORE_PASSWORD`, `SYNCVR_KEY_ALIAS`, `SYNCVR_KEY_PASSWORD`. CI must fail clearly if any is missing.
- Protocol stays as docs/PROTOCOL.md; `hello` gains `player: "native"`.

## Current state
- Branch `claude/adoring-curie-qnd0ib` (also the repo's only/default branch on GitHub, so no PR can be opened yet).
- Server, dashboard, sim, adb tool, Unity app, CI (`.github/workflows/ci.yml`) done; 42 pytest + C# engine tests green.
- Reference sync engine: `server/syncvr/sync_engine.py`; scenario tests: `server/tests/test_sync_engine.py`, `headset/Tests~/EngineTests.cs`.

## Open questions
- Whether the Go's firmware supports VRAPI_LAYER_TYPE_EQUIRECT2 (answered at checkpoint 1; fallback = app-drawn sphere sampling the external OES texture).
- Which public mirror/commit holds SDK 19.0 exactly (lovr-org/ovr_sdk_mobile master is SDK 25 / VrApi 1.42 — too new).

## Next step
Start Phase 1A, step 1: write numbered steps to docs/plan.md, then create the `player-android/` Gradle project skeleton and a CI job that builds a signed APK.
