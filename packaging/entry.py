"""PyInstaller entry point: the frozen app always behaves like ``python -m syncvr gui``.

Extra arguments are passed through, e.g. ``SyncVR --self-test`` or ``SyncVR --console``.
"""
import multiprocessing
import sys

from syncvr.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main(["gui"] + sys.argv[1:]))
