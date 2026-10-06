# SyncVR protocol and API

## Ports

| Port | Transport | Purpose |
|---|---|---|
| 8080 | HTTP | JSON API (operator app, show control), content downloads (`/content/<name>`) |
| 8765 | TCP | Headset control connection; also serves content downloads (`GET`/`HEAD /content/<name>`, nothing else), told apart by the first bytes |
| 8766 | UDP | Discovery beacons (server → broadcast) |

All three can be changed with `serve` options; headsets learn the TCP port from the
beacon and the HTTP port from the `welcome` message. Download URLs sent to headsets
use the TCP port, so headsets only need 8765; 8080 is just the operator API.

## Discovery

Once a second the server broadcasts:

```json
{"type":"beacon","service":"syncvr","proto":1,"version":"0.1.0","server_name":"SyncVR","tcp_port":8765,"http_port":8080}
```

to 255.255.255.255 and to the /24 broadcast address of each local interface (override
with `--broadcast`). Headsets connect to the sender's address. A headset with a
`config.json` containing `server` connects there directly instead. With `server_name`,
it ignores beacons from other servers.

## Headset connection

One TCP connection per headset carrying UTF-8 JSON objects, one per line. Every
object has a `type`. **All times are server-clock seconds** (the server's
`time.monotonic()`); headsets convert using their measured offset.

### Headset → server

| type | fields | when |
|---|---|---|
| `hello` | `proto`, `device_id`, `serial`, `model`, `app_version`, `player` (`native` for the player in `player-android/`, `sim` for the simulator; optional, older apps omit it) | first line on every connection |
| `time_ping` | `id`, `t0` (headset clock) | ~10/s right after connecting, then every 2 s |
| `inventory` | `files: [{name, size}]` | after connecting and whenever local files change |
| `status` | see below | every second |
| `downloads_finished` | `ok: [names]`, `failed: [names]`, `cancelled`, `job` (echoed from `sync_content`; absent from older apps) | end of a `sync_content` job |
| `event` | `level` (`info`/`warn`/`error`), `message` | noteworthy things (shown in the operator window log) |

`status` fields: `state` (`idle`, `loading`, `ready`, `playing`, `syncing`, `paused`,
`ended`, `error`), `video`, `position`, `expected`, `duration`, `drift_ms` (smoothed
position error, + = ahead), `rate`, `mode`, `seek_time_ms`, `start_latency_ms`,
`battery` (0–1, -1 unknown), `charging`, `battery_current_a` (amps, + = charging; optional, omitted when unknown), `temp_c`, `worn`, `storage_free`, `wifi_rssi`,
`volume`, `rtt_ms`, `clock_synced`, `fps`, `download` (`{name, received, total}` or null),
`error`.

### Server → headset

| type | fields | effect |
|---|---|---|
| `welcome` | `server_name`, `device_name`, `group`, `http_port`, `server_time`, `settings` | sent on connect |
| `time_pong` | `id`, `t0` (echoed), `ts` (server clock) | reply to `time_ping`, sent immediately |
| `settings` | `settings` | sync tuning changed in the operator window |
| `device_info` | `device_name`, `group` | headset renamed |
| `play` | `video`, `projection`, `stereo`, `rotation`, `duration`, `pos`, `at`, `loop` | be at `pos` at server time `at` and keep playing (loads the video if needed; joins late if `at` has passed) |
| `pause` | same, `at` = when to pause | at `at`, pause and show exactly `pos` (also used to load a video and hold it) |
| `view` | `video`, `projection`, `stereo`, `rotation` | the operator changed how this video is displayed; apply it to the loaded video `video` immediately without seeking or pausing (ignore if another video is loaded). `play`/`pause` always carry the current values too |
| `stop` | | unload, show the idle screen |
| `volume` | `value` 0–1 | |
| `recenter` | | current viewing direction becomes the front |
| `message` | `text`, `seconds` | show text in the headset (`seconds: 0` clears it) |
| `identify` | `name`, `seconds` | show the name in large text and beep |
| `sync_content` | `files: [{name, size, url, sha256?}]`, `delete_others`, `job` | download missing/incomplete files one after another (HTTP Range resume); optionally delete other videos |
| `cancel_downloads` | | stop the current download job |
| `delete_content` | `names` | delete videos (the loaded one is kept) |

`sha256` is the lowercase hex SHA-256 of the file. The server computes it once per file in the background
(cached by size and modification time), so the field is omitted while it is not ready yet; treat a missing
`sha256` as "no checksum, trust the size". When it is present, a headset verifies each file after downloading
it (and may verify an already complete file before skipping it). On a mismatch it deletes the file, downloads
it again from scratch once, and reports the name under `failed` in `downloads_finished` if the second copy
is wrong too. The native player verifies. The same hash is sent as the
`X-Content-SHA256` header of `GET /content/<file>` when known.

`job` is an opaque id the server picks for each `sync_content`. The headset echoes it in
`downloads_finished`, and the server ignores a `downloads_finished` whose `job` is not the current one (a
headset may still be finishing a job from before a server restart); a missing `job` is accepted. While a
job runs, report progress in `status.download` every second, including while checking existing files
(hashing). A job whose headset has reported no `download` for 20 s stops counting toward the concurrent
download limit but is kept, and saved, until `downloads_finished` arrives, so a server restart never loses it.
After a restart the server re-sends `sync_content` on reconnect; the headset must replace its running job
with the new one and resume partial `.part` files.

Projections: `360`, `180`, `flat`. Stereo: `mono`, `tb` (top/bottom), `sbs` (side-by-side).

## Synchronization

### Clock

For every `time_ping` the headset records its send time `t0` and receive time `t1`; the
server stamps `ts` when answering. `offset = ts − (t0 + t1)/2`. Of the last 20 samples,
the one with the smallest round trip `t1 − t0` wins, because queuing delays only ever add
time. On a healthy LAN this gives ~1 ms accuracy. Headsets hold an Android
high-performance Wi-Fi lock so radio power-saving doesn't add delay.

### Anchors and desired state

The server keeps a *desired state* per headset: stopped, paused at `pos`, or playing
from the anchor `(pos, at)`. Commands change the desired state of the targeted headsets and
push it to the online ones. When a headset reconnects it is re-sent its desired state,
and since an anchor fully defines where the video should be at any time, it rejoins
in sync. Headsets that share an anchor form a *cohort*:

* **Play** without a position resumes where the targets were paused, joins a cohort
  already playing that video, or starts at 0. It is scheduled `play_lead_ms` ahead.
* **Pause** computes the position at `now + pause_lead_ms` from the cohort's anchor, so
  every headset stops on the same frame.
* **Seek** while playing sets a new anchor `seek_lead_ms` ahead. While paused, it moves
  the held frame.

### Headset engine

1. **Cue.** Choose a start moment C: the anchor time if it's at least
   `1.5 × seek_time + 0.15 s` away, otherwise that far from now. Pause, seek to the
   position for C, then call play at `C − start_latency`. If the seek finishes too late,
   cue again further ahead.
2. **Learn.** `seek_time` is measured on every seek (rises immediately, decays slowly).
   `start_latency` is corrected from the drift measured just after each cued start. Both
   are saved on the headset.
3. **Correct.** Every frame, `drift = position − expected`, smoothed. In `rate` mode, when
   |drift| > `deadband_ms`, speed = 1 − `rate_gain`·drift (clamped to ±`max_rate_adjust`)
   until drift is under 20% of the deadband. When |drift| > `hard_seek_ms` (or
   `seek_mode_threshold_ms` in `seek` mode), re-cue, at most once per `seek_cooldown_ms`.
4. **Fallbacks.** If the video stops advancing while the speed is not 1.0, the headset
   switches itself to seek-only correction and logs a warning. After the app resumes from
   sleep it re-measures the clock and re-cues.

### Tuning (operator window › Settings)

| Setting | Default | Raise it when… |
|---|---|---|
| `play_lead_ms` / `seek_lead_ms` | 1500 | headsets on a busy network often join late (state `syncing` after a start) |
| `pause_lead_ms` | 300 | paused headsets show different frames |
| `correction_mode` | `rate` | use `seek` if speed changes cause audio glitches |
| `deadband_ms` | 20 | rate keeps toggling (lower it for tighter sync) |
| `rate_gain`, `max_rate_adjust` | 0.8, 0.05 | drift takes too long to fix (higher is faster but more audible) |
| `hard_seek_ms` | 300 | too many visible re-syncs |
| `settle_ms` | 750 | drift readings right after starting are noisy |

## HTTP API

The Android operator app uses the same API, so show-control systems (QLab, Bitfocus Companion, Crestron, …) can drive
SyncVR with plain HTTP calls. With `--password`, send HTTP basic auth (any user name).

| Method & path | Body / result |
|---|---|
| `GET /api/state` | full snapshot: server, settings, devices, library, downloads, events. Each `library` entry also has `sha256` (null until computed), `probe` (ffprobe summary), `issues` (`[{level: error|warn|info, code, message}]`: the checks against the Go's limits) and `analysis` (`pending`, `done` or `unavailable` when ffprobe is not installed) |
| `POST /api/command` | `{"action": …, "targets": …, …}` → `{"ok": true, "result": …}` or 400 `{"error": …}` |
| `GET/POST /api/settings` | read / change sync settings (partial updates; `max_downloads` too) |
| `POST /api/library/rescan` | re-read the content folder (also happens every 30 s) |
| `POST /api/library/<file>` | `{"title", "projection", "stereo", "rotation", "loop"}` (any subset; `projection`/`stereo` also accept `"auto"` to drop the stored choice and re-detect) |
| `POST /api/devices/<id>` | `{"name", "group"}` |
| `DELETE /api/devices/<id>` | forget an offline headset |
| `GET /content/<file>` | the video file (supports Range); `X-Content-SHA256` header once the checksum is known |

`targets`: `"all"` (default), `"online"`, a list of headset ids, or `{"group": "Room A"}`. A target list that
selects no headset is a 400 error.

The result of a command holds `targets` (how many headsets it addressed) and `online` (how many of those are
connected). For `load`, `play`, `pause`, `seek` and `stop`, when none is online the result also has a
`warning` string; the command is still remembered and applied when the headsets connect. The operator window
shows the warning in its status bar.

Actions:

| action | parameters |
|---|---|
| `load` | `video`, `pos` (default 0) |
| `play` | `video` (optional), `pos` (optional: omit to resume/join), `loop` (optional) |
| `pause` | |
| `seek` | `pos` (seconds) or `delta` (seconds, ±) |
| `stop` | |
| `resync` | re-send the desired state |
| `volume` | `value` 0–1 |
| `recenter`, `identify` | |
| `message` | `text`, `seconds` |
| `sync_content` | `videos` (list or `"all"`), `delete_others` (bool) |
| `delete_content` | `videos` (list) |
| `cancel_downloads` | |

Example:

```bash
curl -X POST http://server:8080/api/command -H 'Content-Type: application/json' \
     -d '{"action":"play","video":"concert_360_TB.mp4","targets":{"group":"Room A"}}'
```
