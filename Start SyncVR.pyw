"""Windows double-click launcher (runs without a console window). WSL users: run this from Windows."""
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, "server"))
os.chdir(os.path.join(here, "server"))

from syncvr.__main__ import main  # noqa: E402

sys.exit(main(["gui"]))
