"""Publish frames + entities over WebSocket for external renderers (3D engines, web pages).

    python -m shadow serve            # then open http://127.0.0.1:8765

Each frame is one binary WebSocket message on /ws:

    u32 json_length | json (UTF-8) | for each name in json["layers"]: u32 n | n bytes of RGBA

The JSON is World.to_dict() plus "width", "height", "layers", "hidden_oam" and
"front_of". The images are the game re-rendered at 1x, split so a client can
draw its own models in the game's own draw order (n = 0 means the layer is empty):

    behind  the frame without the entities' sprites and without UI layers (opaque)
    front   what the GBA draws in front of the (first) entity's sprite: map layers
            such as Pokemon's roofs, bed covers and treetops, and other sprites
    hud     screen UI layers (text boxes, menus), as declared by the adapter

Draw behind, then the models, then front, then hud. Clients can send
{"hide_entity_sprites": false} to get a single unsplit frame with the original sprites.

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


LAYERS = ("behind", "front", "hud")


def _as_rgba(img: np.ndarray) -> np.ndarray:
    if img.shape[2] == 4:
        return img
    rgba = np.empty(img.shape[:2] + (4,), np.uint8)
    rgba[..., :3] = img
    rgba[..., 3] = 255
    return rgba


def encode_frame(world_dict: dict, layers: dict[str, np.ndarray | None]) -> bytes:
    """Pack one frame message. `layers` maps name -> (H, W, 3|4) uint8 image or None (empty)."""
    first = next(img for img in layers.values() if img is not None)
    h, w = first.shape[:2]
    meta = json.dumps({**world_dict, "width": w, "height": h, "layers": list(layers)}).encode()
    parts = [struct.pack("<I", len(meta)), meta]
    for img in layers.values():
        data = b"" if img is None else _as_rgba(img).tobytes()
        parts += [struct.pack("<I", len(data)), data]
    return b"".join(parts)


def decode_frame(data: bytes) -> tuple[dict, dict[str, np.ndarray | None]]:
    """Inverse of encode_frame (used by tests and Python clients). Images come back as RGBA."""
    (n,) = struct.unpack_from("<I", data)
    meta = json.loads(data[4:4 + n])
    pos, layers = 4 + n, {}
    for name in meta["layers"]:
        (size,) = struct.unpack_from("<I", data, pos)
        pos += 4
        layers[name] = (np.frombuffer(data, np.uint8, size, pos).reshape(meta["height"], meta["width"], 4)
                        if size else None)
        pos += size
    return meta, layers


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
        meta = world.to_dict()
        if not self.hide_entity_sprites:
            image = self.renderer.render(st).image
            return encode_frame({**meta, "hidden_oam": [], "front_of": None}, {"behind": image})
        hidden = world.entity_oam
        front_of = None
        # Layering is computed relative to the first entity that is drawn by sprites
        # (the player): its sprite priority, and its lowest OAM slot for ties.
        main = next((e for e in world.entities if e.oam), None)
        if main:
            prio = min((int(st.oam16[i * 4 + 2]) >> 10) & 3 for i in main.oam)
            front_of = {"entity": main.id, "priority": prio, "slot": min(main.oam)}
        adapter = self.session.adapter
        hud_bgs = getattr(adapter, "hud_bgs", frozenset()) if adapter else frozenset()
        split = self.renderer.render_split(
            st, hide_oam=hidden, front_of=(front_of["priority"], front_of["slot"]) if front_of else None,
            hud_bgs=hud_bgs)
        return encode_frame({**meta, "hidden_oam": sorted(hidden), "front_of": front_of},
                            {"behind": split.behind, "front": split.front, "hud": split.hud})

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
