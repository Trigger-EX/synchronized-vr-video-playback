"""TCP endpoint the headsets connect to (newline-delimited JSON)."""

import asyncio
import logging
import socket
from urllib.parse import quote

from .protocol import MAX_LINE_BYTES, decode, encode, server_clock

log = logging.getLogger(__name__)

HELLO_TIMEOUT_S = 10.0
# Headsets send status every second; after this much silence the link is dead.
IDLE_TIMEOUT_S = 15.0
# If a headset stops reading, drop it instead of buffering forever.
MAX_WRITE_BUFFER = 4 * 1024 * 1024


class HeadsetConnection:
    def __init__(self, writer: asyncio.StreamWriter, http_port: int):
        self.writer = writer
        self.http_port = http_port
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
        # The address the headset reached us on is by definition reachable from it.
        host = self.local_ip
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.http_port}/content/{quote(name)}"

    def close(self) -> None:
        self.closed = True
        self.writer.close()


class HeadsetServer:
    def __init__(self, controller, host: str, port: int, http_port: int):
        self.controller = controller
        self.host = host
        self.port = port
        self.http_port = http_port
        self.server = None

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, self.host, self.port, limit=MAX_LINE_BYTES)
        if self.port == 0:
            self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            for dev in self.controller.devices.values():
                if dev.conn is not None:
                    dev.conn.close()
            await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        conn = HeadsetConnection(writer, self.http_port)
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
