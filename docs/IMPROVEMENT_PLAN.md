# Improvement plan: better paths to the same goal

The goal stays the same: a fleet of Oculus Go headsets playing local MP4s in tight sync,
driven from a tablet or PC over local Wi-Fi. This document reviews each part of the current
implementation against better alternatives and proposes a plan. **Nothing here is built yet.**

## Summary

* **The biggest win is on the headset: replace Unity with a small native Android app** (Kotlin +
  a thin C++ layer over Oculus's VrApi) that decodes video with ExoPlayer directly into a VR
  compositor layer. That gives a sharper picture, much less GPU work and heat, inaudible sync
  corrections, and APKs built automatically by CI. No more Unity 2019 installs.
* No other engine beats Unity on the Go. Godot, Unreal and WebXR are all worse for this job;
  "no engine" is the better path.
* The server, dashboard, protocol and sync design are sound and carry over unchanged. The new
  headset app speaks the same protocol, so both apps can run side by side during the switch.
* Worthwhile server additions: automatic re-encoding of videos into a Go-friendly format, file
  checksums, and over-the-air app updates.
* Test the current Unity build first (phase 0). It validates the server, network and sync
  design on real hardware, and gives a baseline to measure the native app against.

## 1. Headset engine and runtime

The Go runs Android 7.1 on a Snapdragon 821 and only supports Oculus's legacy **VrApi**, not
OpenXR. Its final OS ships VrApi 1.1.35 and refuses newer loaders with "Oculus Update Required",
so the player builds against Mobile SDK **15.0 (VrApi 1.32)**, the newest release at or below that.

| Option | Verdict |
|---|---|
| **Unity 2019.4 (current)** | Works, but it is a whole game engine running just to show one texture. Its video player adds an extra full-frame GPU copy, and playback-speed changes are unreliable on Android. The editor is hard to install on modern Linux, and headless builds need a Unity licence in CI. |
| Unity + Oculus Integration overlays + ExoPlayer plugin | Gets most of the picture and performance benefits below, but keeps all the Unity baggage and needs the legacy Oculus Integration package plus a custom Java plugin. A fallback if the native route hits a wall. |
| **Native: Kotlin + C++ VrApi + Media3 ExoPlayer** | **Recommended.** Nothing runs except the decoder, the compositor and our sync logic. Small APK, full control over timing and audio, plain Gradle build, easy CI. |
| Godot 3 + Oculus Mobile plugin | Supports the Go, but Godot only plays Ogg Theora in software, so it can't do 4K H.264. Rejected. |
| Unreal 4.2x | Supports the Go, but it is heavier than Unity with no video advantage (uses Android MediaPlayer). Rejected. |
| WebXR in the Oculus Browser | Every viewer has to tap "Enter VR". There is no background control, and browser storage limits make keeping multi-GB videos offline unreliable. Rejected. |

The native app's structure also carries over to Quest later. OpenXR has the same building
blocks (Android surface swapchains and equirect layers), so only the thin VR layer would change.

## 2. Getting video onto the display

**Now:** the decoder outputs to an external texture. Unity copies it into an RGBA texture, draws
it onto a sphere in each eye buffer (about 1024–1280 px per eye), and the compositor then warps
those eye buffers onto the display. The image is resampled twice and the GPU works every frame.

**Better:** the decoder writes straight into a **VrApi Android-surface swapchain**
(`vrapi_CreateAndroidSurfaceSwapChain`), which is shown as a compositor layer:

* 360° and 180°: `VRAPI_LAYER_TYPE_EQUIRECT2`, with stereo handled by giving each eye its half
  of the frame.
* Flat screen: `VRAPI_LAYER_TYPE_CYLINDER2`.
* Text overlays (status, messages, identify): a second small surface layer drawn with Android's
  ordinary `Canvas`, so no text rendering code is needed.

The compositor samples the video once, at display resolution, during its final distortion
pass. Oculus recommended layers for video for exactly this reason: noticeably sharper
picture, and the app renders almost nothing, so less heat and longer sessions before thermal
throttling. The app can also offer **60 Hz** display mode, which matches 30 fps video
evenly and saves more power.

*Risk:* whether the Go's final system software supports the equirect layer has to be checked on
a headset in the first week. Fallback: draw the sphere ourselves by sampling the decoder's
external texture directly. That keeps the no-copy win and loses only part of the sharpness gain.

## 3. Playback control and sync precision

**Now:** Unity's `VideoPlayer.time` only changes once per video frame (33 ms steps at 30 fps),
so drift has to be averaged. Speed changes may freeze the video or glitch audio (the app already
falls back to seek-only correction automatically). Start-up latency has to be learned per
headset.

**Better, with ExoPlayer (Media3):**

* **Pitch-corrected speed changes.** `PlaybackParameters(speed)` time-stretches audio without
  changing pitch, so ±2–5% corrections are practically inaudible. Rate correction can become the
  normal mode and hard re-syncs rare.
* **Exact seeks** with `SeekParameters.EXACT`, plus a position read from the audio clock
  (sub-millisecond) instead of the last decoded frame.
* **True on-screen timing.** The video-frame metadata listener reports the exact moment each
  frame is released to the display, so drift is measured on what the viewer actually sees.
* **Optional later step:** drive `MediaCodec` and `AudioTrack` directly and hand every frame to
  the OS with `releaseOutputBuffer(index, renderTimeNs)`, computed from the shared clock.
  Frames would then appear at scheduled times with no start-up guesswork. Only worth doing if
  ExoPlayer measurements show a need.

**Clock:** the current NTP-style method (about 1 ms on a LAN) stays. The native app would
use `SystemClock.elapsedRealtimeNanos()`, which keeps counting while the headset sleeps, so no
re-measurement is needed after waking. Moving pings from TCP to UDP is a possible minor
refinement, but only if measurements show retransmission noise.

The sync engine itself (anchors, cueing, learned delays, drift correction) is ported to Kotlin
unchanged, and the same scenario tests that already run against the Python and C# versions run
against it on the JVM.

## 4. Discovery and control protocol

The design is sound: UDP beacon plus a fixed-address fallback, one TCP connection per headset,
JSON lines, and a desired-state model with automatic rejoin. I considered mDNS: it
needs the same multicast support on the router as the beacon and adds nothing here. **No
change**, apart from small protocol additions for the new features below (all backwards
compatible, so the Unity app keeps working during the switch).

## 5. Content preparation (new)

Most playback problems on the Go come from files it can't decode smoothly: too large, wrong
profile, long keyframe intervals that make seeks slow, or audio formats it doesn't support.

* The server probes each video with `ffprobe` and flags problems in the Library tab ("5760×2880
  is above the Go's decoder limit", "keyframes every 10 s: seeks will be slow").
* An **"Optimize for Go"** button re-encodes with `ffmpeg` in the background: resolution capped
  for the Go, H.264 High or HEVC, 1-second keyframe interval, AAC audio, moov atom at the
  front. The original is kept.
* Optional: a fixed quality ladder, for example a reduced-bitrate version for older headsets.

## 6. Content distribution

**Now:** headsets download over HTTP with resume, a few at a time; USB `adb push` for bulk.
That is the right design for a shared Wi-Fi channel. Alternatives I rejected:

* **Multicast file transfer** (send once to all headsets): Wi-Fi sends multicast at the lowest
  basic rate unless the access point converts it to unicast, so it is usually slower. Rejected.
* **Peer-to-peer between headsets:** all headsets share the same airtime, so it adds no capacity.
  Rejected.

Additions:

* **Checksums.** The server computes a fast hash (xxHash64) once per file, and headsets verify
  after downloading, instead of trusting the file size. A corrupt file is re-downloaded
  instead of failing mid-show.
* **Bulk USB mode in the dashboard.** The server sees headsets plugged into its USB hub and
  copies content to them at USB speed, showing progress on the same cards.

## 7. Fleet operations (native app makes these possible)

* **Start on boot.** Power the headset on and it goes straight into SyncVR. (To verify on the Go;
  the fallback is a single launcher shortcut.)
* **Over-the-air app updates** from the server. Android on the Go will still ask for one
  confirmation inside each headset. The alternative is Wi-Fi ADB, driven from the dashboard.
* **Display mode** (60/72 Hz) and a per-show brightness setting, pushed from the dashboard.
* Charging overview: cards sorted by battery level, plus warnings before a show.

The Go has no device-owner or kiosk mode, so fully locked-down kiosk behaviour isn't possible
without factory-level provisioning. These features get as close as the platform allows.

## 8. Build and release

**Native app:** a Gradle project, built in GitHub Actions on every push. Each commit gets a
ready-to-install `SyncVRPlayer.apk` as a download, so no one has to install Unity.

The Mobile SDK 15.0 headers and loader library are under Meta's SDK licence. They will be
downloaded at build time (from Meta, or a public mirror of that exact version) rather than
committed to the repo.

## 9. Server and dashboard

Python/aiohttp is appropriate for this load: hundreds of headsets, a few messages per second
each. Optional conveniences:

* A `systemd` service and a Raspberry Pi setup guide, so a small box can live with the router.
* A single-file download of the server for Windows or Mac operators (PyInstaller).
* Installable dashboard (web-app manifest), so it opens full-screen from a tablet's home screen.

## Plan

| Phase | What | Result / acceptance |
|---|---|---|
| **0. Baseline** (you) | Test the current Unity build on 2–3 Go headsets using the checklist in `HEADSET_SETUP.md`. Record drift, temperature and fps over a full-length video. | Server, network and sync design confirmed on hardware; numbers to beat. |
| **1. Native player** | New `player-android/` Gradle project: Kotlin app, C++ VrApi loop (SDK 15.0), ExoPlayer into surface-swapchain equirect/cylinder layers, Canvas overlay layer, Kotlin port of the sync engine with the shared scenario tests, same protocol. CI builds the APK. | Passes the same checklist as the Unity app; side-by-side comparison of sharpness, temperature and drift. |
| **2. Sync polish** | Frame-release metrics, pitch-corrected rate as the default, exact seeks, retuned defaults from phase 1 measurements. Raw MediaCodec/AudioTrack scheduling only if the numbers call for it. | Drift under one display frame (about 14 ms) across a room of headsets, with no audible corrections. |
| **3. Content pipeline** | `ffprobe` checks, "Optimize for Go" re-encoding, checksums, bulk USB copy from the dashboard. | Any file you drop in either plays well or tells you why it won't. |
| **4. Fleet operations** | Start on boot, OTA updates, 60/72 Hz and brightness from the dashboard, charging overview, Raspberry Pi guide. | Headset powers on into SyncVR; updates without cables. |
| **5. Retire Unity** | Once the native app has run real shows, remove `headset/`, or keep it as a reference. | One headset codebase. |

Phases 1 and 3 are independent, so they can proceed in parallel. Each phase ends with a
build for you to test on headsets; I can't test on hardware myself.

## Risks and open questions

* **Equirect layer support on the Go's final firmware.** Checked in week one of phase 1, with the
  eye-buffer fallback described in section 2.
* **SDK availability and licence.** Meta still lists legacy downloads, and public mirrors of the
  Mobile SDK exist. The build fetches it rather than redistributing it.
* **Developer mode on Go headsets in 2026.** Needed for any sideloaded app, Unity or native.
  If your headsets were set up to run Headjack Link, it is probably already enabled.
* **Hardware testing.** Every phase needs a round of real-headset testing by you, like the
  current build.
