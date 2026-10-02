# Handoff

## Goal
Replace the Unity headset app with a native Android player for Oculus Go (Kotlin + C++ VrApi + Media3 ExoPlayer), then the server and fleet phases, per docs/EXECUTION_PLAN.md. Step list for Phase 1A is in docs/plan.md.

## Decisions and constraints
- Order: 1A skeleton → 3.1 content checks → 1B full player → 2 → 3.2 → 4 → 5. Phase 0 skipped.
- `player-android/`: modules `core` (plain Kotlin, JVM tests) and `app` (Android). Package `com.syncvr.player`, minSdk = targetSdk = 25 (lint `ExpiredTargetSdkVersion` disabled), compileSdk 34, `armeabi-v7a` only, JVM target 17 (no toolchain; local JDK is 21).
- AGP 8.7.3, Kotlin 2.0.21, Gradle wrapper 8.14.3.
- Release signing via env `SYNCVR_KEYSTORE_FILE`, `SYNCVR_KEYSTORE_PASSWORD`, `SYNCVR_KEY_ALIAS`, `SYNCVR_KEY_PASSWORD`; CI decodes secret `SYNCVR_KEYSTORE_BASE64`. versionCode = `GITHUB_RUN_NUMBER`.
- Oculus Mobile SDK 19.0 (VrApi 1.36), fetched in CI from a public mirror pinned by commit + checksum, never committed.
- APK builds only in CI (dl.google.com blocked here). Local core tests: scratch settings that includes only `:core` (root build applies AGP, which can't resolve here).
- `hello` gains `player: "native"` (`PlayerInfo.PLAYER`).

## Current state
- Branch `code/great-sagan-bkb1x1` (no PR). Step 1 of docs/plan.md done: Gradle skeleton + CI job `player-android` in `.github/workflows/ci.yml` (core tests → secret check → signed `assembleRelease` → `apksigner verify` → artifact `SyncVRPlayer`). CI run 36983931444 all green.
- `app` is a placeholder `MainActivity` that only logs to tag `SyncVR`.

## Open questions
- Does the Go firmware support `VRAPI_LAYER_TYPE_EQUIRECT2`? (checkpoint 1; fallback = app-drawn sphere sampling the OES texture)
- Which public mirror/commit holds SDK 19.0 exactly (lovr-org/ovr_sdk_mobile master is SDK 25 — too new).

## Next step
docs/plan.md step 2: find a pinned public mirror of Oculus Mobile SDK 19.0, add a CI step that downloads it, verifies SHA-256, and exposes VrApi headers/libs to the `app` build.
