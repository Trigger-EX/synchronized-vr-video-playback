#!/bin/sh
# Starts the SyncVR server with its control window (falls back to the terminal without a display).
cd "$(dirname "$0")/server" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required: sudo apt install python3 python3-tk python3-aiohttp"
    exit 1
fi
exec python3 -m syncvr gui "$@"
