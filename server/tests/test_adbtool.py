import argparse
from unittest import mock

from syncvr import adbtool


def run_cli(argv, calls, output="ok"):
    p = argparse.ArgumentParser()
    adbtool.add_arguments(p)
    args = p.parse_args(["--adb", "adb"] + argv)

    def fake_run(cmd, **kw):
        calls.append(cmd)
        out = output(cmd) if callable(output) else output
        return mock.Mock(stdout=out, stderr="", returncode=0)

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


ALIAS = "cmd package set-home-activity com.syncvr.player/.HomeAlias"
RESOLVE = ("cmd package resolve-activity --brief -a android.intent.action.MAIN "
           "-c android.intent.category.HOME")
BCAST_ON = "am broadcast -n com.syncvr.player/.KioskReceiver --ez on true"
BCAST_OFF = "am broadcast -n com.syncvr.player/.KioskReceiver --ez on false"
START_HOME = "am start -a android.intent.action.MAIN -c android.intent.category.HOME"


def sh(cmd):
    return ["adb", "-s", "A1", "shell", cmd]


PM_PLAYER = "pm list packages com.syncvr.player"


def fake_device(home, disabled=(), enable_fixes=True, installed=True, needs_root=False):
    """Output function emulating a headset: `home` is what HOME resolves to,
    `disabled` what `pm list packages -d` lists until `pm enable --user 0` (if enable_fixes)."""
    state = {"disabled": list(disabled), "root": False}

    def out(cmd):
        c = cmd[-1]
        if c == "root":
            state["root"] = True
        if c == "unroot":
            state["root"] = False
        if c == PM_PLAYER:
            return "package:com.syncvr.player" if installed else ""
        if needs_root and not state["root"] and c.startswith("pm enable"):
            return "Exception occurred: SecurityException: Shell cannot change component state"
        if c == "pm list packages -d":
            return "\n".join(f"package:{p}" for p in state["disabled"])
        if c.startswith("pm enable --user 0") and enable_fixes:
            state["disabled"] = []
        if c == RESOLVE:
            return f"priority=0 preferredOrder=0\n{home}"
        return "ok"
    return out


def test_kiosk_on():
    calls = []
    assert run_cli(["-s", "A1", "kiosk", "on"], calls, output=fake_device("com.syncvr.player/.HomeAlias")) == 0
    assert calls == [
        sh(PM_PLAYER),
        sh("pm disable-user --user 0 com.oculus.vrshell"),
        sh(BCAST_ON),
        sh(ALIAS),
        sh(RESOLVE),
    ]


def test_kiosk_on_root():
    calls = []
    assert run_cli(["-s", "A1", "kiosk", "on", "--root"], calls,
                   output=fake_device("com.syncvr.player/.HomeAlias")) == 0
    assert calls == [
        sh(PM_PLAYER),
        ["adb", "-s", "A1", "root"],
        ["adb", "-s", "A1", "wait-for-device"],
        sh("pm disable com.oculus.vrshell"),
        sh(BCAST_ON),
        sh(ALIAS),
        ["adb", "-s", "A1", "unroot"],
        ["adb", "-s", "A1", "wait-for-device"],
        sh(ALIAS),
        sh(RESOLVE),
    ]


def test_kiosk_on_fails_when_home_not_player():
    calls = []
    assert run_cli(["-s", "A1", "kiosk", "on"], calls,
                   output=fake_device("android/com.android.internal.app.ResolverActivity")) == 1
    assert sh("pm enable com.oculus.vrshell") in calls  # recovered


def test_kiosk_on_refuses_when_player_not_installed(capsys):
    calls = []
    assert run_cli(["-s", "A1", "kiosk", "on"], calls,
                   output=fake_device("com.oculus.vrshell/.MainActivity", installed=False)) == 1
    assert calls == [sh(PM_PLAYER)]
    assert "not installed" in capsys.readouterr().out


def test_kiosk_on_verification_failure_reenables_via_root_fallback():
    calls = []
    out = fake_device("android/com.android.internal.app.ResolverActivity",
                      disabled=["com.oculus.vrshell"], needs_root=True)
    assert run_cli(["-s", "A1", "kiosk", "on"], calls, output=out) == 1
    assert ["adb", "-s", "A1", "root"] in calls and ["adb", "-s", "A1", "unroot"] in calls
    assert calls.index(["adb", "-s", "A1", "root"]) > calls.index(sh("pm disable-user --user 0 com.oculus.vrshell"))


def test_kiosk_off_root_fallback_without_flag():
    calls = []
    out = fake_device("com.oculus.vrshell/.MainActivity", disabled=["com.oculus.vrshell"], needs_root=True)
    assert run_cli(["-s", "A1", "kiosk", "off"], calls, output=out) == 0
    assert ["adb", "-s", "A1", "root"] in calls
    assert calls.index(["adb", "-s", "A1", "unroot"]) < calls.index(sh(BCAST_OFF))


def test_restore_root_fallback_without_flag():
    calls = []
    out = fake_device("com.oculus.vrshell/.MainActivity", disabled=["com.oculus.vrshell"], needs_root=True)
    assert run_cli(["-s", "A1", "kiosk", "restore"], calls, output=out) == 0
    assert ["adb", "-s", "A1", "root"] in calls


def test_kiosk_off_still_disabled_prints_manual_commands(capsys):
    calls = []
    out = fake_device("com.oculus.vrshell/.MainActivity", disabled=["com.oculus.vrshell"],
                      enable_fixes=False, needs_root=True)
    assert run_cli(["-s", "A1", "kiosk", "off"], calls, output=out) == 1
    text = capsys.readouterr().out
    assert "adb -s A1 root" in text and "pm enable --user 0 com.oculus.vrshell" in text


def test_kiosk_off_restores_vrshell_home():
    calls = []
    assert run_cli(["-s", "A1", "kiosk", "off"], calls, output=fake_device("com.oculus.vrshell/.MainActivity")) == 0
    assert calls == [
        sh("pm enable com.oculus.vrshell"),
        sh("pm list packages -d"),  # verify
        sh("pm list packages -d"),  # final check
        sh(BCAST_OFF),
        sh("cmd package set-home-activity com.oculus.vrshell/.MainActivity"),
        sh(RESOLVE),
        sh(START_HOME),
    ]


def test_kiosk_off_retries_enable_for_user_zero():
    calls = []
    out = fake_device("com.oculus.vrshell/.MainActivity", disabled=["com.oculus.vrshell"])
    assert run_cli(["-s", "A1", "kiosk", "off"], calls, output=out) == 0
    assert sh("pm enable --user 0 com.oculus.vrshell") in calls


def test_kiosk_off_fails_loudly_when_vrshell_still_disabled(capsys):
    calls = []
    out = fake_device("com.oculus.vrshell/.MainActivity", disabled=["com.oculus.vrshell"], enable_fixes=False)
    assert run_cli(["-s", "A1", "kiosk", "off"], calls, output=out) == 1
    assert "still disabled" in capsys.readouterr().out
    assert sh(START_HOME) not in calls and sh(BCAST_OFF) not in calls


def test_kiosk_off_fails_when_home_not_vrshell():
    calls = []
    assert run_cli(["-s", "A1", "kiosk", "off"], calls, output=fake_device("com.syncvr.player/.MainActivity")) == 1
    assert sh(START_HOME) not in calls


def test_kiosk_restore_unhides_first():
    calls = []
    assert run_cli(["-s", "A1", "kiosk", "restore"], calls, output=fake_device("com.oculus.vrshell/.MainActivity")) == 0
    assert calls[0] == sh("pm unhide com.oculus.vrshell")
    assert sh("pm enable com.oculus.vrshell") in calls


def test_kiosk_restore_root_wraps_in_root():
    calls = []
    assert run_cli(["-s", "A1", "kiosk", "restore", "--root"], calls,
                   output=fake_device("com.oculus.vrshell/.MainActivity")) == 0
    assert calls[0] == ["adb", "-s", "A1", "root"]
    assert calls[-2:] == [["adb", "-s", "A1", "unroot"], ["adb", "-s", "A1", "wait-for-device"]]


def test_kiosk_reports_errors():
    calls = []
    assert run_cli(["-s", "A1", "kiosk", "on"], calls, output="Exception occurred") == 1
