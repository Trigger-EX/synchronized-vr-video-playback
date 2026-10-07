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
| `bandwidth_result` | `job` (echoed), `ok`, `bytes` read, `seconds`, `mbps` (megabits/s over what was read), `error` | end of a `bandwidth_test` (also sent as `ok: false` when the headset refuses it: no url, busy downloading, a test already running) |
| `pose` | `yaw`, `pitch`, `roll` (degrees, relative to the recentered front; yaw + = left, pitch + = up) | only while a `pose_stream` lease is active, at the requested rate; the server stores it silently (no dashboard refresh) |
| `event` | `level` (`info`/`warn`/`error`), `message` | noteworthy things (shown in the operator window log) |

`status` fields: `state` (`idle`, `loading`, `ready`, `playing`, `syncing`, `paused`,
`ended`, `error`), `video`, `position`, `expected`, `duration`, `drift_ms` (smoothed
position error, + = ahead), `rate`, `mode`, `seek_time_ms`, `start_latency_ms`,
`battery` (0–1, -1 unknown), `charging`, `battery_current_a` (amps, + = charging; optional, omitted when unknown), `temp_c`, `worn`, `storage_free`, `wifi_rssi`,
`volume`, `rtt_ms`, `clock_synced`, `fps`, `download` (`{name, received, total}` or null),
`error`, `anchor` (`{pos, at, loop}`: the anchor the player follows, in server-clock seconds; present only while anchored, old apps omit it; the server uses it to recover a show after a restart).

### Server → headset

| type | fields | effect |
|---|---|---|
| `welcome` | `server_name`, `device_name`, `group`, `http_port`, `server_time`, `settings` | sent on connect |
| `time_pong` | `id`, `t0` (echoed), `ts` (server clock) | reply to `time_ping`, sent immediately |
| `settings` | `settings` | sync tuning changed in the operator window |
| `device_info` | `device_name`, `group` | headset renamed |
| `pose_stream` | `hz` | start streaming `pose` at `hz` (capped at 30); renews a 15 s auto-off lease, so the server resends about every 10 s. `hz` <= 0 stops. Used by the operator's "follow headset view" |
| `play` | `video`, `projection`, `stereo`, `rotation`, `duration`, `pos`, `at`, `loop` | be at `pos` at server time `at` and keep playing (loads the video if needed; joins late if `at` has passed) |
| `pause` | same, `at` = when to pause | at `at`, pause and show exactly `pos` (also used to load a video and hold it) |
| `view` | `video`, `projection`, `stereo`, `rotation` | the operator changed how this video is displayed; apply it to the loaded video `video` immediately without seeking or pausing (ignore if another video is loaded). `play`/`pause` always carry the current values too |
| `stop` | | unload, show the idle screen |
| `volume` | `value` 0–1 | |
| `recenter` | | current viewing direction becomes the front |
| `message` | `text`, `seconds` | show text in the headset (`seconds: 0` clears it) |
| `identify` | `name`, `seconds` | show the name in large text and beep |
| `sync_content` | `files: [{name, size, url, sha256?}]`, `delete_others`, `job` | download missing/incomplete files one after another (HTTP Range resume); optionally delete other videos |
| `bandwidth_test` | `job`, `url`, `bytes` (1-200 MB, default 20; the server clamps it to the file size), `seconds` (5-120, default 30) | GET `url` from the start, read and discard up to `bytes` or until `seconds` pass, then reply `bandwidth_result`. One test at a time, refused while downloading. Old apps ignore the message, so the server reports "timed out (player may not support bandwidth_test)" after `seconds` + 5 s |
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
| `POST /api/command/preview` | same body as a command (`action`, `targets`, optional `dry_run`, default true) → `{"scope_text", "labels", "warnings", "every", "show_mode", "token", "needs_confirm"}`; nothing is sent to any headset |
| `POST /api/adb/scan` | `{"cidr": "192.168.1.0/27"}` → `{"ok": true, "result": {"job", "addresses"}}`. Probes TCP 5555 on each host (private IPv4 ranges only; more than 32 hosts is a 400, never truncated), then `adb connect`s open hosts that are a known headset's saved address. Other open hosts are only logged. Runs as job `scan` |
| `POST /api/show_mode` | `{"enabled": true\|false}` → `{"ok": true, "brake": {...}}`; anything but a boolean is a 400. See "Show Mode and brakes" |
| `POST /api/features/<key>` | mark a feature as tested → `{"ok": true, "key", "tested": true}`; unknown key is a 400 |
| `DELETE /api/features/<key>` | mark it untested again |
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
| `sleep`, `wake` | adb jobs; confirm token required (see below). Screen off (`input keyevent 223`) or on (`224`) |
| `screen_refresh` | adb job; confirm token required. `min_asleep_s` (default 1, max 30). Sends one 223, polls `dumpsys power \| grep mWakefulness` until `Asleep` (3 s timeout), waits `min_asleep_s`, sends 224 and confirms `Awake`. Interrupts playback |
| `poweroff` | adb job (`reboot -p`); confirm token required; gated by feature `power.poweroff`; `dry_run` defaults to true. A real (`dry_run: false`) power off of every headset (target `all`/omitted, or a list naming every known headset) is refused unless the command also has `"confirm_every": true` |
| `connect` | adb job, no confirm token. `adb connect <ip>:5555` for each target's saved IP, once per distinct address; a shared address reports "same address as X", a headset with no IP "no saved address" |
| `purge` | adb job; confirm token (hashes the saved addresses, not `adb devices`); gated by feature `adb.purge`; `dry_run` defaults to true. Per address: `adb disconnect <ip>:5555`, 0.5 s, `adb connect`. A live purge is refused while the CLI push lock is held or any adb or scan job runs; Show Mode only adds a warning. The preview also returns `dry_run_choice` and `tokens: {dry_run, live}` |
| `bandwidth_test` | gated by feature `debug.bandwidth`. `mb` (default 20, 1-200), `timeout_s` (default 30, 5-120), `parallel` (default 1, max 8), `video` (default: the largest in the library, at least 1 MB). Sends `bandwidth_test` to each online target, at most `parallel` at once. Refused outright (no override) when a brake is active (Show Mode, sync in progress, push lock), when any target is playing, or when a test is already pending for a target. Job result per headset: `<mbps> Mbps`; the finished job has `summary: {n, median, min, min_label, failed}` and each device in the snapshot carries its last `bandwidth` (`ok`, `mbps`, `bytes`, `seconds`, `error`, `t`; not persisted) |
| `snapshot` | read-only adb job, gated by feature `debug.snapshot` (so `testing: true` works as for `poweroff`); no confirm token. `screenshot` (bool, default false) adds `adb exec-out screencap -p`. Writes `<data_dir>/snapshots/<label>_<serial>_<YYYYmmdd-HHMMSS-ffffff>/` on the server (nothing on the headset), one file per command: `power`, `display`, `window` (`dumpsys window windows`), `activity`, `audio`, `surfaceflinger`, `surfaceflinger_list`, `thermal`, `battery`, `logcat` (`-d -t 2000`), `getprop` (each `<name>.txt`), plus `screenshot.png` and `SUMMARY.txt` (wakefulness, display state, focus, thermal status, battery temperature). A command that fails or times out writes `<name>.error.txt` and the rest still run. Each headset's job result is `saved to <folder>` (with the failed command names); it is FAILED only when every command failed or the headset is unreachable. At most 4 headsets are captured at a time |

Example:

```bash
curl -X POST http://server:8080/api/command -H 'Content-Type: application/json' \
     -d '{"action":"play","video":"concert_360_TB.mp4","targets":{"group":"Room A"}}'
```

### Preview, confirmation and feature gating

Actions that act through adb (rather than the headset connection) are checked by the server:

- **Preview.** `POST /api/command/preview` resolves the targets and returns `scope_text` (for example
  `Sleep 3 headsets: A, B, C`, or `Sleep EVERY headset (72)` when all known headsets are targeted), the headsets'
  `labels`, `warnings`, `every`, a `token`, and `needs_confirm`. The token is a hash of the action, the sorted adb serials of the targets
  and `dry_run`, so it changes when the target set, their adb reachability or `dry_run` changes.
- **Confirmation.** An action with `needs_confirm: true` is refused with a 400 unless the command carries
  `"confirm": <token>` matching a fresh computation. Preview again when it is refused.
- **Dry run.** Destructive actions take `dry_run` (default `true`): they resolve the targets and log what they
  would do without sending anything. Send `"dry_run": false` to act.
- **Feature gating.** Each gated action belongs to a feature (`features` in `GET /api/state`: `key`, `category`,
  `label`, `tested`). The server refuses a gated action unless its feature is marked tested or the command has
  `"testing": true`. Marked features persist in `state.json` as `tested_features`.
- **Jobs.** adb actions return `{"job": "<id>"}` at once and run in the background. Each headset's outcome is added to
  `events` as one `OK` or `FAILED` line, and `jobs` in `GET /api/state` lists recent jobs:
  `{id, action, state: running|done|failed, total, done, failed, started, results: [{device, ok, message}]}`.
- **Snapshot.** `snapshot` is an adb job like the power actions (the web layer fetches `adb devices` off the event loop for it) but needs no confirmation token; `preview` still works and reports `needs_confirm: false`.
- **Power actions.** `sleep`, `wake`, `screen_refresh` and `poweroff` run per headset by adb serial (`ip:5555`, else
  USB serial). A headset adb cannot reach is reported as `FAILED (unreachable ...)` and nothing is sent to it. The
  preview adds `warnings` (list of strings: dry run notice, headsets not reachable by adb, and for `screen_refresh`
  a note that playback is interrupted on headsets that are playing) and `every` (true when the target set is every
  known headset).
- **Intentionally asleep.** `sleep` records its targets in an in-memory set (removed again if the sleep fails);
  `wake` and `screen_refresh` clear them. `GET /api/state` exposes it as `asleep` (headset ids) so watchdogs can
  skip those headsets. It is not persisted.
- Saved headsets now also keep their last `ip` (used to reach them with `adb -s <ip>:5555`).

### Show Mode and brakes

Brakes tell automation (the watchdogs below) not to send anything. They never block manual commands.
`GET /api/state` has `"brake": {"active": bool, "reason": str|null, "show_mode": bool}`. The first matching reason wins:

| `reason` | Meaning |
|---|---|
| `show_mode` | Show Mode is on. Set by an operator with `POST /api/show_mode {"enabled": bool}` or the topbar toggle; never switched on by playback. Persisted in `state.json` as `show_mode` |
| `sync in progress` | the server has active content download jobs (queued-only jobs do not count) |
| `push in progress` | a CLI `syncvr adb push` is running: it writes its PID to `push.lock` in the data folder (`--data DIR`, default the launcher's data folder) and removes the file on exit, including on errors. The lock counts only while that PID is alive; a missing, unreadable or dead-PID file never brakes |

While Show Mode is on, the preview (`warnings`, plus `"show_mode": true`) says "Show Mode is on: automation is paused,
but this command will still run.", and the GUI confirmation dialog shows it.

### Watchdogs

Five unattended checks, implemented in `server/syncvr/watchdogs.py`: `stay_awake`, `popup`, `overheat`,
`black_screen`, `keepalive`. Each runs as its own asyncio task, started by `SyncServer.start()` and cancelled on stop.

**Cycle.** Every `cfg.interval_s` (when enabled): list `adb devices` once, then for every headset NOT in the
intentionally-asleep set, observe (adb on the shared executor, filtered on the headset with `grep`, or player
telemetry where that already has the answer), call the pure `decide(history, obs, cfg)`, and carry out the returned
actions. Immediately before EACH send `Controller.brake()` is checked again (and once more after waiting for a poll
slot). When braked the decision is logged ("would wake ... braked: show_mode, not sent") and nothing is sent. A
watchdog that is enabled but not armed is observe-only: it logs the same way. Every cycle and every headset catches and
logs its own errors, so a bad cycle never ends the loop. At most 4 adb observations or sends run at once across all
watchdogs.

**State.** `GET /api/state` has `"watchdogs": [...]`, one object per watchdog: `name`, `title`, `feature`, `rule` (the
exact text quoted in the GUI arm dialog), `enabled`, `armed`, `mode` (`off` | `observe` | `armed`), `tested`,
`arm_blocker` (why it cannot be armed, or null), `cfg`, `pattern_hash`, `last_cycle` (wall clock), `last_summary`,
`counters` and the last 10 `decisions` (`t`, `device`, `label`, `text`, `sent`, `suppressed`, `verified`, `error`).
`enabled` and `cfg` persist in `state.json` under `watchdogs`; **`armed` never does**, so every restart is observe-only.

**`POST /api/watchdogs/{name}`** with `{"enabled": bool, "armed": bool, "cfg": {...}, "testing": bool}` (all optional).
Returns `{"ok": true, "watchdog": {...}}`; 400 with `{"error"}` for an unknown name, a wrong type, an unknown or invalid
`cfg` key, or a refused arming. Arming is refused unless the watchdog's feature (`watchdog.<name>`) is marked tested or
the request has `"testing": true`, and when its `arm_blocker` is set. Disabling also disarms. ANY change to `cfg` (or to
the setting set) disarms it, and an enable that goes from off to on starts clean (cursors and failure counts reset).
Every `cfg` value is bounded (out-of-range is a 400); text keys (`package`, `exclude`, regexes) have a length cap, and a
regex must be at least 3 characters, at most 200, and may not use backreferences, nested or alternated repeats such as
`(a|b)+`, or match the empty string.

| Watchdog | Sends | Only when |
|---|---|---|
| `stay_awake` (15 s) | WAKE (keyevent 224) | wakefulness is Asleep, Dozing or Dreaming and the headset is not intentionally asleep. A worn headset (telemetry) is not polled |
| `popup` (10 s) | BACK (4), verified after 1.5 s, at most 3 failed tries per dialog | `mCurrentFocus` title matches `Application Error` or `Application Not Responding` and `cfg.evidence` (default: the player package) matches the focus line. If the player is disconnected and the app is not in front for 2 samples: launch (60 s cooldown) |
| `overheat` (15 s) | BACK (4), 60 s cooldown | `cfg.pattern` matches a logcat line written since the last cycle (`logcat -t 1000`, filtered in Python) AND a line of the window list, player lines excluded from both. No built-in pattern |
| `black_screen` (30 s) | WAKE, or a screen refresh (`cfg.recovery`), 120 s cooldown | display OFF, player in front, Awake, not intentionally asleep and desired mode playing, for `cfg.samples` (3) samples in a row. Display ON while playing is only appended to `<data_dir>/probe/probe_YYYYMMDD.csv` (`picture` is `unsampled`; adb cannot see pixels) |
| `keepalive` (30 s) | `adb connect <ip>:5555`, then WAKE | a headset with a remembered `ip` is missing from `adb devices` (retry every 30 s). A connect that does not print `connected to` fails and skips the wake. Blocked by brakes like the rest |

**Safety rules (review fixes).**
- *History.* The 20-entry decision deque is for display only. Cooldowns, give-up counts, `connect_fails` and
  `intentionally_asleep` live in per-headset memory outside it. The attempt is recorded BEFORE the send and kept in a
  `finally`, so a send that raises still counts (as `failed`, `verified: false`). Keepalive connects back off
  exponentially after repeated failures.
- *Identity.* An IP claimed by more than one device (stale or duplicate) is refused and logged once, and acts on no
  headset; a reported serial that is listed by `adb devices` is preferred over the IP.
- *Listing.* A failed `adb devices` is distinct from an empty one: the cycle is skipped (counter `listing_failed`). A
  keepalive cycle also skips (counter `implausible`, with a log line) when more than 50% and more than 5 headsets look
  missing.
- *Regexes.* User patterns are matched in `observe()` on the executor, never on the loop thread. Logcat output is capped
  (1000 lines), each line is length-capped, matching has a time budget between lines, and an observe timeout turns a
  stuck headset into "no match" plus one log line. The stdlib `re` cannot be interrupted mid-match, so the pattern
  validator is the main defence.
- *Sends.* Sends run on a dedicated 2-worker executor (`syncvr-wd-send`). The worker re-checks the brake and the
  intentionally-asleep set right before the adb command; if either flipped while it waited, nothing is sent and the
  record is rolled back.
- *Persistence.* `intentionally_asleep` is saved in `state.json` and restored (filtered to known headsets). `pattern_confirmed`
  is stripped from `cfg` on load; confirming the pattern again is required after a restart.
- *Black-screen* does not act on a headset the player reports as not worn (`worn` is false).
- *Popup launch* (crash recovery) needs an active desired mode (playing or paused).
- *Overheat* needs a match in BOTH logcat and the window list; after BACK it verifies the prompt is gone from the window
  list (an unreachable headset is never "verified"). The logcat cursor is compared on a circular 366-day year (year
  rollover, clock behind), cleared on enable and when the headset is unreachable, re-baselined when it is later than
  the newest line, and the player's own lines never become the baseline.
- *Confirmation.* `test_pattern` with `confirm` requires a match in both `logcat.txt` and `window.txt`; the confirmed hash
  covers pattern, `exclude` and `package`.
- *Probe CSV* rotates at 5 MB and keeps 14 files; "unsampled" rows are throttled (one per headset per 5 minutes) and are
  written off the loop thread. History, cursors, memory and logged-error keys of removed devices are pruned each cycle.

**Overheat pattern.** `POST /api/watchdogs/overheat/test_pattern` with `{"folder": <snapshot folder>, "pattern": str,
"confirm": bool}` runs the pattern over that folder's `logcat.txt` and `window.txt` (player lines dropped) and returns
`{"pattern", "hash", "error", "files", "matches": [{"file", "line_no", "line"}], "total", "confirmed"}`. The folder must
be inside `<data_dir>/snapshots` (a relative path is taken from there). With `"confirm": true` it is refused for an
invalid pattern or zero matches; otherwise it stores `pattern` and `pattern_confirmed` (the first 16 hex digits of the
SHA-256 of the pattern). Arming needs `pattern_confirmed` to equal the hash of the current pattern, and
`pattern_confirmed` cannot be set through the plain endpoint.

The operator GUI shows these in the Tools tab's WATCHDOGS card: an Observe/Armed pill, an enable toggle, an Arm
button that opens a dialog quoting `rule`, the last cycle and last decision, and for overheat the pattern box and
"Test pattern against snapshot...".
