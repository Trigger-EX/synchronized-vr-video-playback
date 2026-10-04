# Handoff

## Goal
SyncVR: synchronized video playback on Oculus Go (Python server `server/`, native Kotlin/C++ player `player-android/`). Current focus: headset downloads hung at 0% ("connect timed out").

## Decisions and constraints
- Cause: headsets reach TCP 8765 but not HTTP 8080 on the user's Linux Mint hotspot host (likely firewall). User refuses to open 8080.
- Decision: serve `/content` on the TCP port 8765 by sniffing `GET `/`HEAD ` on the first bytes; 8080 is dashboard only. No Kotlin change (player uses the URL the server sends).
- `--public-host IP` added to override the download host (WSL2/Docker NAT); probably not needed here.
- Design in `docs/plan.md` (delete it and this file before any PR).
- Tests need pytest-asyncio.

## Current state
- Branch `testing`, pushed. Commits: `--public-host`, plan, single-port downloads.
- Files: `server/syncvr/headset_server.py` (`_Sniffer`, aiohttp AppRunner), `app.py`, `__main__.py`, `server/tests/test_integration.py`, `docs/PROTOCOL.md`, `docs/HEADSET_SETUP.md`.
- 87 server tests pass. Not yet verified on hardware.

## Open questions
- Does a download complete over 8765 on the headset (and resume after interruption)?
- Dashboard shows no error for Load/Play with no headset online; leftover Unity mentions (controller.py default, protocol.py, PROTOCOL.md, Kotlin comments).

## Next step
User retests on the headset: pull `testing`, restart `python3 -m syncvr serve`, push a video. If it still fails, get `serve -v` logs and `adb logcat -s SyncVR`.
