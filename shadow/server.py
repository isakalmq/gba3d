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

3D maps: clients send {"view": "3d"} to get the map as 3D geometry instead of a
picture (when the adapter can read the map, e.g. Pokemon Emerald with its ROM in
roms/). Frames then carry only the "sprites" (sprites other than the entities')
and "hud" layers, and separate messages describe the map:

    u32 json_length | json | for each entry in json["sections"]: u32 n | n bytes

    {"type": "map", ...}    atlas (RGBA), positions (n,4,3 f32), uvs (n,4,2 f32),
                            normals (n,3 f32), kinds (n u8); sent when the map changes.
                            json["props"]: models standing in for furniture (shadow/props.py),
                            {"model", "x", "z" (world px of the model origin), "yaw" (deg), "cells"}
    {"type": "atlas", ...}  a re-rendered atlas (tile animations, palette fades)
    {"type": "map", "key": null}   no map any more

Every message's JSON has a "type": "frame", "map" or "atlas". Frames say which
"view" they were made for ("2d" or "3d").

GET / serves a small three.js demo client (shadow/web/index.html); GET /models/<name>.glb
serves its models from shadow/web/models/.
"""
from __future__ import annotations

import asyncio
import json
import struct
import time
from pathlib import Path

import numpy as np
from websockets.asyncio.server import ServerConnection, broadcast, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from .protocol import DEFAULT_PORT
from .render import Renderer
from .session import LiveSession
from .props import place_props
from .tilemap import build_atlas, build_diorama

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
    first = next((img for img in layers.values() if img is not None), None)
    h, w = first.shape[:2] if first is not None else (160, 240)
    meta = json.dumps({"type": "frame", **world_dict, "width": w, "height": h, "layers": list(layers)}).encode()
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


def encode_blob(meta: dict, sections: dict[str, np.ndarray]) -> bytes:
    """A JSON header plus named binary arrays (dtype and shape are in the JSON)."""
    arrays = {name: np.ascontiguousarray(a) for name, a in sections.items()}
    meta = {**meta, "sections": [{"name": n, "dtype": str(a.dtype), "shape": list(a.shape)} for n, a in arrays.items()]}
    head = json.dumps(meta).encode()
    parts = [struct.pack("<I", len(head)), head]
    for a in arrays.values():
        data = a.tobytes()
        parts += [struct.pack("<I", len(data)), data]
    return b"".join(parts)


def decode_blob(data: bytes) -> tuple[dict, dict[str, np.ndarray]]:
    (n,) = struct.unpack_from("<I", data)
    meta = json.loads(data[4:4 + n])
    pos, out = 4 + n, {}
    for sec in meta.get("sections", []):
        (size,) = struct.unpack_from("<I", data, pos)
        pos += 4
        out[sec["name"]] = np.frombuffer(data, sec["dtype"], size // np.dtype(sec["dtype"]).itemsize, pos).reshape(sec["shape"])
        pos += size
    return meta, out


ATLAS_REFRESH_SECONDS = 1 / 15


class FrameServer:
    def __init__(self, session: LiveSession, http_host: str = "127.0.0.1", http_port: int = DEFAULT_HTTP_PORT):
        self.session = session
        self.http_host, self.http_port = http_host, http_port
        self.renderer = Renderer(scale=1)
        self.clients: set[ServerConnection] = set()
        self.hide_entity_sprites = True
        self.view = "2d"
        self.frames_sent = 0
        self._stop = asyncio.Event()
        self._map_key = None          # key of the map last sent
        self._map_msg = None          # encoded map message, for clients that join later
        self._map_parts = None        # (meta, diorama) to re-encode with a fresh atlas
        self._atlas = None
        self._atlas_checked = 0.0

    # -- HTTP + WebSocket -----------------------------------------------------
    def _process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        if request.path == "/ws":
            return None                                        # continue with the WebSocket handshake
        if request.path in ("/", "/index.html"):
            body = (WEB_DIR / "index.html").read_bytes()
            headers = Headers([("Content-Type", "text/html; charset=utf-8"),
                               ("Content-Length", str(len(body))), ("Cache-Control", "no-cache")])
            return Response(200, "OK", headers, body)
        if request.path.startswith("/models/"):
            name = request.path.removeprefix("/models/")
            path = WEB_DIR / "models" / name
            if "/" not in name and name.endswith(".glb") and path.is_file():
                body = path.read_bytes()
                headers = Headers([("Content-Type", "model/gltf-binary"),
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
                if cmd.get("view") in ("2d", "3d"):
                    self.view = cmd["view"]
                    if self.view == "3d" and self._map_msg is not None:
                        await ws.send(self._map_msg)
        finally:
            self.clients.discard(ws)

    # -- 3D map -----------------------------------------------------------------
    def _map_messages(self, st) -> tuple[list[bytes], bool]:
        """Messages that bring clients up to date with the current map, and whether one exists."""
        adapter = self.session.adapter
        tm = adapter.tilemap(st, self.session.rom) if adapter and hasattr(adapter, "tilemap") else None
        if tm is None:
            if self._map_key is None:
                return [], False
            self._map_key = self._map_msg = self._map_parts = self._atlas = None
            return [encode_blob({"type": "map", "key": None}, {})], False
        now = time.monotonic()
        if tm.key != self._map_key:
            atlas = build_atlas(tm, st)
            props = place_props(tm, getattr(adapter, "prop_kit", ()))
            dio = build_diorama(tm, atlas, props)
            meta = {"type": "map", "key": str(tm.key), "name": tm.name, "origin": list(tm.origin),
                    "shape": list(tm.shape), "quads": int(len(dio.kinds)),
                    "props": [p.to_dict() for p in props]}
            self._map_key, self._atlas, self._atlas_checked = tm.key, atlas, now
            self._map_parts = (meta, dio)
            self._map_msg = self._encode_map(atlas)
            return [self._map_msg], True
        if now - self._atlas_checked >= ATLAS_REFRESH_SECONDS:
            self._atlas_checked = now
            atlas = build_atlas(tm, st)
            if atlas.digest != self._atlas.digest and atlas.slots == self._atlas.slots:
                self._atlas = atlas
                self._map_msg = self._encode_map(atlas)
                return [encode_blob({"type": "atlas", "key": str(tm.key)}, {"atlas": atlas.image})], True
        return [], True

    def _encode_map(self, atlas) -> bytes:
        meta, dio = self._map_parts
        return encode_blob(meta, {"atlas": atlas.image, "positions": dio.positions, "uvs": dio.uvs,
                                  "normals": dio.normals, "kinds": dio.kinds})

    # -- frame loop -----------------------------------------------------------
    def _make_frame(self, st) -> list[bytes]:
        """Messages for one frame: map updates (3D view) followed by the frame itself."""
        world = self.session.world(st)
        meta = world.to_dict()
        out, has_map = [], False
        if self.view == "3d":
            out, has_map = self._map_messages(st)
        if not self.hide_entity_sprites:
            image = self.renderer.render(st).image
            return out + [encode_frame({**meta, "view": "2d", "hidden_oam": [], "front_of": None}, {"behind": image})]
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
            hud_bgs=hud_bgs, bounds_for={e.id: e.oam for e in world.entities if e.oam},
            picture=not has_map, sprites_only=has_map)
        # "drawn": screen box of the entity's original sprite pixels (x0, y0, x1, y1), so a
        # client can fit its model over exactly the area the game draws the character in.
        for e in meta["entities"]:
            e["drawn"] = split.bounds.get(e["id"])
        meta = {**meta, "hidden_oam": sorted(hidden), "front_of": front_of}
        if has_map:
            return out + [encode_frame({**meta, "view": "3d", "map_key": str(self._map_key)},
                                       {"sprites": split.sprites, "hud": split.hud})]
        return out + [encode_frame({**meta, "view": "2d"},
                                   {"behind": split.behind, "front": split.front, "hud": split.hud})]

    async def _pump(self) -> None:
        last_seq = -1
        while not self._stop.is_set():
            st, seq = self.session.latest()
            if st is None or seq == last_seq or not self.clients:
                await asyncio.sleep(1 / 240)
                continue
            last_seq = seq
            messages = await asyncio.to_thread(self._make_frame, st)
            for data in messages:
                broadcast(self.clients, data)
            self.frames_sent += 1

    async def run(self, ready: asyncio.Event | None = None) -> None:
        async with serve(self._handler, self.http_host, self.http_port,
                         process_request=self._process_request, max_size=2 ** 20,
                         write_limit=2 ** 22) as server:
            self.http_port = server.sockets[0].getsockname()[1]
            print(f"[shadow] open http://{self.http_host}:{self.http_port}  (frames on ws://.../ws)")
            if ready:
                ready.set()
            await self._pump()

    def stop(self) -> None:
        self._stop.set()


def run_server(host: str = "127.0.0.1", port: int = DEFAULT_PORT,
               http_host: str = "127.0.0.1", http_port: int = DEFAULT_HTTP_PORT, rom: str | None = None) -> int:
    session = LiveSession(host, port, rom=rom).start()
    print(f"[shadow] waiting for the mGBA bridge on {host}:{port} - load lua/shadow_bridge.lua in mGBA")
    server = FrameServer(session, http_host, http_port)
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        pass
    finally:
        session.stop()
    return 0
