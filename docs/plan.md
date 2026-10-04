# Phase 1B plan: the full native player

Source: docs/EXECUTION_PLAN.md, Phase 1B. Code lives in `player-android/` (`core` = pure Kotlin,
JVM-testable; `app` = Android + C++). Each step is committed and pushed on its own; CI builds the APK.

1. **Core port.** Port `ClockSync.cs`, `SyncEngine.cs` and `Messages.cs` (headset/Assets/SyncVR/Scripts)
   to Kotlin in `core` (`com.syncvr.player.core.sync`). Port every scenario in `headset/Tests~/EngineTests.cs`
   to JUnit so the Kotlin engine passes the same cases as the C# and Python engines.
2. **Networking (`core`).** UDP discovery, TCP client with auto-reconnect, dedicated ping thread
   with receive-time timestamps. Socket code behind interfaces so it is testable on the JVM.
   `app`: acquire a multicast lock while discovering.
3. **Player backend (`app`).** ExoPlayer behind a Kotlin `VideoPlayer` interface (mirrors `IVideoPlayer`):
   exact seeks, pitch-preserving speed, audio-clock position, looping. Feed its surface into the
   existing external texture / equirect layer path.
4. **Content (`core` + `app`).** Resumable HTTP downloads, checksum verification, inventory, deletion,
   same folder as the Unity app so `adb push` still works.
5. **Telemetry (`app`).** Battery, temperature, free storage, Wi-Fi signal, mounted status, frame rate;
   high-performance Wi-Fi lock.
6. **Operator commands.** Recenter, volume, on-screen messages, identify (name + beep), idle screen
   with name and connection state.
7. **Lifecycle.** Leave VR on pause, re-enter on resume, resync clock and re-cue to the operator's
   current position. Persist calibration (start latency, seek time).
8. **Protocol.** `hello` reports `player: "native"`; dashboard shows it.
9. **End-to-end CI test.** Kotlin client with a simulated decoder joins the Python server, follows
   play/pause/seek, stays in sync.
10. **Docs.** New headset guide without Unity: download APK, `syncvr adb setup`.

Then: tidy HW CHECK comments in cpp/layers.cpp, and Checkpoint 2 on hardware.
