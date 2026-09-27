"""UDP broadcast beacon so headsets find the server without any IP configuration."""

import asyncio
import ipaddress
import json
import logging
import socket
from typing import Callable, List

log = logging.getLogger(__name__)


def local_ipv4_addresses() -> List[str]:
    """Best-effort list of this machine's LAN IPv4 addresses."""
    found = []
    # The address the OS would use to reach the outside world (no packet is sent).
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    result = []
    for ip in found:
        if ip not in result and not ip.startswith("127."):
            result.append(ip)
    return result


def default_broadcast_addresses() -> List[str]:
    """Limited broadcast plus a /24 directed broadcast for each local address.

    Directed broadcasts matter on machines with several interfaces, where
    255.255.255.255 only leaves through one of them. Pass explicit addresses
    with --broadcast when the headset network is not a /24.
    """
    addrs = ["255.255.255.255"]
    for ip in local_ipv4_addresses():
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
        addrs.append(str(net.broadcast_address))
    return list(dict.fromkeys(addrs))


class DiscoveryBeacon:
    def __init__(self, port: int, payload: Callable[[], dict], addresses: List[str], interval: float = 1.0):
        self.port = port
        self.payload = payload
        self.addresses = addresses
        self.interval = interval
        self._transport = None
        self._task = None
        self._failed = set()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=("0.0.0.0", 0), allow_broadcast=True)
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._transport:
            self._transport.close()

    async def _run(self) -> None:
        while True:
            data = json.dumps(self.payload(), separators=(",", ":")).encode("utf-8")
            for addr in self.addresses:
                try:
                    self._transport.sendto(data, (addr, self.port))
                except OSError as exc:
                    if addr not in self._failed:
                        self._failed.add(addr)
                        log.warning("cannot broadcast to %s: %s", addr, exc)
            await asyncio.sleep(self.interval)
