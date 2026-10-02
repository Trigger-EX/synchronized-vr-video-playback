# Plan: Phase 1A (native player skeleton)

From docs/EXECUTION_PLAN.md, Phase 1A. One commit per step.

1. Gradle skeleton in `player-android/`: modules `core` (plain Kotlin, JVM tests) and `app`
   (Android, package `com.syncvr.player`, minSdk 25, armeabi-v7a, placeholder activity).
   Gradle wrapper committed. CI job `player-android` runs `core` tests, builds a release APK
   signed from the `SYNCVR_KEYSTORE_*` secrets (fails clearly if any is missing), uploads it.
2. CI fetches Oculus Mobile SDK 19.0 (VrApi 1.36) from a pinned public mirror commit, verifies
   its checksum, and exposes it to the build (never committed).
3. C++ VR loop (`app/src/main/cpp`, CMake): enter VR mode, frame loop, Android surface swapchain,
   equirect / cylinder / text-panel layers.
4. ExoPlayer (Media3) decodes into the swapchain surface.
5. Text panel drawn with `Canvas` into a second surface layer.
6. Diagnostic mode: play first video in the video folder, cycle modes every 15 s
   (equirect → app sphere → cylinder → stereo top/bottom), log VrApi/system versions,
   decoder limits and refresh rates to logcat tag `SyncVR`.
7. Checkpoint 1 instructions for the user.
