# Plan: headset panel edges, Android operator app, PC launcher

Dashboard API lives in `server/syncvr/web.py`; static UI in `server/syncvr/web/`. Three independent tasks, no shared files.

## A. Headset panel edges
Cause: the panel is a 1024x256 Android-surface swapchain drawn by `PanelRenderer.kt` and shown as an opaque cylinder layer with `CLIP_TO_TEXTURE_RECT` (layers.cpp, app.cpp). The box outline is the hard clip boundary, which the compositor redraws without anti-aliasing every frame. The small panel is minified about 3x with no mipmaps, so the 4 px border and thin strokes shimmer. Colour fringes through the lenses are likely too.
Fix:
1. `core/.../PanelStyle.kt` (pure Kotlin): texture size, transparent margin (~8% per side), corner radius, border width, text sizes; helper `texelsPerDisplayPixel(widthM, radiusM, texW, pxPerDeg = 14.0)`. Test `PanelStyleTest.kt`: ratio <= 2.0 for small (1.2 m) and prominent (2.4 m) placements, margin >= 2 texels.
2. `PanelRenderer.kt`: clear to transparent, anti-aliased rounded rect with soft edge inside the margin, border >= 6 px, sizes from PanelStyle.
3. `app.cpp`: panel 768x192 (or per PanelStyle), `opaque=false`; keep a `kPanelAlpha` constant to switch back.
4. `layers.cpp/.h`: non-opaque layers use premultiplied blend (SRC=ONE, DST=ONE_MINUS_SRC_ALPHA); add chromatic aberration correction flag on the panel layer only (check name in VrApi_Types.h, `#ifdef`). Keep CLIP_TO_TEXTURE_RECT.
Risks: surface may lack alpha (margin shows black); CA costs compositor time; cannot compile here, CI builds, only the headset confirms the look.

## B. Android operator app (native, no WebView)
Separate module `player-android/operator` (applicationId `com.syncvr.operator`, minSdk 24, phone/tablet, no native code).
API (HTTP Basic auth if a password is set): discovery by UDP 8766 beacon `{type:"beacon", service:"syncvr", server_name, tcp_port, http_port}` (host = datagram sender) with manual IP:port fallback; `GET /api/state` (server, settings, devices, library, downloads, events); `POST /api/command {action, targets, ...}` with actions load, play, pause, seek, stop, resync, volume, recenter, message, identify, sync_content, delete_content, cancel_downloads; `POST /api/devices/{id}`, `DELETE /api/devices/{id}`, `POST /api/library/rescan`. v1 polls `/api/state` every 1 s in the foreground (no WebSocket client).
Steps:
1. Logic in `core/src/main/kotlin/com/syncvr/player/core/operator/`: `OperatorApi.kt` (HttpTransport + HttpURLConnection impl), `StateModel.kt`, `ServerFinder.kt`, `OperatorViewModel.kt`.
2. Tests in `core/src/test/.../operator/`: snapshot parse, command bodies, auth header, beacon matching, end-to-end against the Python server (pattern of e2e/EndToEndTest.kt).
3. `operator/build.gradle.kts` (same signing/versionCode as app, no cmake, framework widgets only), manifest (INTERNET, ACCESS_WIFI_STATE, CHANGE_WIFI_MULTICAST_STATE, usesCleartextTraffic), `MainActivity.kt` (connect), `DashboardActivity.kt` (devices, transport, library/distribution, events), `MulticastLockGuard` copy.
4. `settings.gradle.kts`: `include(":operator")` inside the hasAndroidSdk block.
5. `ci.yml`: build `:app:assembleRelease :operator:assembleRelease`, publish `SyncVROperator.apk` with apksigner verify, add to apk/<branch>.
6. `tools/get-apk.sh`: `--operator`; default flow unchanged; tolerate branches without the file. `.gitignore`, `docs/HEADSET_SETUP.md`.

## C. PC control panel launcher
tkinter window (stdlib) running the server in a background thread, opening the browser once. A second launch with port 8080 in use opens the browser and exits.
1. `server/syncvr/launcher.py`: `ServerThread` (own loop, start waits for ready, stop(timeout=5), snapshot via run_coroutine_threadsafe), pure `status_lines`, `port_in_use`, `setup_logging` (rotating `data/logs/syncvr.log`); Tk UI with 1 s refresh and buttons Open dashboard / Open content folder / Open log / Stop & quit; console fallback without tkinter; message box with "Install now" if aiohttp missing. Optional `data/launcher.json`.
2. `__main__.py`: `gui` subcommand.
3. Repo root: `Start SyncVR.desktop` (executable), `start-syncvr.sh`, `Start SyncVR.pyw`; README notes (Nemo "Trust and launch", WSL users use the .pyw from Windows).
4. `server/tests/test_launcher.py`: ServerThread start/snapshot/stop < 5 s with port 0 and discovery off; status_lines; port_in_use; import without tkinter.
Constraints: aiohttp 3.8 compatible (`runner_kwargs`), no signal handlers off the main thread, `controller.snapshot` only on the loop thread.

## Verify
`(cd server && python3 -m pytest -q)`, `(cd player-android && ./gradlew --no-daemon :core:test)`, CI build; hands-on: headset panel while turning head, operator APK on a phone, double-click launcher on Mint.
