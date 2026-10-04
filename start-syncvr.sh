#!/bin/sh
# Starts the SyncVR server with its control window. First run sets up server/.venv (downloads ~100 MB).
cd "$(dirname "$0")/server" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required: sudo apt install python3 python3-venv"
    exit 1
fi
# Needs a first-time install but was started without a terminal (file manager)? Re-run inside one.
if [ ! -t 1 ] && [ -z "$SYNCVR_IN_TERM" ] && [ ! -f .venv/.syncvr-deps ] && [ -z "$SYNCVR_NO_VENV" ]; then
    export SYNCVR_IN_TERM=1
    self="$(cd .. && pwd)/start-syncvr.sh"
    if command -v x-terminal-emulator >/dev/null 2>&1; then
        exec x-terminal-emulator -e "$self" "$@"
    elif command -v gnome-terminal >/dev/null 2>&1; then
        exec gnome-terminal -- "$self" "$@"
    fi
fi
exec python3 -m syncvr.bootstrap gui "$@"
