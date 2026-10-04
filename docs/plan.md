# Plan: native Qt operator panel, no web dashboard, self-contained packaging

## Decisions
- GUI calls `Controller` in-process on the server loop (no HTTP-to-self). `ServerThread.call(fn, *args) -> Future`; `gui/bridge.py` `Bridge(QObject)` emits `state(object)` / `result(...)`; controller listener sets a dirty flag, 250 ms QTimer pulls `snapshot_async()`, 1 s fallback. Widgets only receive snapshot dicts and call bridge methods.
- Package `server/syncvr/gui/`: `app.py` (run_window, show_error, single-instance), `bridge.py`, `main_window.py`, `headsets.py`, `library.py`, `settings.py`, `log.py`, `playback.py`, `format.py` (Qt-free, ported from web/app.js; drift <25 good, <80 meh, else poor). Delete `qt_ui.py`.
- Keep all `/api/*` routes, `/content/{name}` and the 8765 sniffer (Android operator app + headsets use them). Remove `/`, `/static`, `/ws`, the push loop and `server/syncvr/web/`; `/` returns JSON "SyncVR API". Drop `open_browser`, `--no-browser`, `dashboard_url`; status bar shows "Operator app address: ip:8080".
- Packaging: PyInstaller onedir, built natively per OS in CI (ubuntu-22.04 with xcb libs apt-installed, windows-latest --windowed, macos-14 arm64 ad-hoc signed). Published as rolling prerelease GitHub Release `desktop-<branch>`; fetched with `tools/get-desktop.sh` / `.ps1`. Source launcher stays as developer path.
- `paths.py`: frozen data in per-user dir (XDG_DATA_HOME/SyncVR, %LOCALAPPDATA%\SyncVR, ~/Library/Application Support/SyncVR), `portable` marker file next to the exe uses folders beside it; from source keep server/data and server/content. Single instance via QLockFile. `--self-test` flag: server on port 0, offscreen Qt, snapshot, exit 0.

## Steps (one commit each)
1. launcher `ServerThread.call`; `gui/format.py` + tests.
2. `gui/bridge.py`, `app.py`, `main_window.py`; launcher uses gui.app; delete qt_ui.py; `tests/test_gui_window.py`; `qapp` fixture in conftest.
3. `gui/playback.py` + tests (FakeBridge).
4. `gui/headsets.py` (cards, group filter, show offline, multi-select, edit dialog/forget) + tests.
5. `gui/library.py` (table, editable projection/stereo/rotation/loop, on-headsets count, rescan) + tests.
6. `gui/settings.py` (SETTINGS_SPEC, defaults, max downloads), `gui/log.py` + tests.
7. `tests/test_gui_integration.py` with real ServerThread + sim headsets.
8. Remove dashboard (web.py, web/, pyproject package-data, __main__ help, launcher, app.py log line, test_integration `/` `/ws` asserts, test_dashboard_url).
9. `paths.py`, single-instance, choose-content-folder, `tests/test_paths.py`.
10. `packaging/syncvr.spec`, `packaging/entry.py`, CI `desktop` matrix job with `--self-test`, release upload.
11. `tools/get-desktop.sh|.ps1`; README, EXECUTION_PLAN, bootstrap messages.

## Risks
Thread safety (bridge-only access); in-place card updates (no rebuild); don't clobber user edits during refresh; Linux bundle libs; macOS arm64 only + Gatekeeper; Windows SmartScreen; existing server/data not found by frozen app; keep /api stable for Android e2e.

## Cannot verify here
Seeing the GUI, Mint desktop launch, SmartScreen/Gatekeeper, HiDPI, real headsets, Windows/macOS builds (CI only).

---

# Plan: default view type per video (no auto-cycling)

Server already has `guess_format()`, editable `projection`/`stereo`, persistence. Player ignores `cmd.projection/stereo` and cycles 4 modes every 15 s.

1. **Detection** (`server/syncvr/library.py`, `tests/test_library.py`): `guess_format` returns only matched fields; add `guess_from_resolution(w,h)` (2:1 -> 360 mono, 1:1 -> 360 tb, 4:1 -> 360 sbs, 1.6-1.9 -> flat mono, ~3.55 -> flat sbs, else 360 mono). Precedence: operator value > filename > resolution > default. Add non-editable `format_source` to `Video`/`to_json`. `update_meta` merges only changed keys; value `"auto"` for projection/stereo removes the stored key and re-detects.
2. **Live view change** (`controller.py`, `protocol.py`, `docs/PROTOCOL.md`, `tests/test_controller_state.py`): `update_video` changing projection/stereo/rotation updates `desired` and sends `{"type":"view","video","projection","stereo","rotation"}` to devices playing that video.
3. **GUI** (`gui/library.py`, `gui/playback.py` + tests): "Auto (detected: X)" choice and `format_source` tooltip; a "View" combo in the playback panel that sends `bridge.update_video`.
4. **Core Kotlin**: `ViewSpec(layer, stereo, half)` + `from(projection, stereo)`; parse `view` into `PlayerHost.onViewCommand`; tests.
5. **Native/app**: `nativeSetMode(handle, mode, stereo, half)`; SBS and 180 support in layers; `MainActivity` drops `modeTick` unless `--ez cycle_modes true`; apply view from commands. Unverified without hardware.
6. **Docs**: HEADSET_SETUP / EXECUTION_PLAN: cycle is a debug option.

Risks: 2:1 can be 360 mono or 180 SBS (filename token decides); native changes unverified on hardware; stored metadata for old videos counts as "operator".
