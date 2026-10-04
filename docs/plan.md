# Plan: serve content downloads on the headset TCP port (8765)

Why: 8080 is blocked for headsets; 8765 works. Dashboard stays on 8080. No Kotlin change
(player fetches whatever URL the server sends; HttpURLConnection, Range, 200/206/416).

## Approach
Replace `asyncio.start_server` in `server/syncvr/headset_server.py` with `loop.create_server`
and a small sniffing `asyncio.Protocol`. Buffer the first bytes of each connection, then:
- `{` or anything else -> `asyncio.StreamReaderProtocol(StreamReader(limit=MAX_LINE_BYTES), self._handle)` (JSON path unchanged).
- `GET ` / `HEAD ` -> aiohttp `RequestHandler` from `AppRunner(content_app).server()`.
Handoff: `transport.set_protocol(p)`, `p.connection_made(transport)`, `p.data_received(buffered)`.
`content_app` has only `/content/{name}` -> `WebApp.get_content` (reuses FileResponse: Range, sendfile, X-Content-SHA256).
Throttle is in controller's Distributor, nothing to do.

## Steps
1. headset_server.py: `_Sniffer` protocol. connection_made: NODELAY/KEEPALIVE + HELLO_TIMEOUT_S timer closing undecided transport. data_received: decide on `{` immediately, else wait up to 5 bytes for `GET `/`HEAD `; mismatch -> JSON path. Handle EOF/loss before decision; track undecided sniffers for stop().
2. HeadsetServer.__init__(content_app=None); start() builds `AppRunner(content_app, access_log=None)`; content_url() uses resolved TCP port, not http_port.
3. stop(): close listener, devices, sniffers, then `await runner.cleanup()`, then `wait_closed()`.
4. app.py: build content-only `web.Application` from `self.web.get_content`, pass to HeadsetServer. 8080 keeps full routes.
5. docs/PROTOCOL.md, docs/HEADSET_SETUP.md: content served on TCP port; only dashboard needs 8080.

## Risks
Hello split across packets / 1-byte first segment (never decide on partial `G`/`H`); HTTP keep-alive uses aiohttp's 75s, not 15s idle; non-/content paths on 8765 -> 404; no password on content (as today).

## Tests (server/tests/test_integration.py)
GET/HEAD/Range bytes=10-19 on tcp_port (206, bytes, SHA header); `..%2F` -> 404; silent connection closed after (monkeypatched) hello timeout; hello in 1-byte writes connects; manifest URL contains `:{tcp_port}/content/`; stop() completes with an open keep-alive HTTP connection. All 82 existing tests must pass.
