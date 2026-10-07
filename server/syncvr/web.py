"""HTTP side: JSON API for the Android operator app and show-control systems, and content downloads."""

import base64
import asyncio
import binascii
import hmac
import logging

from aiohttp import web

from .controller import CommandError

log = logging.getLogger(__name__)



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
        middlewares = [make_auth_middleware(password)] if password else []
        self.app = web.Application(middlewares=middlewares, client_max_size=1024 * 1024)
        r = self.app.router
        r.add_get("/", self.index)
        r.add_get("/api/state", self.get_state)
        r.add_post("/api/command", self.post_command)
        r.add_post("/api/command/preview", self.post_preview)
        r.add_post("/api/features/{key}", self.post_feature)
        r.add_delete("/api/features/{key}", self.delete_feature)
        r.add_get("/api/settings", self.get_settings)
        r.add_post("/api/settings", self.post_settings)
        r.add_post("/api/library/rescan", self.post_rescan)
        r.add_post("/api/library/{name}", self.post_video)
        r.add_post("/api/devices/{id}", self.post_device)
        r.add_delete("/api/devices/{id}", self.delete_device)
        r.add_get("/content/{name}", self.get_content)

    async def index(self, request):
        return web.json_response({"name": "SyncVR API", "state": "/api/state", "command": "/api/command"})

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

    async def _adb_listing(self):
        """`adb devices` on the executor, so no adb process blocks the event loop."""
        fleet = self.controller.fleet
        return await asyncio.get_running_loop().run_in_executor(fleet.executor, fleet.listed)

    async def post_command(self, request):
        try:
            body = await self._body(request)
            action = body.get("action", "")
            listed = await self._adb_listing() if action in self.controller.confirm_actions else None
            result = self.controller.execute(action, body, listed=listed)
        except (CommandError, ValueError, TypeError, KeyError) as exc:
            return _json_error(400, str(exc))
        return web.json_response({"ok": True, "result": result})

    async def post_preview(self, request):
        try:
            body = await self._body(request)
            result = self.controller.preview(body, listed=await self._adb_listing())
        except (CommandError, ValueError, TypeError, KeyError) as exc:
            return _json_error(400, str(exc))
        return web.json_response(result)

    async def post_feature(self, request):
        try:
            self.controller.set_feature_tested(request.match_info["key"], True)
        except CommandError as exc:
            return _json_error(400, str(exc))
        return web.json_response({"ok": True, "key": request.match_info["key"], "tested": True})

    async def delete_feature(self, request):
        try:
            self.controller.set_feature_tested(request.match_info["key"], False)
        except CommandError as exc:
            return _json_error(400, str(exc))
        return web.json_response({"ok": True, "key": request.match_info["key"], "tested": False})

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
        response = web.FileResponse(path, chunk_size=256 * 1024)
        sha256 = self.controller.library.sha256_of(request.match_info["name"])
        if sha256:
            response.headers["X-Content-SHA256"] = sha256
        return response
