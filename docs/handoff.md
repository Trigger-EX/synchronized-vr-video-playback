# Handoff

## Goal
Replace the Unity headset app with a native Oculus Go player per docs/EXECUTION_PLAN.md. Checkpoint 2 hardware testing is nearly done.

## Decisions and constraints
- `player-android/`: Kotlin + C++ VrApi 1.36, package `com.syncvr.player`, `.MainActivity`. `core` = pure Kotlin (JVM tests), `app` = Android + C++ (CI only).
- APK built only in CI job `player-android`, published to `apk/<branch>`; install with `git apk --install`. Local check: `cd player-android && ./gradlew --no-daemon --offline :core:test`.
- Connection: Wi-Fi needs firewall open (TCP 8765, 8080, UDP 8766); alternative `python3 -m syncvr adb launch --usb` (adb reverse, lost on replug). Server: `python3 -m syncvr serve` (needs `pip install -e .` in `server/`).
- `ServerConnection.send` queues to a writer thread (no main-thread socket IO).

## Current state
- Branch `code/serene-albattani-a6633i`, draft PR https://github.com/Trigger-EX/synchronized-vr-video-playback/pull/4 into main. Head 717c796.
- Hardware verified good: connect, Load/Play/sync, recenter, worn/proximity, resume.
- 717c796 fixes a ~30s hang at 49% when re-pushing after an interrupted download (ContentStore.abort(), read timeout 10s, .part kept). Core tests pass; not yet verified on hardware.
- PR #7 (session setup v6) is open; PRs #5 and #6 hold older setup versions.

## Open questions
- Does re-push after interruption now resume promptly on hardware (re-test with new APK)?
- Stereo eye order (mode 4) untested; user has no stereo video yet.
- Optional: show a dashboard error when Load/Play is sent with no headset online.

## Next step
Check CI on 717c796, then have the user retest download resume with the new APK. Before marking PR #4 ready, delete docs/handoff.md and docs/plan.md in a final commit.
