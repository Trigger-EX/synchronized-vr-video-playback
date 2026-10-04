#!/usr/bin/env bash
# Downloads the APK that CI built for a branch (default: the current one) from the git branch
# apk/<branch>, into ./SyncVRPlayer.apk. With --install, also installs it on the connected headset.
# Usage: get-apk.sh [branch] [--install]     (or, after the one-time alias: git apk [--install])
set -euo pipefail

branch="" install=0
for arg in "$@"; do
  case "$arg" in
    --install) install=1 ;;
    *) branch="$arg" ;;
  esac
done
[ -n "$branch" ] || branch=$(git rev-parse --abbrev-ref HEAD)

if ! git fetch -q origin "refs/heads/apk/$branch"; then
  echo "error: no APK published for '$branch' yet (CI publishes after the player-android job passes)" >&2
  exit 1
fi
git show FETCH_HEAD:SyncVRPlayer.apk > SyncVRPlayer.apk
git show FETCH_HEAD:BUILD.txt

built=$(git show FETCH_HEAD:BUILD.txt | sed -n 's/^commit: //p')
latest=$(git ls-remote origin "refs/heads/$branch" | cut -f1)
if [ -n "$latest" ] && [ "$latest" != "$built" ]; then
  echo "note: $branch is now at ${latest:0:7}; CI has not published that build yet (still running or failed)."
fi
echo "Saved $(pwd)/SyncVRPlayer.apk"

if [ "$install" = 1 ]; then
  adb install -r SyncVRPlayer.apk
fi
