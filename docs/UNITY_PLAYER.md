# Unity player (fallback)

The original headset app, a Unity 2019.4 LTS project in [`headset/`](../headset). It is being
replaced by the native player ([HEADSET_SETUP.md](HEADSET_SETUP.md)) and stays only as a fallback
until the native player can run a show (phase 1B). It is removed in phase 5 of
[EXECUTION_PLAN.md](EXECUTION_PLAN.md). Both apps use the package `com.syncvr.player`, so
installing one replaces the other.

It builds its whole scene at runtime, so the project is mostly scripts:

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


1. Install **Unity 2019.4 LTS** (use 2019.4.41f2, the security-patched release) with **Android Build
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

## Installing

Same as the native player: follow [Preparing headsets](HEADSET_SETUP.md#preparing-headsets), but pass
this APK instead:

```bash
python3 -m syncvr adb setup ../headset/Builds/SyncVRPlayer.apk
```

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
