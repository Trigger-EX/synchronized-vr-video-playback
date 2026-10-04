import argparse
from unittest import mock

from syncvr import adbtool


def run_cli(argv, calls):
    p = argparse.ArgumentParser()
    adbtool.add_arguments(p)
    args = p.parse_args(["--adb", "adb"] + argv)

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return mock.Mock(stdout="ok", stderr="", returncode=0)

    with mock.patch.object(adbtool.subprocess, "run", fake_run), \
            mock.patch.object(adbtool.shutil, "which", return_value="adb"):
        return adbtool.run(args)


def test_launch_usb_reverses_ports_and_passes_server():
    calls = []
    assert run_cli(["-s", "A1", "launch", "--usb"], calls) == 0
    assert calls == [
        ["adb", "-s", "A1", "reverse", "tcp:8765", "tcp:8765"],
        ["adb", "-s", "A1", "reverse", "tcp:8080", "tcp:8080"],
        ["adb", "-s", "A1", "shell",
         "am start -n com.syncvr.player/.MainActivity --es server 127.0.0.1"],
    ]


def test_launch_without_usb_unchanged():
    calls = []
    assert run_cli(["-s", "A1", "launch"], calls) == 0
    assert len(calls) == 1 and "reverse" not in calls[0] and "monkey" in calls[0][-1]


def test_setup_usb_launches_with_reverse():
    calls = []
    assert run_cli(["-s", "A1", "setup", "x.apk", "--usb"], calls) == 0
    assert ["adb", "-s", "A1", "reverse", "tcp:8765", "tcp:8765"] in calls
    assert any("--es server 127.0.0.1" in c[-1] for c in calls)
