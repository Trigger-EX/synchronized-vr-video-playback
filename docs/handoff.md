# Handoff

## Goal
Replace the Unity headset app with a native Oculus Go player, following docs/EXECUTION_PLAN.md
(order: 1A → 3.1 → 1B → 2 → 3.2 → 4 → 5). Right now: get Checkpoint 1 (hardware check) running on a real Go.

## Decisions and constraints
- `player-android/`: Kotlin + C++ VrApi 1.32 (Oculus Mobile SDK 15.0; Go final OS has VrApi 1.1.35 and rejects newer loaders), package `com.syncvr.player`, activity `.MainActivity`, minSdk 25, target 34, armeabi-v7a.
- APK is built only in CI (job `player-android`, artifact `SyncVRPlayer-apk`); dl.google.com is blocked locally.
- On the Go, VrApi's loader aborts if any `vrapi_*` query (e.g. `vrapi_GetVersionString`) runs before `vrapi_Initialize`. Only call VrApi from the render thread after `App::InitVrApi()` (cpp/app.cpp).
- Launch by shell: `adb shell am start -n com.syncvr.player/.MainActivity` (or `python3 -m syncvr adb launch`).

## Current state
- Phase 1A and 3.1 done and merged to `main`.
- User's first hardware run crashed instantly: SIGABRT "vrapi_GetVersionString was called before vrapi_Initialize()" from `MainActivity.onCreate` → `NativeBridge.vrApiVersion()`.
- Fixed in 7145102 on branch `code/serene-albattani-a6633i` (removed the JNI `vrApiVersion` and its call; native `LogDiagnostics` already logs the version after init). Files: cpp/jni_bridge.cpp, MainActivity.kt, NativeBridge.kt. No PR yet. Not yet verified on hardware.
- CI green on 0af6f40 (run 37169127216). APK given to user: https://github.com/Trigger-EX/synchronized-vr-video-playback/actions/runs/37169127216/artifacts/11290178126 (expires 2027-01-02).

## Open questions
- Checkpoint 1 results (display path, orientation, panel placement, decoder limits, pause behavior; see docs/HEADSET_SETUP.md).
- Any further crash after this fix.

## Next step
Wait for the user's hardware run of the new APK. If they send a crash log, fix it; if they send Checkpoint 1 results, apply them (display path, HW CHECK spots in cpp/layers.cpp, limits.py). Otherwise start Phase 1B step 1.
