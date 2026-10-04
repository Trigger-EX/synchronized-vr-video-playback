"""Provision a fleet of headsets over ADB, in parallel.

Plug headsets in over USB (a powered hub works well) or connect them over
Wi-Fi ADB, then e.g.:

    python -m syncvr adb devices
    python -m syncvr adb setup SyncVRPlayer.apk --server 192.168.1.10
    python -m syncvr adb push content/*.mp4

Pushing content over USB is much faster than Wi-Fi for large fleets; files
land where the player app looks for them, so the server sees them as present.
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List

DEFAULT_PACKAGE = "com.syncvr.player"
USB_PORTS = (8765, 8080)  # TCP control, HTTP content


class Adb:
    def __init__(self, adb: str = "adb", package: str = DEFAULT_PACKAGE):
        self.adb = adb
        self.package = package

    @property
    def files_dir(self) -> str:
        return f"/sdcard/Android/data/{self.package}/files"

    @property
    def videos_dir(self) -> str:
        return f"{self.files_dir}/videos"

    def run(self, serial, *args, timeout=None, check=True) -> str:
        cmd = [self.adb] + (["-s", serial] if serial else []) + list(args)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (proc.stdout + proc.stderr).strip()
        if check and proc.returncode != 0:
            raise RuntimeError(out or f"adb exited with {proc.returncode}")
        return out

    def shell(self, serial, command: str, **kw) -> str:
        return self.run(serial, "shell", command, **kw)

    def devices(self) -> List[str]:
        out = self.run(None, "devices")
        serials = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                serials.append(parts[0])
        return serials


def parse_battery(dumpsys: str) -> str:
    level = temp = None
    for line in dumpsys.splitlines():
        key, _, value = line.strip().partition(":")
        if key == "level":
            level = value.strip()
        elif key == "temperature":
            try:
                temp = int(value) / 10.0
            except ValueError:
                pass
    parts = []
    if level is not None:
        parts.append(f"battery {level}%")
    if temp is not None:
        parts.append(f"{temp:.1f}°C")
    return ", ".join(parts)


def for_each(serials, fn, workers=16) -> int:
    failures = 0

    def task(serial):
        try:
            return serial, True, fn(serial)
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            return serial, False, str(exc)

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(serials)))) as pool:
        for serial, ok, result in pool.map(task, serials):
            status = "ok " if ok else "ERR"
            print(f"[{status}] {serial}: {result}" if result else f"[{status}] {serial}")
            failures += not ok
    return failures


def run(args) -> int:
    adb = Adb(args.adb, args.package)
    if shutil.which(adb.adb) is None and not os.path.exists(adb.adb):
        print(f"adb not found ({adb.adb}). Install Android platform-tools or pass --adb PATH.")
        return 2
    if args.adb_command == "connect":
        for address in args.addresses:
            if ":" not in address:
                address += ":5555"
            print(adb.run(None, "connect", address, check=False))
        return 0

    serials = args.serial or adb.devices()
    if not serials:
        print("no headsets found; check USB cables, developer mode and the 'Allow USB debugging' prompt")
        return 1
    cmd = args.adb_command
    print(f"{len(serials)} headset(s)")

    def install(s):
        return adb.run(s, "install", "-r", "-g", args.apk, timeout=600)

    def launch(s):
        if getattr(args, "usb", False):
            return launch_usb(s)
        return adb.shell(s, f"monkey -p {adb.package} -c android.intent.category.LAUNCHER 1 >/dev/null && echo launched")

    def launch_usb(s):
        for port in USB_PORTS:
            adb.run(s, "reverse", f"tcp:{port}", f"tcp:{port}")
        adb.shell(s, f"am start -n {adb.package}/.MainActivity --es server 127.0.0.1")
        return "launched (USB, adb reverse)"

    def prox_off(s):
        # Keeps the headset awake and playing when nobody is wearing it (until reboot).
        return adb.shell(s, "am broadcast -a com.oculus.vrpowermanager.prox_close >/dev/null && echo proximity sensor disabled")

    def write_config(s, server):
        cfg = {"server": server} if server else {}
        if getattr(args, "server_name", None):
            cfg["server_name"] = args.server_name
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(cfg, f)
        try:
            adb.shell(s, f"mkdir -p {adb.files_dir}")
            adb.run(s, "push", f.name, f"{adb.files_dir}/config.json")
        finally:
            os.unlink(f.name)
        return f"config: {cfg or 'auto-discovery'}"

    if cmd == "devices":
        def info(s):
            model = adb.shell(s, "getprop ro.product.model", check=False)
            return f"{model}, {parse_battery(adb.shell(s, 'dumpsys battery', check=False))}"
        return for_each(serials, info)
    if cmd == "install":
        return for_each(serials, install)
    if cmd == "setup":
        def setup(s):
            steps = [install(s).splitlines()[-1]]
            steps.append(write_config(s, args.server))
            if not args.keep_proximity:
                steps.append(prox_off(s))
            steps.append(launch(s))
            return "; ".join(x for x in steps if x)
        return for_each(serials, setup)
    if cmd == "configure":
        return for_each(serials, lambda s: write_config(s, args.server))
    if cmd == "push":
        missing = [f for f in args.files if not Path(f).is_file()]
        if missing:
            print("not found: " + ", ".join(missing))
            return 1

        def push(s):
            adb.shell(s, f"mkdir -p {adb.videos_dir}")
            for f in args.files:
                adb.run(s, "push", f, f"{adb.videos_dir}/{Path(f).name}", timeout=None)
            return f"{len(args.files)} file(s) pushed"
        return for_each(serials, push, workers=args.parallel)
    if cmd == "list":
        return for_each(serials, lambda s: adb.shell(s, f"ls -l {adb.videos_dir}", check=False))
    if cmd == "launch":
        return for_each(serials, launch)
    if cmd == "stop":
        return for_each(serials, lambda s: adb.shell(s, f"am force-stop {adb.package}") or "stopped")
    if cmd == "prox-off":
        return for_each(serials, prox_off)
    if cmd == "prox-on":
        return for_each(serials, lambda s: adb.shell(
            s, "am broadcast -a com.oculus.vrpowermanager.automation_disable >/dev/null && echo proximity sensor enabled"))
    if cmd == "wifi":
        def wifi(s):
            addr = None
            for line in adb.shell(s, "ip -f inet addr show wlan0", check=False).splitlines():
                parts = line.split()
                if len(parts) > 1 and parts[0] == "inet":
                    addr = parts[1].split("/")[0]
                    break
            adb.run(s, "tcpip", "5555")
            return f"Wi-Fi ADB on {addr}:5555" if addr else "Wi-Fi ADB enabled (no wlan0 address found)"
        return for_each(serials, wifi)
    if cmd == "reboot":
        return for_each(serials, lambda s: adb.run(s, "reboot") or "rebooting")
    if cmd == "shell":
        command = " ".join(shlex.quote(a) for a in args.shell_command)
        return for_each(serials, lambda s: adb.shell(s, command, check=False))
    raise AssertionError(cmd)


USB_HELP = "reach the server over adb reverse (127.0.0.1; ports 8765, 8080), no firewall rules needed"


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--adb", default=os.environ.get("ADB", "adb"), help="path to adb")
    p.add_argument("--package", default=DEFAULT_PACKAGE, help="headset app package name")
    p.add_argument("-s", "--serial", action="append", help="only this headset (repeatable); default: all")
    sub = p.add_subparsers(dest="adb_command", required=True)
    sub.add_parser("devices", help="list connected headsets with battery and temperature")
    x = sub.add_parser("install", help="install/update the player APK")
    x.add_argument("apk")
    x = sub.add_parser("setup", help="install APK, write config, disable proximity sensor, launch")
    x.add_argument("apk")
    x.add_argument("--server", help="server IP to use instead of auto-discovery")
    x.add_argument("--server-name", help="only accept a discovered server with this name")
    x.add_argument("--usb", action="store_true", help=USB_HELP)
    x.add_argument("--keep-proximity", action="store_true", help="leave the proximity sensor enabled")
    x = sub.add_parser("configure", help="write the headset config file")
    x.add_argument("--server", help="server IP (omit for auto-discovery)")
    x.add_argument("--server-name", help="only accept a discovered server with this name")
    x = sub.add_parser("push", help="copy video files to the headsets over USB")
    x.add_argument("files", nargs="+")
    x.add_argument("--parallel", type=int, default=4, help="headsets to copy to at once")
    sub.add_parser("list", help="list video files on the headsets")
    x = sub.add_parser("launch", help="start the player app")
    x.add_argument("--usb", action="store_true", help=USB_HELP)
    sub.add_parser("stop", help="force-stop the player app")
    sub.add_parser("prox-off", help="disable the proximity sensor until reboot")
    sub.add_parser("prox-on", help="re-enable the proximity sensor")
    sub.add_parser("wifi", help="switch headsets to Wi-Fi ADB (port 5555)")
    x = sub.add_parser("connect", help="connect to headsets over Wi-Fi ADB")
    x.add_argument("addresses", nargs="+")
    sub.add_parser("reboot", help="reboot the headsets")
    x = sub.add_parser("shell", help="run a shell command on every headset")
    x.add_argument("shell_command", nargs=argparse.REMAINDER)
