# Plan: bringing the old CXVR panel's commands into SyncVR

Source: `Trigger-EX/vr-control-panel` (Tkinter, adb). Goal: consolidate its worthwhile commands here without duplicating what SyncVR has (volume, content push/delete/verify/resume, adb connect/wifi/reboot/launch/stop/shell/kiosk, telemetry, identify, recenter, scrcpy mirror) and without copying code verbatim.

## Findings that shaped the plan
- **Reboot:** the old panel never sends keyevent 26. `rebootAll.sh` runs `adb shell reboot`, `powerOff.sh` runs `shell reboot -p`. SyncVR's `adbtool reboot` (`adbtool.py:328`) is correct; port neither script. Reboot is CLI-only today; exposing it in the GUI is optional.
- **Mirror exists:** `gui/mirror.py` (`MirrorManager`) and `gui/viewpane.py` already do scrcpy. Only the multi-headset part (tiled capture-all, batch cycling, close-all) is new.
- **GUI is PySide6** (`gui/main_window.py` tabs; selection is `window.targets` / `target_spec()`). `web.py` is a JSON API only.
- **Single process:** the old panel used marker files because its daemons were separate processes. Here the brakes become in-memory controller state, which removes the stale-sentinel bug (audit M8).
- **adb hardening needed first:** `Adb.run` has no default timeout, inherits stdin, and decodes UTF-8 strictly (audit C1, L5, M6/L18).

## Architecture decisions
1. **`server/syncvr/fleetops.py` (no Qt).** `AdbFleet` wraps `Adb`: `stdin=DEVNULL`, `errors="replace"`, default timeout 20 s, one shared `ThreadPoolExecutor` capped at 10 (audit M11). `adb_serial(dev)` returns `ip:5555` if listed by `adb devices`, else USB `dev.serial`, else "not reachable by adb". `Device.saved()` also persists `ip`.
2. **Jobs, not blocking calls.** `Controller.execute` stays synchronous. New adb actions (`sleep`, `wake`, `screen_refresh`, `poweroff`, `snapshot`) start a job on the executor and return `{"job": id}`. Results return via `loop.call_soon_threadsafe` into `log_event` (one OK/FAILED line per headset) and a new `jobs` field in `snapshot()`.
3. **Server-checked confirmation tokens.** `POST /api/command/preview {action, targets, dry_run}` returns `{scope_text, labels[], token, needs_confirm}`; token = hash(action, sorted adb serials, dry_run). Confirm-required actions fail without a matching `confirm: token`. GUI shows `scope_text` ("Sleep 3 headsets: A, B, C" / "EVERY headset (72)").
4. **Dry run.** Destructive actions (power off, purge) take `dry_run`, default true: resolve and log "would ...", send nothing.
5. **Feature gating (`features.py`).** Registry key -> (category, label); tested set persisted in state.json as `tested_features`; `snapshot()` exposes `features`. Server refuses a gated action unless marked tested or the request has `testing: true` (stricter than the old panel, which only hid buttons). GUI: untested actions live in a "Testing" section with Open / Mark tested.
6. **Brakes (`automation.py`).** `Controller.brake()` returns a reason or None: `show_mode` (persisted), sync in progress (Distributor has active jobs), or CLI push lock (pid-file written by `adbtool push`, counts only while PID alive). Brakes block automation only; manual commands run but the confirmation says "Show Mode is on". Snapshot and topbar show brake state and last-run per watchdog.
7. **Watchdogs.** One asyncio task each, started from `SyncServer.start()`. Cycle: observe (adb on executor, filtered on-device) -> pure `decide(history, obs, cfg)` -> re-check brake right before each send. State: `enabled`, `armed`, `last_cycle`, counters, decision ring buffer. **Armed never persists**; restart is always observe-only. Prefer player telemetry (online, worn, desired mode) over adb polls where it has the answer.
8. **Terminal:** GUI only, POSIX only; never exposed through the web API.

## Phases (each independently shippable)
**P0 Foundation.** Harden `Adb.run`; add `fleetops.py`, `features.py`; controller jobs, `ip` in `saved()`, `preview()`, `tested_features`; web `/api/command/preview` and `/api/features/{key}`; `docs/PROTOCOL.md`. Tests: `server/tests/fakeadb/adb` (bash; logs argv to `$FAKE_ADB_LOG`, answers `devices`/`dumpsys` from `server/tests/fixtures/dumpsys/`), `test_fleetops.py`.

**P1 Sleep / Wake / Screen Refresh / Power off.**
- Sleep = keyevent 223, Wake = 224.
- Refresh: one 223, poll `dumpsys power | grep mWakefulness` until Asleep (3 s timeout), wait `min_asleep_s` (default 1 s), send 224, confirm Awake. Replaces the old 16 SLEEPs / 5 WAKEs (audit M11/L1).
- Power off = `reboot -p`, gated `power.poweroff`, dry run default, dialog lists every headset, all unticked.
- Sleep adds headsets to an "intentionally asleep" set so stay-awake and the probe leave them alone (audit L28/M3); wake clears it.
- Files: `gui/headsets.py` context menu; new `gui/tools.py` tab (Power + Testing cards); register in `gui/main_window.py`; CLI `sleep`/`wake`/`refresh` in `adbtool.py`. Tests: `test_fleetops.py`, `test_gui_tools.py` (scope wording, token mismatch, dry run).

**P2 Show Mode and brakes.** `automation.py` `brake()`; `show_mode` persisted, `"brake"` in snapshot; `POST /api/show_mode`; push pid-lock in `adbtool.py`; topbar toggle + brake pill. Tests: persistence, stale PID doesn't brake, brake reason while downloads active.

**P3 Diagnostic snapshot (read-only, gated `debug.snapshot`).** Ships before any watchdog can be armed (it collects the evidence). New `diagnostics.py`, per-command timeouts: dumpsys power/display/window windows/activity activities/audio/SurfaceFlinger (+`--list`)/thermalservice/battery; `logcat -d -t 2000`; `getprop`; optional screenshot via `adb exec-out screencap -p` (nothing written on headset, audit L14). Output `<data_dir>/snapshots/<label>_<serial>_<YYYYmmdd-HHMMSS-ffffff>/` (audit L13). Fleet concurrency 4 (audit L15). `SUMMARY.txt` from shared `parsers.py` (wakefulness, display state, focus, thermal status, battery temp), reused by watchdogs. GUI "Open folder". Tests: parsers on fixture dumps, folder layout.

**P4 Watchdogs (each observe-only and gated).** In `automation.py`; GUI rows in `gui/tools.py` with Observe/Armed pill and an arm dialog quoting the exact rule; API `POST /api/watchdogs/{name}` `{enabled, armed, cfg}`.
- *Stay-awake:* send 224 only to headsets whose wakefulness is not Awake and not intentionally asleep; default 15 s.
- *Popup/crash:* BACK (4) only when `mCurrentFocus` is a known dialog (`Application Error|Application Not Responding` + evidence pattern) over the player; verify after send, stop after 3 failures. Crash (player disconnected, app not in front) -> existing `launch` (old version sent HOME on loose matching, audit M2).
- *Overheat:* logcat since last cycle (`-T`), excluding the player's own lines, requires the prompt in the window list (audit M4). No built-in pattern; arming refused unless `pattern_confirmed` matches the pattern hash, set by "Test pattern against snapshot...".
- *Black-screen probe:* act only on display OFF + app in front + Awake + not intentionally asleep + desired mode playing (audit M3); 3 consecutive samples, 120 s cooldown; recovery wake or refresh. Display ON but black is logged to `<data_dir>/probe/*.csv`, never acted on.
- *Keepalive:* `adb connect` persisted IPs of headsets missing from `adb devices`, then wake (old one never reconnected, audit M5); each cycle catches and logs its own errors.
Tests: `decide()` table tests on fixtures, fake clock, brake suppresses send while cycle still logged.

**P5 Multi-headset capture.** `gui/mirror.py`: `start_many(devices, max_parallel=6)` with grid via `--window-x/-y/-width/-height` and `tile_geometry()` from QScreen; `BatchPreview` (pure, injected clock; groups of N, dwell, close before next); 15 fps tiles; WM class `SyncVR-batch`. View menu: Capture selected/all, Batch preview, Close all. Tests: grid geometry, batch stepping, parallel cap.

**P6 Terminal (gated `terminal.shell`).** `gui/terminal.py` (QDockWidget + QPlainTextEdit) and Qt-free `termstream.py` (escape stripping, CR/backspace, 5,000-line cap; old design was sound). `pty.openpty` + `setsid`; local `bash -i`, headset `adb -s X shell`; cleanup SIGHUP then SIGKILL to the session. Disabled on Windows. Tests: `termstream` units, pty run against fake adb.

**P7 Smaller items.** Mass connect (persisted IPs in parallel, optional subnet scan: TCP probe 5555, cap 32); purge = `adb disconnect` + mass connect, token + dry run default. Bandwidth: player-side download test via new `bandwidth_test` protocol message (recommended; matches the real HTTP content path) vs timed `adb push` — needs decision.

## Risks
- Empty `window.targets` means "all": power off must refuse "all" unless "EVERY headset (N)" wording was confirmed.
- Headsets with no IP that never connected to the player can't be adb-targeted.
- Executor callbacks must never touch controller state off the loop thread.
- Watchdog rules are unverified on real Go hardware; need P3 snapshot evidence before arming. `decide()` design deserves an opus review.
- Refresh interrupts playback (visible hitch); dialog must say so for playing headsets.
- ~70 headsets x several watchdogs can flood the show AP: use a global poll budget.

## Verify
`(cd server && python3 -m pytest -q)` with fake adb on PATH; GUI tests under offscreen Qt like existing `test_gui_*`; manual check on 2-3 headsets (refresh, brake suppressing a send, snapshot folder, tiled capture).

## Open questions
1. Auto-enable Show Mode while any headset is playing?
2. May any watchdog (e.g. black-screen probe) act while Show Mode is on?
3. Should keepalive's `adb connect` be blocked by brakes?
4. Bandwidth test: player-side (Kotlin change) or adb push?
5. Should adb-only headsets (never ran the player) appear in the GUI?
6. Which features already count as tested on real headsets?
