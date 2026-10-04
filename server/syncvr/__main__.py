"""Command line entry point: ``syncvr serve | sim | adb``."""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from . import __version__, adbtool, sim
from .protocol import DEFAULT_DISCOVERY_PORT, DEFAULT_HTTP_PORT, DEFAULT_TCP_PORT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="syncvr", description="Synchronized video playback for VR headset fleets")
    parser.add_argument("--version", action="version", version=f"syncvr {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the operator server and dashboard")
    serve.add_argument("--content", default="content", help="folder holding the video files (default: ./content)")
    serve.add_argument("--data", default="data", help="folder for saved state (default: ./data)")
    serve.add_argument("--host", default="0.0.0.0", help="interface to listen on")
    serve.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT, help="dashboard/content port")
    serve.add_argument("--tcp-port", type=int, default=DEFAULT_TCP_PORT, help="headset control port")
    serve.add_argument("--discovery-port", type=int, default=DEFAULT_DISCOVERY_PORT, help="UDP beacon port")
    serve.add_argument("--broadcast", action="append", metavar="ADDR",
                       help="beacon destination (repeatable), e.g. 192.168.1.255; default: auto")
    serve.add_argument("--no-discovery", action="store_true", help="do not broadcast beacons")
    serve.add_argument("--name", default="SyncVR", help="server name shown on headsets")
    serve.add_argument("--public-host", default="", metavar="IP",
                       help="address headsets download from (default: the address they connected to; "
                            "set the PC's LAN IP under WSL2/Docker)")
    serve.add_argument("--password", default="", help="require this password for the dashboard/API")
    serve.add_argument("--max-downloads", type=int, default=None,
                       help="headsets downloading content at the same time (0 = unlimited, default 4)")

    gui = sub.add_parser("gui", help="PC control window: runs the server and opens the dashboard")
    gui.add_argument("--content", default=None, help="video folder (default: server/content)")
    gui.add_argument("--data", default=None, help="saved state folder (default: server/data)")
    gui.add_argument("--http-port", type=int, default=None, help="dashboard port (default 8080)")
    gui.add_argument("--no-discovery", action="store_true", help="do not broadcast beacons")
    gui.add_argument("--no-browser", action="store_true", help="do not open the browser on start")
    gui.add_argument("--console", action="store_true", help="skip the window, run in the terminal")

    simp = sub.add_parser("sim", help="run simulated headsets against a server")
    sim.add_arguments(simp)

    adbp = sub.add_parser("adb", help="provision physical headsets over USB/ADB")
    adbtool.add_arguments(adbp)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "gui":
        from . import launcher  # sets up its own file logging
        try:
            return launcher.main(args)
        except KeyboardInterrupt:
            return 0
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    try:
        if args.command == "serve":
            from .app import ServerConfig, serve_forever  # lazy: needs aiohttp, which adb/sim do not
            config = ServerConfig(
                content_dir=Path(args.content), data_dir=Path(args.data), host=args.host,
                http_port=args.http_port, tcp_port=args.tcp_port, discovery_port=args.discovery_port,
                broadcast=args.broadcast, discovery=not args.no_discovery, name=args.name,
                password=args.password, public_host=args.public_host, max_downloads=args.max_downloads)
            asyncio.run(serve_forever(config))
        elif args.command == "sim":
            asyncio.run(sim.run_fleet(args))
        elif args.command == "adb":
            return adbtool.run(args)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
