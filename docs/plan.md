# Plan: Phase 1A (native player skeleton)

Source: docs/EXECUTION_PLAN.md, Phase 1A. One commit per step; CI on every push.

1. ✅ **Project + CI.** `player-android/` Gradle project (Kotlin DSL, wrapper committed): `core`
   (plain Kotlin JVM, JUnit tests) and `app` (Android, package `com.syncvr.player`, minSdk 25,
   armeabi-v7a, placeholder activity). New CI job `player-android`: runs `core` tests, checks the
   four signing secrets (fails with a clear error if any is missing), decodes the keystore,
   builds a signed release APK and uploads it as `SyncVRPlayer.apk`.
2. ✅ **Meta SDK fetch.** Script + CI step that downloads Oculus Mobile SDK 19.0 (VrApi 1.36) from a
   pinned public mirror commit, verifies its checksum, and exposes VrApi headers/libs to CMake.
3. ✅ **VR loop in C++.** `app/src/main/cpp` + CMake: enter VR mode, frame loop, Android surface
   swapchain, equirect / cylinder / panel layers.
4. ✅ **Video.** Media3 ExoPlayer decoding into the swapchain surface.
5. ✅ **Text panel.** Canvas-drawn label into a second surface layer.
6. ✅ **Diagnostic mode.** Play first video in the videos folder, cycle modes every 15 s (equirect →
   app-drawn sphere → cylinder → stereo top/bottom), log VrApi/system versions, decoder limits,
   refresh rates under tag `SyncVR`.
7. ✅ **Checkpoint 1 hand-off.** Update docs/HEADSET_SETUP.md with install + test steps for the user.
