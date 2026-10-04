"""TCP endpoint the headsets connect to (newline-delimited JSON)."""

import asyncio
import logging
import socket
from urllib.parse import quote

import aiohttp
from aiohttp import web

from .protocol import MAX_LINE_BYTES, decode, encode, server_clock

log = logging.getLogger(__name__)


def runner_kwargs(timeout: float) -> dict:
    """AppRunner shutdown_timeout exists from aiohttp 3.9; older versions pass it on to the handler and crash."""
    try:
        new = tuple(int(x) for x in aiohttp.__version__.split(".")[:2]) >= (3, 9)
    except ValueError:
        new = False
    return {"shutdown_timeout": timeout} if new else {}

HELLO_TIMEOUT_S = 10.0
# Headsets send status every second; after this much silence the link is dead.
IDLE_TIMEOUT_S = 15.0
# If a headset stops reading, drop it instead of buffering forever.
MAX_WRITE_BUFFER = 4 * 1024 * 1024


class HeadsetConnection:
    def __init__(self, writer: asyncio.StreamWriter, http_port: int, content_port: int,
                 public_host: str = ""):
        self.writer = writer
        self.http_port = http_port
        self.content_port = content_port
        self.public_host = public_host
        peer = writer.get_extra_info("peername") or ("?", 0)
        local = writer.get_extra_info("sockname") or ("127.0.0.1", 0)
        self.remote_ip = peer[0]
        self.local_ip = local[0]
        self.closed = False

    def send(self, msg: dict) -> None:
        if self.closed:
            return
        transport = self.writer.transport
        if transport.is_closing():
            self.closed = True
            return
        if transport.get_write_buffer_size() > MAX_WRITE_BUFFER:
            log.warning("headset %s is not reading; dropping connection", self.remote_ip)
            self.close()
            return
        self.writer.write(encode(msg))

    def content_url(self, name: str) -> str:
        # Normally the address the headset reached us on; --public-host overrides it
        # when that address is not routable from the headset (WSL2/Docker NAT, port forwards).
        host = self.public_host or self.local_ip
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.content_port}/content/{quote(name)}"

    def close(self) -> None:
        self.closed = True
        self.writer.close()


class _Sniffer(asyncio.Protocol):
    """Looks at the first bytes of a connection and hands it to the JSON or the HTTP handler."""

    HTTP_PREFIXES = (b"GET ", b"HEAD ")

    def __init__(self, owner: "HeadsetServer"):
        self.owner = owner
        self.transport = None
        self.buffer = b""
        self.timer = None

    def connection_made(self, transport) -> None:
        self.transport = transport
        sock = transport.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self.owner._sniffers.add(self)
        self.timer = asyncio.get_running_loop().call_later(HELLO_TIMEOUT_S, transport.close)

    def data_received(self, data: bytes) -> None:
        self.buffer += data
        buf = self.buffer
        if buf[:1] != b"{" and self.owner._runner is not None:
            # Never decide on a partial "GET " / "HEAD ": a hello may arrive in tiny segments.
            if buf.startswith(self.HTTP_PREFIXES):
                self._hand_off(self.owner._runner.server())
                return
            if any(prefix.startswith(buf) for prefix in self.HTTP_PREFIXES):
                return
        reader = asyncio.StreamReader(limit=MAX_LINE_BYTES)
        self._hand_off(asyncio.StreamReaderProtocol(reader, self.owner._handle))

    def _hand_off(self, protocol) -> None:
        self._forget()
        transport, buf = self.transport, self.buffer
        self.buffer = b""
        transport.set_protocol(protocol)
        protocol.connection_made(transport)
        protocol.data_received(buf)

    def _forget(self) -> None:
        self.owner._sniffers.discard(self)
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None

    def eof_received(self):
        # Closed before saying anything useful.
        self._forget()
        return False

    def connection_lost(self, exc) -> None:
        self._forget()


class HeadsetServer:
    def __init__(self, controller, host: str, port: int, http_port: int, public_host: str = "",
                 content_app: "web.Application" = None):
        self.controller = controller
        self.host = host
        self.port = port
        self.http_port = http_port
        self.public_host = public_host
        self.content_app = content_app
        self.server = None
        self._runner = None
        self._sniffers = set()

    async def start(self) -> None:
        # Content downloads share the headset port: some networks only let this one through.
        if self.content_app is not None:
            self._runner = web.AppRunner(self.content_app, access_log=None, **runner_kwargs(2.0))
            await self._runner.setup()
        loop = asyncio.get_running_loop()
        self.server = await loop.create_server(lambda: _Sniffer(self), self.host, self.port)
        if self.port == 0:
            self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            for dev in self.controller.devices.values():
                if dev.conn is not None:
                    dev.conn.close()
            for sniffer in list(self._sniffers):
                sniffer.transport.close()
            if self._runner:
                await self._runner.cleanup()
            await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = HeadsetConnection(writer, self.http_port, self.port, self.public_host)
        dev = None
        try:
            hello = decode(await asyncio.wait_for(reader.readline(), HELLO_TIMEOUT_S))
            if hello["type"] != "hello" or not hello.get("device_id"):
                raise ValueError("expected hello with device_id")
            dev = self.controller.headset_connected(hello, conn)
            while not conn.closed:
                line = await asyncio.wait_for(reader.readline(), IDLE_TIMEOUT_S)
                if not line:
                    break
                msg = decode(line)
                if msg["type"] == "time_ping":
                    # Answer immediately: the headset's clock estimate assumes
                    # the reply timestamp sits in the middle of the round trip.
                    conn.send({"type": "time_pong", "id": msg.get("id"), "t0": msg.get("t0"),
                               "ts": server_clock()})
                    continue
                self.controller.headset_message(dev, msg)
        except asyncio.TimeoutError:
            log.info("headset %s timed out", conn.remote_ip)
        except (ConnectionError, OSError):
            pass
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("protocol error from %s: %s", conn.remote_ip, exc)
        finally:
            if dev is not None:
                self.controller.headset_disconnected(dev, conn)
            conn.close()
