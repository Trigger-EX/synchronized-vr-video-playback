# Headset setup (Oculus Go)

The headset app is the native player in [`player-android/`](../player-android): Kotlin and C++
on Oculus VrApi 1.32, with ExoPlayer for video. Its package name is `com.syncvr.player`, so it
replaces the old Unity app on a headset and uses the same video folder.

**No Unity is needed.** The APK is built by CI; you only need `adb`, Python 3.8+ and this repo.
The player discovers the server by UDP broadcast, connects, follows play/pause/seek on the shared
clock, downloads content, reports telemetry and shows its name and connection state while idle.
It has not yet been checked on hardware (see [Checkpoint 1](#checkpoint-1-hardware-check) and
Checkpoint 2 in [EXECUTION_PLAN.md](EXECUTION_PLAN.md)). The old Unity app is legacy: see
[UNITY_PLAYER.md](UNITY_PLAYER.md). Both use the package `com.syncvr.player`, so installing one
replaces the other.

**Quick path:** download the APK (below), then with headsets on USB, from `server/`:

```bash
cd server && python3 -m pip install -e .          # use a venv (python3 -m venv .venv) if pip is externally-managed
python3 -m syncvr adb setup ../SyncVRPlayer.apk   # install, write config, proximity off, launch
python3 -m syncvr adb push ../content/*.mp4       # optional: USB preload
python3 -m syncvr serve                           # headsets appear in the dashboard
```

## Getting the APK

Nothing needs building on your machine. GitHub Actions builds and signs the APK on every push
and publishes it to the git branch `apk/<branch>` (one commit, replaced on each build).

With git, once per clone, add an alias:

```sh
git config alias.apk '!player-android/tools/get-apk.sh'
```

Then, after each push has gone green, from your checkout of the branch:

```sh
git pull
git apk              # writes ./SyncVRPlayer.apk (repo root) and prints which commit it was built from
git apk --install    # same, then adb install -r onto the connected headset
git apk --launch     # install, then (re)start the app on the headset
git apk main         # the APK of another branch
```

If the branch has commits newer than the published APK, `git apk` says so (CI still running or
failed). Without git: on GitHub → **Actions** → the latest green **CI** run on the branch →
**Artifacts** → **SyncVRPlayer-apk** (a zip containing `SyncVRPlayer.apk`).

Every build is signed with the same key (stored in the repository secrets), so a newer APK
installs over an older one without losing videos.

## Preparing headsets

Each Oculus Go needs **developer mode** once (Oculus/Meta phone app › Devices › the
headset › Developer Mode; this requires a developer organization on the Meta developer
site). Then connect it over USB and accept the *Allow USB debugging* prompt inside the headset.

With [Android platform-tools](https://developer.android.com/tools/releases/platform-tools)
(`adb`) installed, from `server/`:

```bash
python3 -m syncvr adb devices                            # list headsets, battery, temperature
python3 -m syncvr adb setup ~/Downloads/SyncVRPlayer.apk # install + config + prox-off + launch
python3 -m syncvr adb push ../content/*.mp4              # copy videos over USB (4 headsets at a time)
python3 -m syncvr adb list                               # what's on each headset
```

Every command runs on all connected headsets in parallel; add `-s SERIAL` to target one.
Other commands: `install`, `configure`, `launch`, `stop`, `prox-off`, `prox-on`, `wifi`
(switch to Wi-Fi ADB), `connect IP…`, `reboot`, `shell CMD…`.

* **Proximity sensor.** `setup` runs `prox-off` so the headset keeps playing when nobody is
  wearing it (useful while preparing a room). This lasts until the headset reboots; use
  `--keep-proximity` to skip it.
* **Where videos live.** `/sdcard/Android/data/com.syncvr.player/files/videos/`. Reinstalling
  with `adb install -r` (what `install`/`setup` do) keeps them; *uninstalling* the app deletes them.
* **Fixed server address.** `python3 -m syncvr adb configure --server 192.168.1.10` (or
  `--server-name "Room A"`, or `setup --server ...`) writes `config.json` into the app's files folder.
  The native player does **not** read this file yet: it always uses UDP broadcast discovery, so
  broadcast must reach the headsets (no client isolation; `serve --broadcast` for other subnets). The
  file only applies to the legacy Unity app.

Once connected, the player shows its name and connection state on a dark screen while idle. The
dashboard shows `native` as the player type. Headsets use their Android serial number as their ID,
the same serial `adb devices` shows, so it's easy to match a physical headset to its dashboard card.

## Checkpoint 1 (hardware check)

The player still cycles through four ways of showing a video (it keeps doing so while connected). The goal is to
find out which display path works on the Go. It takes about 15 minutes with one headset.

### Install and copy a video

After installing the APK as above, from `server/`:

```bash
python3 -m syncvr adb push my_360_video.mp4   # any 360° equirectangular video
python3 -m syncvr adb launch                  # restart so it picks up the video
```

The player plays the **first video, alphabetically**, in
`/sdcard/Android/data/com.syncvr.player/files/videos/` (`.mp4`, `.mkv`, `.webm`, `.mov`).
A mono 360° video is the most useful. If you also have a top/bottom 3D 360° video, test it as
a second run (rename it so it sorts first, or push it alone).

### Watch the four modes

Put the headset on. Every **15 seconds** it switches to the next mode and loops. A small text
panel in front of you names the current mode (for example "1/4 Equirect layer (mono)"):

| # | Panel label | What you should see if it works |
|---|---|---|
| 1 | Equirect layer (mono) | The 360° video all around you, sharp, not mirrored, horizon level. |
| 2 | Sphere fallback (app-rendered) | The same 360° view, drawn by the app instead of the compositor. Compare sharpness and smoothness with mode 1. |
| 3 | Cylinder layer (flat screen) | The video as a flat, slightly curved screen in front of you. |
| 4 | Equirect layer (stereo top/bottom) | With a 3D top/bottom video: depth, each eye seeing its own half. With a mono video, the image looks squashed vertically, which is expected. |

For each mode, note:

* Is the picture there at all (or black, frozen, flickering)?
* Is it upside down, mirrored, or rotated (where is the "front" of the video)?
* Is it smooth when you turn your head, or does it judder or tear?
* Is the text panel readable, and where is it (too close, too far, off to one side)?
* Is there sound?

### Send the log

Leave it running through at least two full cycles (about 2 minutes), then:

```bash
adb logcat -d -s SyncVR > syncvr-checkpoint1.txt
```

Send that file along with your notes per mode. The log has the VrApi and system versions,
the H.264/HEVC decoder size limits, supported refresh rates, and any errors.

### What this checkpoint settles

Things I couldn't verify without a headset:

* **Main display path:** compositor equirect layer (mode 1) or app-drawn sphere (mode 2).
  Whichever looks better becomes the main path in phase 1B.
* **Orientation:** texture origin and which half is the left eye in stereo (mode 4).
* **Cylinder size and placement** (mode 3), and the text panel's distance and size.
* **Decoder limits:** the largest H.264/HEVC sizes the Go accepts. These replace the
  conservative defaults in the Library tab's checks.
* **Pausing:** take the headset off, wait for it to sleep, put it back on. Does the video come
  back (it may restart from the beginning)? Also try the Oculus button, then return to the app.

If the app shows nothing at all, send the log anyway; it says which step failed.

Known suspects, so you know what to look for:

* **Panel not visible:** its placement is a guess. Look down, up and behind you; it may also
  be upside down.
* **Stereo eyes swapped** in mode 4: depth looks inside-out. Just note it.
* **Black video in modes 1, 3 or 4:** look for `ExoPlayer error` or `No video in` in the log.
* **Black in mode 2 only:** look for `updateTexImage` or `Sphere` errors in the log.
* **`vrapi_EnterVrMode failed` repeating**, or `Render thread still in VR mode` after taking
  the headset off: note when it happened.
