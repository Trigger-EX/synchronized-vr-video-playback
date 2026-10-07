# Headset setup (Oculus Go)

The headset app is the native player in [`player-android/`](../player-android): Kotlin and C++
on Oculus VrApi 1.32, with ExoPlayer for video. Its package name is `com.syncvr.player`.

The APK is built by CI; you only need `adb`, Python 3.8+ and this repo.
The player discovers the server by UDP broadcast, connects, follows play/pause/seek on the shared
clock, downloads content, reports telemetry and shows its name and connection state while idle.
It has not yet been checked on hardware (see [Checkpoint 1](#checkpoint-1-hardware-check) and
Checkpoint 2 in [EXECUTION_PLAN.md](EXECUTION_PLAN.md)).

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
git apk --operator   # ./SyncVROperator.apk, the operator app for a phone or tablet (see below)
```

If the branch has commits newer than the published APK, `git apk` says so (CI still running or
failed). Without git: on GitHub → **Actions** → the latest green **CI** run on the branch →
**Artifacts** → **SyncVRPlayer-apk** (a zip containing `SyncVRPlayer.apk`) or
**SyncVROperator-apk** (`SyncVROperator.apk`).

Every build is signed with the same key (stored in the repository secrets), so a newer APK
installs over an older one without losing videos.

## Operator app (phone or tablet)

`SyncVROperator.apk` (package `com.syncvr.operator`, Android 7.0+) is a native remote control for
the same dashboard: no browser needed. It is signed with the same key as the player and is
installed on the operator's phone, not on a headset.

```sh
git apk --operator --install   # phone connected over USB with debugging on; or copy the file over and open it
```

1. Put the phone on the same Wi-Fi as the server and open **SyncVR Operator**. Servers are found
   automatically (UDP beacon on port 8766, the same one headsets use); tap one, or type
   `192.168.1.10` or `192.168.1.10:8080` (port 8080 is the default). Enter the password if the
   server was started with one.
2. **Headsets** lists every headset with online state, status, video and position. Tap rows to
   choose which ones the controls apply to (none chosen means all); long-press to rename, set a
   group, or forget an offline headset.
3. Controls at the top: Play, Pause, Stop, a seek bar, -10 s / +10 s, Resync, Identify and
   Volume. **Library** picks a video to Load or Play and sends files to headsets
   (**Send to headsets**, **Cancel sends**, **Rescan**). **Events** shows the server log.

The app refreshes once a second while it is in the foreground. If discovery finds nothing (guest
Wi-Fi, client isolation, a VPN), enter the address by hand. Plain HTTP is used, as in the browser
dashboard.

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
  broadcast must reach the headsets (no client isolation; `serve --broadcast` for other subnets).
* **USB mode (no firewall rules).** `python3 -m syncvr adb launch --usb` (or `setup APK --usb`)
  runs `adb reverse tcp:8765 tcp:8765` and `tcp:8080 tcp:8080` on each headset (content downloads use 8765 too; 8080 is only the dashboard), then starts the app
  with `--es server 127.0.0.1`, so the headset reaches the server through the USB cable and no
  discovery or open ports are needed (the default ports only; the server must run on the machine
  the headsets are plugged into). The reverse is lost on unplug or reboot: replug and rerun
  `adb launch --usb`. Without `--usb` nothing changes (UDP discovery).

Once connected, the player shows its name and connection state on a dark screen while idle. The
dashboard shows `native` as the player type. Headsets use their Android serial number as their ID,
the same serial `adb devices` shows, so it's easy to match a physical headset to its dashboard card.

## Terminal (desktop app)

**View > Open local terminal** opens a shell on the operator's computer; **View > Open terminal on selected
headset** opens `adb shell` on the one selected online headset (resolved like the other adb tools, so it must
appear in `adb devices`; otherwise it is refused). Sessions dock at the bottom, and closing a dock or the app
hangs up the shell's process group (SIGHUP, then SIGKILL after a second).

* Linux and macOS only; the menu entries are greyed out on Windows. The adb folder comes from
  **File > Set scrcpy/adb folder...**.
* Gated as `terminal.shell`: it opens only once marked tested, or via **Open** in the Tools tab's Testing card
  (which uses the selected headset, or a local shell when none is selected).
* Output is plain text (colours and cursor escapes are dropped, 5,000 lines kept); it is not a full-screen
  terminal, so use `adb shell` in a real terminal for `top`, `vi` and the like.
* It is GUI-only on purpose: the web dashboard and the CLI have no route to a shell.

## Kiosk mode

`python3 -m syncvr adb kiosk on` disables the Oculus home (`com.oculus.vrshell`) and makes the
player the home app, so the headset boots into it and the Oculus button returns to it. It
needs a current APK: the HOME role lives in a disabled `.HomeAlias` that `kiosk on` enables
through the app's `KioskReceiver` (protected by `android.permission.DUMP`, which only adb shell holds).

```bash
python3 -m syncvr adb kiosk on             # disable vrshell, enable the alias, set the player as home
python3 -m syncvr adb kiosk on --root      # the adb root recipe; needs USB, not Wi-Fi ADB
python3 -m syncvr adb kiosk off            # re-enable vrshell, clear the player's home choice, restore vrshell home
python3 -m syncvr adb kiosk restore        # off, plus pm unhide
python3 -m syncvr adb kiosk restore --root # also runs pm enable as root; USB only
```

* **Wait before `adb reboot`.** Android writes the disabled packages and preferred home to
  `package-restrictions.xml` lazily (about 10 s), so rebooting right after a change boots the
  previous state. After a successful `on`/`off`/`restore`, the command runs `adb shell sync` and
  waits 15 s (`PERSIST_WAIT_S`) per headset, then prints "safe to reboot". Wait for the command to
  return before `adb reboot`. `--no-wait` skips the wait; then allow ~15 s yourself. A failed
  command does not wait.
* **Verification.** `on` and `off` check the result and exit non-zero on failure. `off` and
  `restore` fail if `pm list packages -d` still lists `com.oculus.vrshell` (after retrying
  `pm enable --user 0`), and if the HOME activity (`cmd package resolve-activity`) is not
  vrshell's. `on` fails if HOME does not resolve to the player (for example `ResolverActivity`).
  The error says the next command to run; usually `kiosk restore --root` over USB.
* **Recovery.** If a headset is stuck in the player, run `kiosk off`, and `kiosk restore` if that
  fails. Set up Wi-Fi on the headset *before* `kiosk on`, since the Oculus UI is how you do that.
  Use USB for `--root` (`adb root` restarts adbd, which drops Wi-Fi ADB).
* **"Always" choice.** If you picked SyncVR with "Always" in the home chooser, that entry points
  at the player; `off`/`restore` clear it (without `pm clear`, so `config.json` survives).
* **Server address.** Kiosk headsets rely on `config.json` or UDP discovery for the server, not
  `--usb` (the `adb reverse` is lost on reboot).
* **Unverified.** vrshell's home is assumed to be `com.oculus.vrshell/.MainActivity`, and that
  the shell is granted `DUMP` on the Go; neither is confirmed on hardware after this change.

## Checkpoint 1 (hardware check)

Normally the player shows each video in the view set for it on the server (projection `360`, `180` or `flat`;
stereo `mono`, `tb` or `sbs`; changes on the dashboard apply live, unknown values show as 360 mono) and does
not cycle. For this check, launch it in the debug cycle mode, which steps through four ways of showing a video:
`adb shell am start -n com.syncvr.player/.MainActivity --ez cycle_modes true`. The goal is to find out
which display path works on the Go. The 180 and side-by-side mappings and the eye order are not yet confirmed
on hardware: also check one 180 video and one SBS video in normal mode. It takes about 15 minutes with one headset.

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

Put the headset on. In cycle mode it switches every **15 seconds** to the next mode and loops. A small text
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

## Viewing several headsets at once (operator panel)

The panel's **View** menu opens scrcpy mirrors (needs adb and scrcpy, and `adb tcpip 5555` once per headset):

* **Capture selected / Capture all** tiles one window per online headset across the screen (15 fps, one eye).
  At most 6 run at once; extras are refused with a message. Headsets adb cannot reach are skipped and logged.
* **Batch preview...** shows N headsets for a set number of seconds, closes them, then shows the next group.
* **Close all captures** closes every window the panel opened this way (not the docked single view).

Batch windows use the window class `SyncVR-batch`. If scrcpy is too old for `--window-x`, windows open untiled.

## Network tools (Tools tab, NETWORK card)

* **Connect all** runs `adb connect <ip>:5555` for every saved headset address (needs `adb tcpip 5555` once per headset).
* **Purge...** drops those connections and reconnects them. It starts as a dry run; untick it to act. It is refused
  while a push is running or another adb job is active.
* **Scan subnet...** probes a small private subnet (at most 32 hosts, e.g. `192.168.1.0/27`) for port 5555 and
  connects headsets the server already knows. Other devices that answer are logged and left alone.
* **Bandwidth test** makes each selected, idle headset download up to 20 MB of the largest video and reports Mbps
  (the real HTTP path, so it includes the access point). It is refused in Show Mode, while content is syncing, and
  for headsets that are playing. Headsets running an older app time out with "player may not support bandwidth_test".

Purge and Bandwidth test stay disabled until marked tested in the Testing card.
