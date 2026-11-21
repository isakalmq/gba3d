"""Publish frames + entities over WebSocket for external renderers (3D engines, web pages).

    python -m shadow serve            # then open http://127.0.0.1:8765

Each frame is one binary WebSocket message on /ws:

    u32 json_length | json (UTF-8) | RGBA pixels (width * height * 4)

The JSON is World.to_dict() plus "width", "height" and "hidden_oam". The image
is the game re-rendered at 1x; by default the sprites that belong to entities
are left out so a client can draw its own models in their place. Clients can
send {"hide_entity_sprites": false} to get the original sprites back.

GET / serves a small three.js demo client (shadow/web/index.html).
"""
from __future__ import annotations

import asyncio
import json
import struct
from pathlib import Path

import numpy as np
from websockets.asyncio.server import ServerConnection, broadcast, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from .protocol import DEFAULT_PORT
from .render import Renderer
from .session import LiveSession

WEB_DIR = Path(__file__).parent / "web"
DEFAULT_HTTP_PORT = 8765


def encode_frame(world_dict: dict, rgb: np.ndarray) -> bytes:
    """Pack one frame message. `rgb` is (H, W, 3) uint8."""
    h, w = rgb.shape[:2]
    rgba = np.empty((h, w, 4), np.uint8)
    rgba[..., :3] = rgb
    rgba[..., 3] = 255
    meta = json.dumps({**world_dict, "width": w, "height": h}).encode()
    return struct.pack("<I", len(meta)) + meta + rgba.tobytes()


def decode_frame(data: bytes) -> tuple[dict, np.ndarray]:
    """Inverse of encode_frame (used by tests and Python clients)."""
    (n,) = struct.unpack_from("<I", data)
    meta = json.loads(data[4:4 + n])
    rgba = np.frombuffer(data, np.uint8, offset=4 + n).reshape(meta["height"], meta["width"], 4)
    return meta, rgba


class FrameServer:
    def __init__(self, session: LiveSession, http_host: str = "127.0.0.1", http_port: int = DEFAULT_HTTP_PORT):
        self.session = session
        self.http_host, self.http_port = http_host, http_port
        self.renderer = Renderer(scale=1)
        self.clients: set[ServerConnection] = set()
        self.hide_entity_sprites = True
        self.frames_sent = 0
        self._stop = asyncio.Event()

    # -- HTTP + WebSocket -----------------------------------------------------
    def _process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        if request.path == "/ws":
            return None                                        # continue with the WebSocket handshake
        if request.path in ("/", "/index.html"):
            body = (WEB_DIR / "index.html").read_bytes()
            headers = Headers([("Content-Type", "text/html; charset=utf-8"),
                               ("Content-Length", str(len(body))), ("Cache-Control", "no-cache")])
            return Response(200, "OK", headers, body)
        return connection.respond(404, "not found\n")

    async def _handler(self, ws: ServerConnection) -> None:
        self.clients.add(ws)
        try:
            async for message in ws:
                try:
                    cmd = json.loads(message)
                except (TypeError, ValueError):
                    continue
                if "hide_entity_sprites" in cmd:
                    self.hide_entity_sprites = bool(cmd["hide_entity_sprites"])
        finally:
            self.clients.discard(ws)

    # -- frame loop -----------------------------------------------------------
    def _make_frame(self, st) -> bytes:
        world = self.session.world(st)
        hidden = world.entity_oam if self.hide_entity_sprites else set()
        image = self.renderer.render(st, hide_oam=hidden).image
        return encode_frame({**world.to_dict(), "hidden_oam": sorted(hidden)}, image)

    async def _pump(self) -> None:
        last_seq = -1
        while not self._stop.is_set():
            st, seq = self.session.latest()
            if st is None or seq == last_seq or not self.clients:
                await asyncio.sleep(1 / 240)
                continue
            last_seq = seq
            data = await asyncio.to_thread(self._make_frame, st)
            broadcast(self.clients, data)
            self.frames_sent += 1

    async def run(self, ready: asyncio.Event | None = None) -> None:
        async with serve(self._handler, self.http_host, self.http_port,
                         process_request=self._process_request, max_size=2 ** 20) as server:
            self.http_port = server.sockets[0].getsockname()[1]
            print(f"[shadow] open http://{self.http_host}:{self.http_port}  (frames on ws://.../ws)")
            if ready:
                ready.set()
            await self._pump()

    def stop(self) -> None:
        self._stop.set()


def run_server(host: str = "127.0.0.1", port: int = DEFAULT_PORT,
               http_host: str = "127.0.0.1", http_port: int = DEFAULT_HTTP_PORT) -> int:
    session = LiveSession(host, port).start()
    print(f"[shadow] waiting for the mGBA bridge on {host}:{port} - load lua/shadow_bridge.lua in mGBA")
    server = FrameServer(session, http_host, http_port)
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        pass
    finally:
        session.stop()
    return 0
