# Plan: resume downloads after server restart; kiosk mode

(Single-port downloads on 8765 is done; see git history.)

## Task 1: downloads resume after a server restart
Distributor queue/active lives in memory, so a restart loses it. Persist pending jobs in `state.json`.
1. `server/syncvr/controller.py`
   - `Distributor.pending_json()`: `{device_id: {"files", "delete_others"}}` for active + queue, without `started`/`bytes`.
   - Set `controller.dirty = True` in `request`, `finished`, `cancel`, `disconnected`.
   - `dump()` adds `"downloads"`; `load()` rebuilds `distributor.queue`, only for known device ids.
   - On reconnect the existing `headset_connected` -> `pump()` sends `sync_content`; the player resumes from `.part`.
2. Tests: request -> dump -> new Controller.load -> headset_connected sends `sync_content`; cancel clears it from the dump.
3. Check Ctrl+C reaches `stop()` in `serve_forever` and saves state after `headsets.stop()`.
4. Optional, needs new APK (not done now): ContentManager.kt retry on IOException with backoff.

## Task 2: kiosk mode
- Manifest: second intent-filter on MainActivity (MAIN + HOME + DEFAULT), `stateNotNeeded="true"`.
- `adbtool.py`: `kiosk {on,off}` with `--root`.
  - on: `pm disable-user --user 0 com.oculus.vrshell`, then `cmd package set-home-activity <pkg>/.MainActivity`.
  - on --root: old recipe: `adb root`, wait-for-device, `pm disable com.oculus.vrshell`, set-home, `adb unroot`, wait-for-device, set-home.
  - off: `pm enable com.oculus.vrshell`, resolve vrshell's HOME activity (`cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.HOME com.oculus.vrshell`), set-home to it.
  - Report output containing "Exception"/"Error".
- Tests in `server/tests/test_adbtool.py`: exact command lists for on, on --root, off.
- `docs/HEADSET_SETUP.md`: Kiosk section (on/off, recovery via `kiosk off` over ADB, provision Wi-Fi first, use USB for --root, kiosk relies on config.json/discovery not --usb).
- Rejected: device owner/lock task (Oculus account blocks dpm; doesn't stop Oculus button), screen pinning (2D prompt), HOME filter alone (Oculus button still exits).
- Unknown: vrshell's HOME activity name on stock firmware; run resolve-activity on a headset first.
