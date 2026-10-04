#!/usr/bin/env bash
# Downloads the desktop app that CI built for a branch (default: the current git branch) from the
# rolling GitHub release desktop-<branch> and installs it into ~/SyncVR. On Linux it also creates a
# menu entry (~/.local/share/applications/SyncVR.desktop). Files fetched with curl are not
# quarantined, so macOS Gatekeeper does not prompt.
# Usage: get-desktop.sh [branch] [--launch]
set -euo pipefail

repo="Trigger-EX/synchronized-vr-video-playback"
dest="$HOME/SyncVR"

branch="" launch=0
for arg in "$@"; do
  case "$arg" in
    --launch) launch=1 ;;
    *) branch="$arg" ;;
  esac
done
[ -n "$branch" ] || branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null) || true
if [ -z "$branch" ] || [ "$branch" = HEAD ]; then
  echo "error: could not tell which branch to fetch; pass it: get-desktop.sh <branch>" >&2
  exit 1
fi
tag="desktop-${branch//\//-}"

os=$(uname -s) arch=$(uname -m)
case "$os-$arch" in
  Linux-x86_64) asset=SyncVR-linux-x86_64.tar.gz ;;
  Darwin-arm64) asset=SyncVR-macos-arm64.zip ;;
  *) echo "error: no desktop build for $os $arch (Linux x86_64, macOS Apple silicon and Windows only)" >&2; exit 1 ;;
esac

url="https://github.com/$repo/releases/download/$tag/$asset"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
echo "Downloading $url"
if ! curl -fL --progress-bar -o "$tmp/$asset" "$url"; then
  echo "error: no desktop build published for '$branch' yet (CI publishes after the server and desktop jobs pass)" >&2
  exit 1
fi

mkdir -p "$dest"
if [ "$os" = Linux ]; then
  rm -rf "$dest/SyncVR" "$dest/_internal"   # replace the old app, keep anything else (e.g. a 'portable' marker)
  tar -xzf "$tmp/$asset" -C "$dest" --strip-components=1
  exe="$dest/SyncVR"
  chmod +x "$exe"
  apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
  mkdir -p "$apps"
  cat > "$apps/SyncVR.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=SyncVR
Comment=Run the SyncVR server and open the operator panel
Exec="$exe"
Terminal=false
Categories=AudioVideo;
DESKTOP
  echo "Menu entry: $apps/SyncVR.desktop"
else
  rm -rf "$dest/SyncVR.app"
  ditto -x -k "$tmp/$asset" "$dest"
  exe="$dest/SyncVR.app"
fi
echo "Installed $exe"

if [ "$launch" = 1 ]; then
  if [ "$os" = Darwin ]; then open "$exe"; else nohup "$exe" >/dev/null 2>&1 & fi
fi
