# Handoff

## Goal
Replace the Unity headset app with a native Oculus Go player per docs/EXECUTION_PLAN.md. Phase 1B code (docs/plan.md steps 1-10) is done; next is Checkpoint 2 on hardware.

## Decisions and constraints
- `player-android/`: Kotlin + C++ VrApi 1.36, package `com.syncvr.player`, `.MainActivity`, minSdk 25, target 34, armeabi-v7a, media3 1.4.1. `core` = pure Kotlin (JVM tests), `app` = Android + C++.
- APK builds only in CI (job `player-android`, artifact `SyncVRPlayer-apk`, also published to `apk/<branch>`). `app` can't compile locally; local check: `cd player-android && ./gradlew --no-daemon :core:test` (rerun on Maven 429).
- VrApi calls only from the render thread after `App::InitVrApi()`; ExoPlayer calls on the main thread.
- Native player ignores config.json (discovery only). `hello` sends `player: "native"`.

## Current state
- Branch `code/serene-albattani-a6633i`, draft PR https://github.com/Trigger-EX/synchronized-vr-video-playback/pull/4 into main.
- Done: sync port, networking, ExoVideoPlayer, content mgmt, PlayerController wiring, telemetry, Overlay/recenter/identify, lifecycle + calibration, protocol `player` field + dashboard chip, e2e test (EndToEndTest.kt, CI sets SYNCVR_E2E_REQUIRED=1), docs (HEADSET_SETUP.md etc.).
- CI green through 4d0b7f8 (APK built). CI on 09074ae (e2e) and 697a13c (docs) was still running at checkpoint.

## Open questions
- Does the e2e step pass in CI (new setup-python/aiohttp in ci.yml)?
- Unverified on hardware: recenter yaw sign, resume flow, proximity "worn", eye order (mode 4), HW CHECK comments in cpp/layers.cpp to tidy.

## Next step
Check CI on 697a13c and fix any failure. Then give the user the new SyncVRPlayer-apk for Checkpoint 2 (steps in docs/HEADSET_SETUP.md); apply their results. Before marking PR ready, delete docs/handoff.md and docs/plan.md.
