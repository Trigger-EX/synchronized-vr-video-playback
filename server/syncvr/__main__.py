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
    serve.add_argument("--password", default="", help="require this password for the dashboard/API")
    serve.add_argument("--max-downloads", type=int, default=None,
                       help="headsets downloading content at the same time (0 = unlimited, default 4)")

    simp = sub.add_parser("sim", help="run simulated headsets against a server")
    sim.add_arguments(simp)

    adbp = sub.add_parser("adb", help="provision physical headsets over USB/ADB")
    adbtool.add_arguments(adbp)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    try:
        if args.command == "serve":
            from .app import ServerConfig, serve_forever  # lazy: needs aiohttp, which adb/sim do not
            config = ServerConfig(
                content_dir=Path(args.content), data_dir=Path(args.data), host=args.host,
                http_port=args.http_port, tcp_port=args.tcp_port, discovery_port=args.discovery_port,
                broadcast=args.broadcast, discovery=not args.no_discovery, name=args.name,
                password=args.password, max_downloads=args.max_downloads)
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
