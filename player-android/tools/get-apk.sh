#!/usr/bin/env bash
# Downloads the APK that CI built for a branch (default: the current one) from the git branch
# apk/<branch>, into ./SyncVRPlayer.apk. With --install, also installs it on the connected headset;
# with --launch, installs it and then (re)starts the app.
# With --operator, fetches ./SyncVROperator.apk (the phone/tablet operator app) instead; --install
# and --launch then act on the connected phone. Branches built before the operator app existed
# have no such file, which is reported without failing the default flow.
# Usage: get-apk.sh [branch] [--operator] [--install|--launch]   (or, after the one-time alias: git apk [--launch])
set -euo pipefail

branch="" install=0 launch=0 operator=0
for arg in "$@"; do
  case "$arg" in
    --install) install=1 ;;
    --launch) install=1 launch=1 ;;
    --operator) operator=1 ;;
    *) branch="$arg" ;;
  esac
done
[ -n "$branch" ] || branch=$(git rev-parse --abbrev-ref HEAD)

if ! git fetch -q origin "refs/heads/apk/$branch"; then
  echo "error: no APK published for '$branch' yet (CI publishes after the player-android job passes)" >&2
  exit 1
fi
if [ "$operator" = 1 ]; then
  apk=SyncVROperator.apk package=com.syncvr.operator
else
  apk=SyncVRPlayer.apk package=com.syncvr.player
fi
if ! git cat-file -e "FETCH_HEAD:$apk" 2>/dev/null; then
  echo "error: apk/$branch has no $apk (it was built before that app existed; push again and wait for CI)" >&2
  exit 1
fi
git show "FETCH_HEAD:$apk" > "$apk"
git show FETCH_HEAD:BUILD.txt

built=$(git show FETCH_HEAD:BUILD.txt | sed -n 's/^commit: //p')
latest=$(git ls-remote origin "refs/heads/$branch" | cut -f1)
if [ -n "$latest" ] && [ "$latest" != "$built" ]; then
  echo "note: $branch is now at ${latest:0:7}; CI has not published that build yet (still running or failed)."
fi
echo "Saved $(pwd)/$apk"

if [ "$install" = 1 ]; then
  adb install -r "$apk"
fi
if [ "$launch" = 1 ]; then
  adb shell am start -S -n "$package/.MainActivity"
fi
