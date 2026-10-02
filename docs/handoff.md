# Handoff

## Goal
Replace the Unity headset app with a native Oculus Go player, following docs/EXECUTION_PLAN.md
(order: 1A → 3.1 → 1B → 2 → 3.2 → 4 → 5).

## Decisions and constraints
- Branch `code/nice-clarke-o9fuha` (the old `claude/adoring-curie-qnd0ib` named in EXECUTION_PLAN is obsolete). No PR yet.
- `player-android/`: Kotlin + C++ VrApi 1.36 (Oculus Mobile SDK 19.0), package `com.syncvr.player`, minSdk 25, compile/target 34, armeabi-v7a, AGP 8.7.3, Kotlin 2.0.21, Gradle 8.14.3, Media3 1.4.1.
- The SDK is fetched by `player-android/tools/fetch-vrapi.sh` (lovr-org/ovr_sdk_mobile @447c814, per-file SHA-256) into git-ignored `third_party/`.
- Signing uses only the GitHub secrets SYNCVR_KEYSTORE_BASE64/_PASSWORD, SYNCVR_KEY_ALIAS, SYNCVR_KEY_PASSWORD. Never commit a keystore.
- dl.google.com is blocked locally, so only `:core` builds here (ANDROID_HOME unset). The APK is built only in CI (job `player-android`, artifact `SyncVRPlayer-apk`).

## Current state
- Phase 1A is done (all steps in docs/plan.md ✅). CI is green on c389060: server, headset-engine and player-android all pass.
- The diagnostic APK cycles 4 modes every 15 s: equirect mono, sphere fallback, cylinder, stereo TB. Logs use tag `SyncVR`.
- Phase 3.1 is done: ffprobe checks, SHA-256 in `sync_content`, Library "Checks" column. Code is in server/syncvr/analysis.py, limits.py and mp4.py.
- docs/HEADSET_SETUP.md has the Checkpoint 1 guide.
- Uncertain on hardware (marked "HW CHECK" in cpp/layers.cpp): panel/cylinder placement, texture origin, stereo eye order, pause blocking up to 3 s.

## Open questions
- Checkpoint 1 results: which display path to use, orientation fixes, decoder limits for `GoLimits`.

## Next step
If the user sent Checkpoint 1 results, apply them first: pick the display path, fix orientation and placement, and update limits.py. Otherwise start Phase 1B step 1, which doesn't depend on them: write docs/plan.md for 1B, then port clock sync, the sync engine and the protocol to `player-android/core`, with the shared scenario tests.
