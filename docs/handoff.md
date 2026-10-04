# Handoff

## Goal
Replace the Unity headset app with a native Oculus Go player per docs/EXECUTION_PLAN.md; now executing Phase 1B (docs/plan.md, 10 steps).

## Decisions and constraints
- `player-android/`: Kotlin + C++ VrApi 1.36, package `com.syncvr.player`, `.MainActivity`, minSdk 25, target 34, armeabi-v7a. `core` = pure Kotlin (JVM tests), `app` = Android + C++.
- APK builds only in CI (job `player-android`); dl.google.com blocked locally, so `app` can't compile locally. Local check: `cd player-android && ./gradlew --no-daemon :core:test` (a Maven 429 can occur; rerun).
- VrApi calls only from the render thread after `App::InitVrApi()`.
- Protocol: ports 8765/8766, JSON lines, same as server and Unity client.

## Current state
- Branch `code/serene-albattani-a6633i`, draft PR https://github.com/Trigger-EX/synchronized-vr-video-playback/pull/4 into main.
- Step 1 (sync port) done in 875d165; step 2 (networking) done in 6b45458: `core/.../net/` (Sockets.kt, Discovery.kt, ServerConnection.kt with backoff reconnect + ping thread), app `MulticastLockGuard`/`DiscoveryGuard`, Wi-Fi permissions. 42 core tests pass.
- CI for ff17cda was still running; 6b45458 CI not yet checked.

## Open questions
- `app` module not compiled locally; check CI.
- ServerConnection not yet wired into MainActivity; real UDP receiver untested.
- Stereo eye order (mode 4) unverified; HW CHECK comments in cpp/layers.cpp need tidying.

## Next step
Check CI on 6b45458 (fix any `player-android` app build error), then plan step 3: ExoPlayer backend behind `VideoPlayer`.
