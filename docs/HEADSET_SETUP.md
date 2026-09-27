# Headset setup (Oculus Go)

The headset app is a Unity 2019.4 LTS project in [`headset/`](../headset). It builds
its whole scene at runtime, so the project is mostly scripts:

| Script | Role |
|---|---|
| `SyncVRApp.cs` | Entry point: wires everything together, handles server commands, reports status every second. |
| `ServerConnection.cs` | UDP discovery, TCP connection with auto-reconnect, clock-sync pings (background threads). |
| `ClockSync.cs` | Offset between the headset clock and the server clock (lowest-round-trip sample wins). |
| `SyncEngine.cs` | Scheduling and drift correction; a port of `server/syncvr/sync_engine.py`. |
| `UnityVideoBackend.cs` | Unity `VideoPlayer` (hardware decoding) behind the `IVideoPlayer` interface. |
| `VideoScreen.cs` | 360/180 via the `Skybox/Panoramic` shader (handles stereo per eye), or a flat screen. |
| `ContentManager.cs` | Local video folder, resumable downloads, deletion. |
| `DeviceInfo.cs` | Battery, temperature, storage, Wi-Fi signal, worn/not worn; Wi-Fi locks. |
| `Overlay.cs` | In-headset text: status screen, operator messages, identify banner + beep. |
| `HeadsetConfig.cs` | Optional `config.json` (fixed server address, discovery filters). |
| `Editor/SyncVRBuild.cs` | Menu items to configure the project for Oculus Go and build the APK. |

## Building the APK

1. Install **Unity 2019.4 LTS** (2019.4.40f1 is the last release) with **Android Build
   Support** (including the Android SDK/NDK and OpenJDK options) from Unity Hub's archive.
   Newer Unity versions dropped Oculus Go support.
2. Open the `headset/` folder as a project in Unity Hub.
3. Menu **SyncVR › 1. Configure for Oculus Go**. This switches to Android, sets the
   player settings (package `com.syncvr.player`, API 25+, ARMv7, OpenGL ES 3, single-pass
   stereo), creates the scene and materials, installs the *Oculus (Android)* package and
   enables Oculus in the legacy VR settings. Watch the Console: it ends with
   "project configured for Oculus Go".
   * If the package can't be installed automatically, add **Oculus (Android)** in
     *Window › Package Manager*, then in *Project Settings › Player › XR Settings* (Android
     tab) tick *Virtual Reality Supported* and add *Oculus*.
4. Menu **SyncVR › 2. Build APK** → `headset/Builds/SyncVRPlayer.apk`.

After configuring once in the editor (and committing `headset/ProjectSettings` and
`headset/Packages`), builds can run headless:

```bash
Unity -batchmode -quit -projectPath headset \
      -executeMethod SyncVR.EditorTools.SyncVRBuild.BuildFromCommandLine -logFile build.log
```

## Preparing headsets

Each Oculus Go needs **developer mode** once (Oculus/Meta phone app › Devices › the
headset › Developer Mode; this requires a developer organization on the Meta developer
site). Then connect it over USB and accept the *Allow USB debugging* prompt inside the headset.

With [Android platform-tools](https://developer.android.com/tools/releases/platform-tools)
(`adb`) installed, from `server/`:

```bash
python3 -m syncvr adb devices                                   # list headsets, battery, temperature
python3 -m syncvr adb setup ../headset/Builds/SyncVRPlayer.apk  # install + config + prox-off + launch
python3 -m syncvr adb push ../content/*.mp4                     # copy videos over USB (4 headsets at a time)
python3 -m syncvr adb list                                      # what's on each headset
```

Every command runs on all connected headsets in parallel; add `-s SERIAL` to target one.
Other commands: `install`, `configure`, `launch`, `stop`, `prox-off`, `prox-on`, `wifi`
(switch to Wi-Fi ADB), `connect IP…`, `reboot`, `shell CMD…`.

* **Proximity sensor.** `setup` runs `prox-off` so the headset keeps playing when nobody is
  wearing it (useful while preparing a room). This lasts until the headset reboots; use
  `--keep-proximity` to skip it.
* **Where videos live.** `/sdcard/Android/data/com.syncvr.player/files/videos/`. Reinstalling
  with `adb install -r` (what `install`/`setup` do) keeps them; *uninstalling* the app deletes them.
* **Fixed server address.** If UDP broadcast doesn't reach the headsets (client isolation,
  VLANs), write a config: `python3 -m syncvr adb configure --server 192.168.1.10`. With
  several servers on one network, `--server-name "Room A"` makes headsets only join the
  server started with `--name "Room A"`.

The headset shows its name and connection state on a dark screen while idle. Headsets
use their Android serial number as their ID, which is the same serial `adb devices`
shows, so it's easy to match a physical headset to its dashboard card.

## First test on hardware

This app compiles against the Unity 2019.4 APIs, but it hasn't been built or run on an
Oculus Go yet. Check these first, roughly in order of risk:

1. **Speed changes on Unity's Android video player.** Rate correction sets
   `VideoPlayer.playbackSpeed` to between 0.95 and 1.05. Some Unity versions had problems
   with speed changes on Android (freezing, or audio dropping out). The engine detects
   a frozen video and switches that headset to seek-only correction by itself.
   If audio misbehaves, set *Correction mode* to `seek` in the dashboard's Settings tab.
   That applies to the whole fleet immediately, with no rebuild.
2. **360° orientation.** The Panoramic skybox shader and the base yaw used in
   `VideoScreen.cs` should put the centre of the video in front of the viewer. If it's
   off by 90° or 180°, set *Yaw°* for that video in the Library tab (or change `Base360Yaw`).
3. **Stereo.** Play a top/bottom 3D video and check each eye gets its own half.
4. **Sync.** Put two or three headsets side by side with their speakers on. After a few
   seconds of playback the audio should sound like a single source. The *drift* figure on
   each card should settle under ~20 ms. The *start_latency_ms* and *seek_time_ms*
   calibration values are in each headset's status (visible via `GET /api/state`) and
   are saved on the headset between runs.
5. **Discovery.** If headsets stay on "Searching for server…", check the router's
   client isolation setting or use `adb configure --server`.
6. **Downloads.** Push a large file, turn Wi-Fi off halfway through, and check it resumes.

Useful logs: `adb logcat -s Unity` shows the app's `[SyncVR]` messages.

## Using a different video decoder

Everything playback-related goes through `IVideoPlayer` (in `SyncEngine.cs`). To use,
say, AVPro Video (ExoPlayer backend, HEVC, pitch-corrected speed changes), write a
class implementing that interface around its `MediaPlayer` and construct it in
`SyncVRApp.Awake` instead of `UnityVideoBackend`.

## Engine tests without Unity

```bash
cd headset
mcs -out:/tmp/enginetests.exe Tests~/EngineTests.cs Assets/SyncVR/Scripts/SyncEngine.cs \
    Assets/SyncVR/Scripts/Messages.cs Assets/SyncVR/Scripts/ClockSync.cs
mono /tmp/enginetests.exe
```

These run the C# engine against a simulated decoder on a virtual clock. The same
scenarios run against the Python reference in `server/tests/test_sync_engine.py`.
