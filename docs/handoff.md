# Handoff

## Goal
Replace the Unity headset app with a native Oculus Go player, following docs/EXECUTION_PLAN.md
(order: 1A → 3.1 → 1B → 2 → 3.2 → 4 → 5). Now: Phase 1B, the full native player.

## Decisions and constraints
- `player-android/`: Kotlin + C++ on VrApi 1.32 (Mobile SDK 15.0, lovr-org/ovr_sdk_mobile f88e937). The Go's final OS has VrApi 1.1.35 and rejects newer loaders. Package `com.syncvr.player`, activity `.MainActivity`, minSdk 25, armeabi-v7a.
- Only call VrApi on the render thread, after `App::InitVrApi()` (cpp/app.cpp).
- The APK is built only in CI (job `player-android`). CI force-pushes it to the branch `apk/<branch>`. The user fetches it with `git apk [--install|--launch]` (player-android/tools/get-apk.sh).
- Checkpoint 1 passed (results are in EXECUTION_PLAN.md): the main path is the equirect layer (mode 1), the sphere is the fallback, everything runs at 60 fps, and decoders handle up to 4096×2048 at 30 fps.
- The user wants a woken headset to resync to the operator's playback position (Phase 1B step 7).

## Current state
- Branch `code/serene-albattani-a6633i`, draft PR https://github.com/Trigger-EX/synchronized-vr-video-playback/pull/4 into main.
- The branch has the VrApi 1.32 pin, APK publishing, `git apk --launch`, the checkpoint hooks/CLAUDE.md, and the checkpoint 1 results.

## Open questions
- Stereo eye order (mode 4) is unverified. It needs a real top/bottom 3D video.
- HW CHECK comments in cpp/layers.cpp (texture origin, cylinder size) still need tidying, since modes 1 to 3 looked right.

## Next step
Phase 1B: write numbered steps in docs/plan.md (it touches many files), commit, then start step 1 (port clock sync, sync engine, and protocol to Kotlin `core`, with the shared scenario tests).
