# Execution plan

How the phases in [IMPROVEMENT_PLAN.md](IMPROVEMENT_PLAN.md) will be carried out, step by step.
Phase 0 (testing the Unity build) is skipped by choice; the Unity app stays in the repo as a
fallback until the native player has proven itself.

## How the work gets built and checked

| What | Where it is built and tested |
|---|---|
| Kotlin sync engine, protocol and networking (`player-android/core`, plain Kotlin) | In my environment **and** in CI: JVM unit tests, plus an end-to-end test where the Kotlin client joins the real Python server. |
| The Android app (Kotlin + C++ + ExoPlayer) → `SyncVRPlayer.apk` | **GitHub Actions** (my environment can't download Google's Android SDK). I push, CI builds, I read the logs and fix until green. Every successful build attaches the APK to the run for you to download. |
| Server features (content pipeline, USB copy, OTA, …) | In my environment and CI: pytest, plus real `ffmpeg` runs on small generated videos. |
| Anything you see or hear in a headset | **You**, at the checkpoints below. I can't run code on a Go. |

Work continues on the branch `claude/adoring-curie-qnd0ib`, with one commit per step and CI on every
push. I can open a pull request per phase instead if you prefer.

## Before starting: two decisions for you

1. **App signing.** Android only lets an update install over an existing app if both are signed
   with the same key, and CI machines are wiped between builds, so the key must be kept somewhere:
   * **Default:** I generate a signing key and commit it to the repo. Simple, and fine for a private
     repo, but anyone with repo access could sign an app that installs over yours.
   * **Safer:** you add the key as a GitHub Actions secret (about 2 minutes in the repo settings;
     I'll give exact steps). The key never appears in the code.
2. **Package name.** The native app reuses `com.syncvr.player` and the same video folder, so
   `syncvr adb` and anything already pushed to headsets keep working. That means it replaces the
   Unity app rather than installing alongside it. Since you haven't installed the Unity build, I
   assume that's fine.

## Phase 1A: player skeleton and the first hardware check

Goal: answer the risky questions about the Go before building everything on top of them.

1. **Project setup.** Create `player-android/` (Gradle, Kotlin, minimum Android 7.1 / API 25,
   32-bit ARM). It has two modules: `core`, plain Kotlin for logic that is testable anywhere, and
   `app`, the Android app.
2. **Meta SDK.** CI downloads Oculus Mobile SDK **15.0 (VrApi 1.32)**; the Go's final OS (VrApi
   1.1.35) rejects newer loaders, from a public mirror pinned to an exact commit and checksum. It is never
   committed to the repo.
3. **VR loop in C++** (`app/src/main/cpp`). Enter VR mode, run the frame loop, create an Android
   surface swapchain, and submit compositor layers: equirect (360/180, stereo via per-eye halves),
   cylinder (flat screen) and a small text panel.
4. **Video.** ExoPlayer (Media3) decodes the file straight into the swapchain's surface.
5. **Text panel.** Drawn with Android's `Canvas` into a second surface layer.
6. **Diagnostic mode.** The skeleton plays the first video in the headset's video folder and cycles
   every 15 s through: equirect layer → fallback sphere drawn by the app → flat cylinder → stereo
   top/bottom. Each mode is labelled on screen. It also logs VrApi and system versions, decoder
   limits (H.264/HEVC maximum sizes) and supported refresh rates to `logcat`.
7. **CI.** A new job builds the APK (Android SDK and NDK come preinstalled on GitHub's runners),
   signs it per decision 1, and uploads it as a downloadable artifact.

**Checkpoint 1 (you, about 15 minutes, one headset):** install the APK with
`syncvr adb setup`, copy any 360 video over with `syncvr adb push`, and tell me which modes looked
right. Also send the `adb logcat` lines tagged `SyncVR`. This decides whether the main path is the
compositor layer or the fallback sphere.

**Checkpoint 1 results (2026-10-03, Go on its final OS, VrApi 1.32, 2880×1440 mono H.264 video):**
* All four modes showed video at a steady ~60 fps. Equirect layer (mode 1) and sphere fallback
  (mode 2) both looked like normal playback. Mode 1 becomes the main path, and mode 2 is kept as a fallback.
* Cylinder (mode 3): a flat screen in front of the viewer, as intended.
* Stereo top/bottom (mode 4) with a mono video: one eye saw the sky and the other the ground. This is
  expected, because each eye gets half of a mono frame. The eye order still needs a real top/bottom 3D video.
* Hardware decoders (AVC and HEVC) accept up to 4096×2048 at 30 fps, but not 5120×2560. This
  matches the defaults in `server/syncvr/limits.py`.
* Sleep/wake and the Oculus button: VR mode is left and re-entered cleanly, and playback resumes
  locally. In 1B it must instead resync to the operator's position (step 7 below).

## Phase 3, part 1 (while waiting for checkpoint 1): content checks and checksums

Server-only work, so nothing sits idle while you test. Pulled forward from phase 3.

1. **ffprobe analysis** of every library video: codec, profile, level, resolution, frame rate,
   keyframe interval, audio codec, whether `moov` is at the front. Problems show in the Library tab,
   checked against the Go's limits from checkpoint 1 (initially conservative defaults).
2. **Checksums.** SHA-256 per file, computed once and cached by size and modification time. Sent
   with `sync_content`; headsets verify after downloading and re-fetch on mismatch. SHA-256 is
   built into both Python and Android, so no new dependencies are needed.
3. Tests with tiny videos generated by `ffmpeg` (installed in CI).

## Phase 1B: the full native player

Built on whichever display path checkpoint 1 confirmed.

1. **Port to Kotlin (`core`):** clock sync, sync engine, protocol messages and JSON. The same
   scenario tests that the Python and C# engines pass are run against it.
2. **Networking (`core`):** UDP discovery (with an Android multicast lock), TCP connection with
   auto-reconnect, and a dedicated ping thread with timestamps taken on receipt.
3. **Player backend:** ExoPlayer behind the same `IVideoPlayer` interface as the Unity app, with
   exact seeks, pitch-preserving speed, audio-clock position and loop support.
4. **Content:** resumable HTTP downloads, checksum verification, inventory and deletion. Same
   folder as before, so `adb push` still works.
5. **Telemetry:**
   * battery and temperature (`BatteryManager`)
   * free storage
   * Wi-Fi signal
   * worn or not (VrApi "mounted" status)
   * frame rate.
   Plus a high-performance Wi-Fi lock.
6. **Operator commands:**
   * recenter (rotates the layers to the viewer's current direction)
   * volume
   * on-screen messages
   * identify (name plus beep)
   * idle screen showing name and connection state
7. **Lifecycle:** leave VR mode on pause and re-enter on resume, then resync the clock and re-cue
   playback. Calibration (start latency and seek time) is saved between runs.
8. **Protocol:** unchanged, apart from `hello` reporting `player: "native"` so the dashboard shows
   which app each headset runs.
9. **End-to-end test in CI:** the Kotlin client, with a simulated decoder, joins the Python server,
   follows play/pause/seek and stays in sync. This proves protocol compatibility without a headset.
10. **Docs:** a new headset guide with no Unity steps: download the APK from GitHub and run
    `syncvr adb setup`.

**Checkpoint 2 (you, 2–3 headsets, about 1 hour):** the full checklist. Discovery, push video, load,
play, pause, seek, recenter, identify, sync by ear with headsets side by side, Wi-Fi drop and
rejoin, and a full-length video while watching temperature and drift on the dashboard.

## Phase 2: sync polish (uses checkpoint 2 data)

1. **Frame-accurate measurement.** ExoPlayer reports when each video frame is actually released
   to the display, and Android reports the audio output position. The headset reports drift
   measured on these instead of the decoder's position, and the dashboard shows it.
2. **Sync test clip.** The server generates (with `ffmpeg`) a video with a large frame counter,
   a white flash and a click every second. In a room you can hear any offset, and filming several
   headset lenses with a phone shows frame differences.
3. **Retune defaults** from your measurements: pitch-preserving rate correction as the norm, a
   smaller deadband (target under ~14 ms, one display frame), and fewer hard re-syncs.
4. **Only if the numbers still fall short:** schedule every decoded frame directly
   (`MediaCodec.releaseOutputBuffer` with a computed display time) with the audio start aligned to
   the shared clock. This removes start-up guesswork. I'll check with you before doing this,
   since it's the largest optional piece.

**Checkpoint 3 (you):** run the sync test clip on several headsets; report what you hear and see.

## Phase 3, part 2: re-encoding and USB copying

1. **"Optimize for Go" button.** A background `ffmpeg` job re-encodes a video within the Go's
   decoder limits (confirmed at checkpoint 1): H.264 High (HEVC optional), 1-second keyframes, AAC
   audio, `moov` at the front. It shows progress, writes `name.go.mp4`, keeps the original, and
   marks the original as source-only so it isn't pushed to headsets. Requires `ffmpeg` on the server
   machine (`sudo apt install ffmpeg`); the button is hidden if it's missing.
2. **USB copying from the dashboard.** The server lists headsets plugged into its USB ports,
   matches them to their cards by serial number, copies selected videos with `adb push`, and shows
   progress on the card.

## Phase 4: fleet operations

1. **Start on boot:** the app launches when the headset powers on. To verify on the Go; if it
   doesn't work there, I'll document the nearest alternative.
2. **Over-the-air app updates:** upload a new APK in the dashboard. Headsets download it, check
   its checksum, and Android asks for one confirmation inside each headset. Fallback: install over
   Wi-Fi ADB, driven by the server.
3. **Display settings from the dashboard:** 60/72 Hz refresh, and brightness if the Go allows it
   (may need a one-time permission granted via `syncvr adb setup`).
4. **Pre-show panel:** low batteries, missing or failed content, headsets running old app
   versions, offline headsets; cards sortable by battery.
5. **Server packaging:**
   * a `systemd` service and Raspberry Pi guide
   * single-file server downloads for Windows, macOS and Linux, built by CI
   * an installable dashboard for tablets (web-app manifest)

**Checkpoint 4 (you):** reboot a headset (does it start SyncVR?), push an app update, switch to 60 Hz.

## Phase 5: retire Unity

After the native player has run real shows: tag the last Unity version (`unity-player-final`),
delete `headset/`, remove its CI job and update the docs. The Python sync engine stays as the
reference specification, with the Kotlin engine tested against it.

## Rough size

| Phase | Relative size | Your time |
|---|---|---|
| 1A skeleton | large (most of the unknowns) | ~15 min test |
| 3.1 content checks | small | none |
| 1B full player | largest | ~1 hour test |
| 2 sync polish | medium (larger if step 4 is needed) | ~30 min test |
| 3.2 re-encoding, USB | medium | quick check |
| 4 fleet operations | medium | ~30 min test |
| 5 retire Unity | small | none |

## What I need from you

* Decisions 1 and 2 above (or "use the defaults").
* Hardware checkpoints when I ask, with `adb logcat` output for anything that goes wrong.
* GitHub Actions enabled on the repo (already the case).
