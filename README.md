# SyncVR — synchronized video playback for VR headset fleets

A self-hosted replacement for the **Headjack Link + Headjack Operator** workflow:
load MP4s onto a fleet of Oculus Go headsets and play, pause and seek them all
in sync from a tablet or laptop, over a local Wi-Fi network, with no cloud
account and no internet connection.

```
                 ┌──────────────────────────── local Wi-Fi ───────────────────────────┐
  tablet/laptop  │                                                                    │
  browser ──HTTP─┤  SyncVR server (python -m syncvr serve)                            │
  dashboard      │   • operator dashboard + JSON API      :8080                       │
                 │   • content downloads (HTTP, resumable) :8080/content/…            │
                 │   • headset control (TCP, JSON lines)   :8765  ◄──┐                │
                 │   • discovery beacon (UDP broadcast)    :8766  ───┼──► headsets    │
                 │                                                   │  (SyncVR Player│
                 │                                         clock sync, status, cmds)  │
                 └────────────────────────────────────────────────────────────────────┘
```

| Part | Where | What it is |
|---|---|---|
| **Server + dashboard** | [`server/`](server/) | Python 3.8+ (one dependency: aiohttp). Runs on any laptop, mini-PC or Raspberry Pi on the headsets' network. Replaces Headjack Operator. |
| **Headset app** | [`player-android/`](player-android/) | Native Oculus Go player (Kotlin + C++, built and signed by CI, no Unity). Replaces Headjack Link / the Cinema template. |
| **Fleet tool** | `python -m syncvr adb …` | Installs the app, writes config and copies videos to many USB-connected headsets in parallel. |
| **Simulator** | `python -m syncvr sim` | Simulated headsets that run the real sync code, for trying the dashboard and load-testing without hardware. |

## What it does (compared with Headjack)

| Headjack capability | SyncVR |
|---|---|
| Operator app remote-controls any number of headsets | Web dashboard: open `http://<server>:8080` on a tablet, phone or PC. Several operators can have it open at once. |
| Play / pause / seek all headsets in sync | Starts are scheduled on a shared clock (default 1.5 s ahead). Each headset measures its clock offset to the server (NTP-style), its own seek time and start-up latency, and corrects drift continuously. |
| Stays in sync on imperfect networks | Only the schedule travels over the network, not the timing. Headsets that reconnect or power on mid-show rejoin in sync automatically. |
| Select headsets / groups | Tap to select; name headsets ("Seat 12") and put them in groups ("Room A"). Commands go to the selection, or to all headsets. |
| Headset status: battery, temperature, connection, playback | Live cards: battery/charging, temperature, worn or not, Wi-Fi signal, round-trip time, content present, download progress, playback position and sync drift. |
| Preload content onto many headsets without sideloading | "Push video" downloads over Wi-Fi with a limit on simultaneous transfers (default 4) and resumes interrupted downloads. For big fleets, `syncvr adb push` copies over USB. |
| Works offline on a local router | Nothing ever touches the internet. |
| Send messages to viewers | Text shown inside the headsets. "Identify" shows a headset's name and beeps so you can find it. |
| Recenter viewers | "Recenter view" makes each viewer's current direction the front of the video. |
| 360 / 180 / flat, mono / stereo | 360° and 180° equirectangular, mono, top/bottom or side-by-side 3D, and a flat cinema screen. Guessed from file names (`_360_TB`, `_180_SBS`, `_flat`), editable in the Library tab. |
| Logs | Event log in the dashboard (connects, downloads, errors reported by headsets). |

Not included (yet): Headjack's cloud CMS and analytics, subtitles, viewer-driven kiosk menus,
and store publishing. See [Roadmap](#roadmap).

## Quick start

### 1. Run the server

```bash
cd server
python3 -m pip install -e .          # or: pip install aiohttp
mkdir content && cp /path/to/*.mp4 content/
python3 -m syncvr serve              # dashboard on http://<this-machine>:8080
```

Useful options: `--content DIR`, `--name "Room A"`, `--password SECRET` (protects the
dashboard), `--max-downloads N`, `--broadcast 192.168.1.255` (if headsets on another
subnet can't find the server). Run `python3 -m syncvr serve --help` for all of them.

No headsets yet? Start 20 simulated ones in another terminal and use the dashboard:

```bash
python3 -m syncvr sim --count 20
```

### 2. Install the headset app

See **[docs/HEADSET_SETUP.md](docs/HEADSET_SETUP.md)**; nothing is built locally. CI publishes the
APK as the **SyncVRPlayer-apk** artifact and on the git branch `apk/<branch>` (`git apk`). With
headsets on USB:

```bash
cd server
python3 -m syncvr adb setup ../SyncVRPlayer.apk   # install, configure, launch
python3 -m syncvr adb push ../content/*.mp4       # optional: fast USB preload
```

The native player has run on hardware (see the status table below).

### 3. Run a show

1. Power on the headsets and start **SyncVR Player** (Library › Unknown Sources on the Go; `adb launch` also works).
   They find the server by themselves and appear in the dashboard.
2. *Headsets* tab: name them and set groups (Edit on each card).
3. Pick a video, **Push video** if it isn't on the headsets yet (the card shows `3/3 videos`).
4. **Load** holds everyone on the first frame; **Play** starts everyone together. **Pause**,
   **±10 s**, the seek bar and **Play from start** all stay in sync.
5. Watch the **drift** figure on each card: under ~20 ms is normal.

## Launcher (PC control window)

Double-click instead of using a terminal: `Start SyncVR.desktop` (Linux; on Linux Mint's Nemo, right-click > Properties > Permissions > "Allow executing file as program", then choose
"Trust and launch" the first time, or run `./start-syncvr.sh`) `Start SyncVR.pyw` (Windows; WSL users
should start it from Windows, not from inside WSL) or `Start SyncVR.command` (macOS; right-click > Open the
first time). The first run creates `server/.venv` and downloads aiohttp and PySide6 (~100 MB; needs internet,
and on Mint `sudo apt install python3-venv`; if the window fails to start, `sudo apt install libxcb-cursor0`). It runs the server, opens the dashboard once, and shows
a window with the headset count, the dashboard URL and buttons Open dashboard / Open content folder /
Open log / Stop & quit. Content goes in `server/content`, logs in `server/data/logs/syncvr.log`.
Without PySide6 or a display it runs in the terminal instead (`SYNCVR_NO_VENV=1` skips the venv). Starting it a
second time while port 8080 is taken just opens the running dashboard. Optional settings go in
`server/data/launcher.json` (`content`, `http_port`, `name`, `password`, `open_browser`).

## Network and content recommendations

* **Use a dedicated router** that you bring to the venue. Turn off *AP/client isolation*
  (headsets must be able to hear the server's UDP broadcast). Prefer 5 GHz.
* Consumer routers handle ~25–30 headsets well; beyond ~60, use enterprise access points.
  Put the server machine on Ethernet.
* If broadcast discovery is blocked, use `serve --broadcast 192.168.1.255`. A fixed server address
  (`syncvr adb configure --server …`) is not read by the native player yet.
* **Encoding for Oculus Go:** H.264 High profile MP4, up to 3840×1920 (360 mono) or
  3840×2160 (stereo) at 30 fps, 20–40 Mbps, AAC audio. A keyframe every 1 s
  (`-g 30` at 30 fps) makes seeks and re-syncs faster. Example:

  ```bash
  ffmpeg -i master.mov -c:v libx264 -profile:v high -preset slow -b:v 30M -maxrate 40M -bufsize 60M \
         -g 30 -pix_fmt yuv420p -vf scale=3840:1920 -c:a aac -b:a 192k -movflags +faststart show_360.mp4
  ```

## How synchronization works

The server never streams timing; it sends *anchors*: "video V is at position P at server
time T". Headsets estimate their clock offset to the server from ping round trips and
schedule against that. To start (or recover), a headset *cues*: pauses, seeks to where
the video must be at a moment far enough ahead, and presses play just before it — early by
its own measured start-up latency. While playing it compares its position with the
anchor every frame; small drift is absorbed by changing speed by a few percent, large
drift triggers a new cue. Details and tuning: [docs/PROTOCOL.md](docs/PROTOCOL.md#synchronization).

In the simulator (40 headsets with random clock offsets, ±300 ppm decoder error, 20–200 ms
start latency and 50–500 ms seek times) the median error stays around 2–5 ms and the
worst headset under 20 ms once playing.

## Project status

| Component | State |
|---|---|
| Server, dashboard, protocol, content distribution, ADB tool | Working; 42 automated tests (`cd server && python3 -m pytest`), including end-to-end runs with simulated headsets. |
| Sync engine | The Python reference engine is tested in `server/tests`, the Kotlin engine in the `player-android/core` tests, with the same scenarios. |
| Headset app (native) | `player-android/`, built and signed by CI: server connection, sync, content, telemetry, operator commands and an end-to-end CI test against the Python server. Verified on an Oculus Go: connect, load/play/sync, recenter, worn/proximity, resume ([checks](docs/HEADSET_SETUP.md)). Stereo eye order and some edge cases remain untested. |

## Roadmap

* On-device validation on Oculus Go; tune default sync settings from measurements.
* Optional AVPro Video / ExoPlayer backend (smoother audio during speed correction, HEVC 5.7K).
* Subtitles and multiple audio tracks.
* Playlists and scheduled shows; viewer-driven kiosk menu.
* Quest build target (OpenXR) for mixed Go/Quest fleets.

## Repository layout

```
server/                 Python package "syncvr"
  syncvr/controller.py  fleet state, commands, content distribution
  syncvr/sync_engine.py reference headset sync algorithm (ported to Kotlin)
  syncvr/web/           dashboard (plain HTML/CSS/JS, no build step)
  tests/                pytest suite
player-android/         native Oculus Go player (Kotlin core + Android/C++ app)
docs/                   protocol, headset setup guide (native player), improvement and execution plans
```
