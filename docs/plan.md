# Plan: kiosk restore, resume after server restart, dashboard warning, Unity cleanup, player retry

Earlier work (single-port downloads, pending-download persistence, `adb kiosk`) is in git history.

Hardware finding: `kiosk off` printed ok but `pm list packages -d` still listed com.oculus.vrshell, and the HOME resolve still returned com.syncvr.player/.MainActivity (user chose SyncVR + "Always" in the chooser). vrshell HOME = com.oculus.vrshell/.MainActivity.

## 1. Kiosk (APK + adbtool)
- Manifest: move the HOME filter to `activity-alias .HomeAlias` (disabled by default). Add exported `KioskReceiver` with `android:permission="android.permission.DUMP"` (shell has it, apps don't).
- `KioskReceiver.kt`: `--ez on true` enables the alias; `false` disables it and calls `clearPackagePreferredActivities(packageName)`. Not `pm clear` (would wipe config.json).
- adbtool `on`: disable vrshell, send broadcast, `set-home-activity .../.HomeAlias` best effort, verify.
- adbtool `off`: `pm enable com.oculus.vrshell`; if `pm list packages -d` still lists it, retry `pm enable --user 0`; fail loudly if still disabled. Then broadcast off, `set-home-activity com.oculus.vrshell/.MainActivity`, verify, `am start -a MAIN -c HOME`.
- New `kiosk restore [--root]`: `off` plus `pm unhide`; with root also `adb root` + `pm enable`.
- Verify with `cmd package resolve-activity --brief -a MAIN -c HOME` (no package). `ResolverActivity` or a non-vrshell result means failure; print the next command to run.
- Tests in `server/tests/test_adbtool.py`; update `docs/HEADSET_SETUP.md`.
- Risks: existing "Always" entry points at MainActivity (`restore` clears it); DUMP must be granted to shell on the Go.

## 2. Resume after server restart
Likely cause: `Distributor.check_idle` measures from job start, so a status without `download` for 20 s ends the job (player hashing existing files, or waiting on its old worker); the job leaves `pending_json` while the headset keeps downloading. Also `stop()` can block up to 60 s on in-flight downloads, and a second Ctrl+C skips the final save.
- `controller.py`: keep idle jobs in an `idle` dict (persisted, not counted toward the limit); time idleness from the last status that had `download`; add a `job` id to `sync_content`, ignore `downloads_finished` with a different id (accept when missing: old APKs).
- `app.py`: save before the first await in `stop()` and again in `finally`; `shutdown_timeout=2` on both AppRunners (aiohttp>=3.9 in pyproject.toml); SIGINT/SIGTERM via `add_signal_handler` (Unix only, guard).
- `sim.py`: echo `job`; add `throttle_bps`.
- Player: echo `job`; report progress while hashing (`ContentStore.ensure`). Document in `docs/PROTOCOL.md`.
- Test in `test_integration.py`: 4 MB video, throttled sim; once `.part` grows, `wait_for(srv.stop(), 5)`; start a new server with same data_dir and tcp_port; assert identical bytes and two `sync_content`. Variant with short grace and slow `_verify`.

## 3. No-headset warning
`Controller.execute` adds `online` to the result and `warning` when no target is online for load/play/pause/seek/stop; empty target list raises `CommandError`. `web/app.js` toasts `result.warning`. Test in `test_integration.py`.

## 4. Unity leftovers
`controller.py` default `player` becomes `""` (not sent; update `test_integration.py` ~line 84). Reword `protocol.py` comment (~line 41), keep the `"external"` value. `PROTOCOL.md` ~lines 38, 76. Kotlin comments in PlayerInfo.kt, Lifecycle.kt, Telemetry.kt, ContentStore.kt, ContentHost.kt, ExoVideoPlayer.kt. Leave `docs/*_PLAN.md`.

## 5. Player retry
`ContentStore` throws `ChecksumMismatch`; `SyncResult` gains `retryable` (not 4xx, not checksum). `ContentManager.run` retries after 5/15/30 s with an injected sleep; waits on the lock, wakes on generation change, keeps `current` set while waiting. Test in `ContentTest.kt`.

## Verify
`(cd server && python3 -m pytest -q)` and `(cd player-android && ./gradlew --no-daemon :core:test)`. Hardware: `kiosk on` (no chooser), reboot, `kiosk off`, reboot to vrshell; Ctrl+C mid-download, check state.json has `downloads`, restart, download resumes.
