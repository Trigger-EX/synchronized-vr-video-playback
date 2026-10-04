# Handoff

## Goal
Replace the Unity headset app with a native Oculus Go player, following docs/EXECUTION_PLAN.md
(order: 1A → 3.1 → 1B → 2 → 3.2 → 4 → 5). Now: Phase 1B, the full native player. Steps are in docs/plan.md.

## Decisions and constraints
- `player-android/`: Kotlin + C++ on VrApi 1.32 (Mobile SDK 15.0, lovr-org/ovr_sdk_mobile f88e937). The Go's final OS has VrApi 1.1.35 and rejects newer loaders. Package `com.syncvr.player`, activity `.MainActivity`, minSdk 25, armeabi-v7a.
- Only call VrApi on the render thread, after `App::InitVrApi()` (cpp/app.cpp).
- APK is built only in CI (job `player-android`), force-pushed to branch `apk/<branch>`; user fetches with `git apk [--install|--launch]`.
- Checkpoint 1 passed: main path is the equirect layer (mode 1), sphere is the fallback, 60 fps, decoders up to 4096×2048@30.
- Woken headset must resync to the operator's position (step 7).
- `core` tests run locally with `gradle -p player-android :core:test` (app module is skipped without an SDK).
- Kotlin core JSON is hand-rolled (no deps); whole-number doubles serialize as `3`, not `3.0`.

## Current state
- Branch `code/serene-albattani-a6633i`, draft PR https://github.com/Trigger-EX/synchronized-vr-video-playback/pull/4 into main.
- docs/plan.md written (10 steps). Step 1 done in 875d165: `player-android/core/src/{main,test}/kotlin/com/syncvr/player/core/sync/` — ClockSync.kt, SyncEngine.kt (holds `VideoPlayer` interface), Messages.kt, FakePlayer.kt, SyncEngineTest.kt (11 C# scenarios + Python's fastest-round-trip case), MessagesTest.kt. 32 core tests pass locally. CI for 875d165 was queued at checkpoint time.

## Open questions
- Stereo eye order (mode 4) unverified; needs a top/bottom 3D video.
- HW CHECK comments in cpp/layers.cpp need tidying.

## Next step
Confirm CI is green on 875d165, then do plan step 2 (networking in `core`: UDP discovery, TCP auto-reconnect, ping thread; multicast lock in `app`).
