# Handoff

## Goal
Native Oculus Go player (replacing the Unity app) for synchronized VR video playback. Checkpoint 2 hardware testing is nearly done.

## Decisions and constraints
- `player-android/`: Kotlin + C++ VrApi 1.36, package `com.syncvr.player`, `.MainActivity`. `core` = pure Kotlin (JVM tests), `app` = Android + C++ (CI only).
- APK built only in CI job `player-android`, published to `apk/<branch>`; install with `git apk --install`. Local check: `cd player-android && ./gradlew --no-daemon --offline :core:test`.
- Connect over Wi-Fi (firewall open: TCP 8765, 8080, UDP 8766) or USB: `python3 -m syncvr adb launch --usb` (adb reverse, lost on replug). Server: `python3 -m syncvr serve`.
- Docs `docs/handoff.md` and `docs/plan.md` must not be in main when a PR is marked ready.

## Current state
- PRs #4 (Go player), #7 (session setup v6) and #8 (docs cleanup) are all merged into main.
- Hardware verified: connect, Load/Play/sync, recenter, worn/proximity, resume.
- Download-resume hang fix (ContentStore.abort(), 10s read timeout, .part kept) is merged; not yet confirmed on hardware.
- Branch `code/serene-albattani-a6633i` is stale (merged history plus a docs-removal commit); safe to delete.

## Open questions
- Does re-pushing after an interrupted download now resume promptly on the headset?
- Stereo eye order (mode 4) untested; no stereo video yet.
- Optional: dashboard error when Load/Play is sent with no headset online.

## Next step
Ask the user for the interrupted-download retest result with the latest main APK (`git apk --install`) and fix anything that fails.
