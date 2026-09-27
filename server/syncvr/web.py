"""HTTP side: operator dashboard, JSON API, live WebSocket feed and content downloads."""

import asyncio
import base64
import binascii
import hmac
import json
import logging
from pathlib import Path

from aiohttp import WSMsgType, web

from .controller import CommandError

log = logging.getLogger(__name__)

WEB_ROOT = Path(__file__).parent / "web"
PUSH_INTERVAL_S = 0.4


def _json_error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


def make_auth_middleware(password: str):
    @web.middleware
    async def auth(request: web.Request, handler):
        # Headsets download content without credentials.
        if request.path.startswith("/content/"):
            return await handler(request)
        header = request.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                if hmac.compare_digest(given, password):
                    return await handler(request)
            except (binascii.Error, UnicodeDecodeError):
                pass
        return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="SyncVR"'})
    return auth


class WebApp:
    def __init__(self, controller, password: str = ""):
        self.controller = controller
        self.sockets = set()
        self._wake = asyncio.Event()
        self._pusher = None
        middlewares = [make_auth_middleware(password)] if password else []
        self.app = web.Application(middlewares=middlewares, client_max_size=1024 * 1024)
        r = self.app.router
        r.add_get("/", self.index)
        r.add_static("/static", WEB_ROOT)
        r.add_get("/ws", self.websocket)
        r.add_get("/api/state", self.get_state)
        r.add_post("/api/command", self.post_command)
        r.add_get("/api/settings", self.get_settings)
        r.add_post("/api/settings", self.post_settings)
        r.add_post("/api/library/rescan", self.post_rescan)
        r.add_post("/api/library/{name}", self.post_video)
        r.add_post("/api/devices/{id}", self.post_device)
        r.add_delete("/api/devices/{id}", self.delete_device)
        r.add_get("/content/{name}", self.get_content)
        self.app.on_startup.append(self._on_startup)
        self.app.on_shutdown.append(self._on_shutdown)
        controller.add_listener(self._wake.set)

    async def _on_startup(self, app):
        self._pusher = asyncio.create_task(self._push_loop())

    async def _on_shutdown(self, app):
        if self._pusher:
            self._pusher.cancel()
        for ws in list(self.sockets):
            await ws.close()

    async def _push_loop(self):
        while True:
            await self._wake.wait()
            self._wake.clear()
            if self.sockets:
                data = json.dumps({"type": "state", "state": self.controller.snapshot()})
                for ws in list(self.sockets):
                    try:
                        await ws.send_str(data)
                    except (ConnectionError, RuntimeError):
                        self.sockets.discard(ws)
            await asyncio.sleep(PUSH_INTERVAL_S)

    async def index(self, request):
        return web.FileResponse(WEB_ROOT / "index.html")

    async def websocket(self, request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self.sockets.add(ws)
        try:
            await ws.send_str(json.dumps({"type": "state", "state": self.controller.snapshot()}))
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                req_id = None
                try:
                    req = json.loads(msg.data)
                    if not isinstance(req, dict):
                        raise CommandError("expected a JSON object")
                    req_id = req.get("id")
                    result = self.controller.execute(req.get("action", ""), req)
                    reply = {"type": "result", "id": req_id, "ok": True, "result": result}
                except (CommandError, ValueError, TypeError, KeyError) as exc:
                    reply = {"type": "result", "id": req_id, "ok": False, "error": str(exc)}
                await ws.send_str(json.dumps(reply))
        finally:
            self.sockets.discard(ws)
        return ws

    async def get_state(self, request):
        return web.json_response(self.controller.snapshot())

    async def _body(self, request) -> dict:
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise CommandError("request body must be JSON")
        if not isinstance(body, dict):
            raise CommandError("request body must be a JSON object")
        return body

    async def post_command(self, request):
        try:
            body = await self._body(request)
            result = self.controller.execute(body.get("action", ""), body)
        except (CommandError, ValueError, TypeError, KeyError) as exc:
            return _json_error(400, str(exc))
        return web.json_response({"ok": True, "result": result})

    async def get_settings(self, request):
        return web.json_response(self.controller.settings)

    async def post_settings(self, request):
        try:
            body = await self._body(request)
            if "max_downloads" in body:
                self.controller.set_max_downloads(body.pop("max_downloads"))
            settings = self.controller.update_settings(body)
        except (CommandError, ValueError, TypeError) as exc:
            return _json_error(400, str(exc))
        return web.json_response(settings)

    async def post_rescan(self, request):
        self.controller.rescan()
        return web.json_response({"ok": True, "videos": len(self.controller.library.videos)})

    async def post_video(self, request):
        try:
            video = self.controller.update_video(request.match_info["name"], await self._body(request))
        except CommandError as exc:
            return _json_error(400, str(exc))
        return web.json_response({"ok": True, "video": video.name})

    async def post_device(self, request):
        try:
            dev = self.controller.update_device(request.match_info["id"], await self._body(request))
        except CommandError as exc:
            return _json_error(400, str(exc))
        return web.json_response({"ok": True, "device": dev.to_json()})

    async def delete_device(self, request):
        try:
            self.controller.forget_device(request.match_info["id"])
        except CommandError as exc:
            return _json_error(400, str(exc))
        return web.json_response({"ok": True})

    async def get_content(self, request):
        path = self.controller.library.path_of(request.match_info["name"])
        if path is None or not path.is_file():
            raise web.HTTPNotFound()
        # FileResponse implements Range requests, which headsets use to resume.
        return web.FileResponse(path, chunk_size=256 * 1024)
