import argparse
from unittest import mock

from syncvr import adbtool


def run_cli(argv, calls, output="ok"):
    p = argparse.ArgumentParser()
    adbtool.add_arguments(p)
    args = p.parse_args(["--adb", "adb"] + argv)

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return mock.Mock(stdout=output, stderr="", returncode=0)

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


HOME = "cmd package set-home-activity com.syncvr.player/.MainActivity"


def test_kiosk_on():
    calls = []
    assert run_cli(["-s", "A1", "kiosk", "on"], calls) == 0
    assert calls == [
        ["adb", "-s", "A1", "shell", "pm disable-user --user 0 com.oculus.vrshell"],
        ["adb", "-s", "A1", "shell", HOME],
    ]


def test_kiosk_on_root():
    calls = []
    assert run_cli(["-s", "A1", "kiosk", "on", "--root"], calls) == 0
    assert calls == [
        ["adb", "-s", "A1", "root"],
        ["adb", "-s", "A1", "wait-for-device"],
        ["adb", "-s", "A1", "shell", "pm disable com.oculus.vrshell"],
        ["adb", "-s", "A1", "shell", HOME],
        ["adb", "-s", "A1", "unroot"],
        ["adb", "-s", "A1", "wait-for-device"],
        ["adb", "-s", "A1", "shell", HOME],
    ]


def test_kiosk_off_restores_vrshell_home():
    calls = []
    assert run_cli(["-s", "A1", "kiosk", "off"], calls, output="priority=0\ncom.oculus.vrshell/.HomeActivity") == 0
    assert calls == [
        ["adb", "-s", "A1", "shell", "pm enable com.oculus.vrshell"],
        ["adb", "-s", "A1", "shell",
         "cmd package resolve-activity --brief -a android.intent.action.MAIN "
         "-c android.intent.category.HOME com.oculus.vrshell"],
        ["adb", "-s", "A1", "shell", "cmd package set-home-activity com.oculus.vrshell/.HomeActivity"],
    ]


def test_kiosk_reports_errors():
    calls = []
    assert run_cli(["-s", "A1", "kiosk", "on"], calls, output="Exception occurred") == 1
