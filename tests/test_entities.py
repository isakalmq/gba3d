"""Entity framework, Pokemon Emerald adapter (on synthetic memory), WebSocket server."""
import asyncio
import json
import socket
import struct
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np
import pytest

from shadow.entities import extract_world
from shadow.games import find_adapter
from shadow.games import pokeemerald as pe
from shadow.protocol import (FLAG_HELLO, HEADER, WATCH_HEADER, WATCH_RANGE, encode_watch, load_capture,
                             parse_hello)
from shadow.render import Renderer
from shadow.server import FrameServer, decode_frame, encode_frame
from shadow.session import LiveSession
from shadow.state import RAM_BASES, REGION_EWRAM, REGION_IWRAM, GBAState, MemoryNotWatched

FIXTURES = Path(__file__).parent / "fixtures"


def hello_message(code: str, title: str) -> bytes:
    payload = bytes([len(code)]) + code.encode() + bytes([len(title)]) + title.encode()
    return HEADER.pack(b"GBSH", 1, FLAG_HELLO, 0, 0, len(payload)) + payload


def poke(st: GBAState, addr: int, fmt: str, *values) -> None:
    data = np.frombuffer(struct.pack("<" + fmt, *values), np.uint8)
    for rid, base in RAM_BASES.items():
        if base <= addr < base + len(st.region(rid)):
            st.write(rid, addr - base, data)
            return
    raise ValueError(hex(addr))


# -- protocol / state ------------------------------------------------------------------
def test_hello_and_watch_encoding():
    msg = hello_message("BPEE", "POKEMON EMER")
    assert parse_hello(msg[HEADER.size:]) == ("BPEE", "POKEMON EMER")
    w = encode_watch([(0x02037350, 0x264), (0x03005D8C, 0x62)], 1)
    magic, version, flags, count = WATCH_HEADER.unpack_from(w)
    assert (magic, version, flags, count) == (b"GBSW", 1, 1, 2)
    assert WATCH_RANGE.unpack_from(w, WATCH_HEADER.size) == (0x02037350, 0x264)


def test_unstreamed_ram_raises():
    st = GBAState()
    with pytest.raises(MemoryNotWatched):
        st.u16(0x02000000)
    st.write(REGION_EWRAM, 0, np.array([0x34, 0x12], np.uint8))
    assert st.u16(0x02000000) == 0x1234
    with pytest.raises(MemoryNotWatched):
        st.u16(0x02000001)                   # second byte not streamed
    st.write(REGION_IWRAM, 0x10, np.array([0xFE], np.uint8))
    assert st.s8(0x03000010) == -2
    assert extract_world(pe.ADAPTER, GBAState()).error.startswith("waiting for RAM")


def test_hide_oam_removes_sprites():
    st = load_capture(next(FIXTURES.glob("frame_000552.gbsh.gz")))
    full = Renderer().render(st)
    hidden = Renderer().render(st, hide_oam={s.index for s in full.sprites})
    assert full.sprites and not hidden.sprites
    assert not np.array_equal(full.image, hidden.image)


# -- Pokemon Emerald adapter on hand-built memory -------------------------------------
def emerald_state(*, map_tile, prev_tile, cam_tile, field_cam, total_offset, sprite_xy,
                  facing=4, save_offset=0x24) -> GBAState:
    """Lay out RAM the way pokeemerald does, with the player as object event 2 / sprite 5."""
    st = GBAState()
    for addr, n in pe.ADAPTER.ram_ranges:      # everything the adapter watches is "streamed"
        poke(st, addr, f"{n}s", bytes(n))
    poke(st, pe.G_MAIN + 4, "I", pe.CB2_OVERWORLD | 1)
    av = pe.G_PLAYER_AVATAR
    poke(st, av, "BBBBBBBB", 0x21, 0, 2, 1, 5, 2, 0, 1)          # on foot+controllable, moving, sprite 5, oe 2
    oe = pe.G_OBJECT_EVENTS + 2 * pe.OBJECT_EVENT_SIZE
    poke(st, oe, "BBBB", 1, 0, 1, 0)                                 # active, isPlayer
    poke(st, oe + 0x05, "B", 0x59)
    poke(st, oe + 0x0B, "B", 3)
    poke(st, oe + 0x10, "hhhh", map_tile[0] + 7, map_tile[1] + 7, prev_tile[0] + 7, prev_tile[1] + 7)
    poke(st, oe + 0x18, "H", facing | (facing << 4))
    spr = pe.G_SPRITES + 5 * pe.SPRITE_SIZE
    # OamData: 16x32 (shape 2, size 2), tile 0x40, palette 0
    poke(st, spr, "HHH", (2 << 14), (2 << 14), 0x40)
    poke(st, spr + 0x20, "hhhhbb", sprite_xy[0], sprite_xy[1], 0, 0, -8, -16)
    poke(st, spr + 0x3E, "B", 0b11)                                  # inUse, coordOffsetEnabled
    poke(st, pe.G_SPRITE_COORD_OFFSET_X, "hh", 0, 0)
    sav = pe.G_SAVEBLOCK1 + save_offset
    poke(st, pe.G_SAVEBLOCK1_PTR, "I", sav)
    poke(st, sav, "hhbb", cam_tile[0], cam_tile[1], 0, 9)
    poke(st, pe.G_FIELD_CAMERA + 0x10, "ii", *field_cam)
    poke(st, pe.G_TOTAL_CAMERA_PIXEL_OFFSET_Y, "H", total_offset[1] & 0xFFFF)
    poke(st, pe.G_TOTAL_CAMERA_PIXEL_OFFSET_X, "H", total_offset[0] & 0xFFFF)
    # Hardware OAM: slots 3+4 draw the player's tiles (split sprite), slot 7 the same tiles
    # with another palette (a reflection), slot 9 something unrelated.
    oam = st.oam16
    oam[:] = 0
    for i in range(128):
        oam[i * 4] = 0x200                                           # disabled
    oam[3 * 4], oam[3 * 4 + 2] = 0x0000, 0x40
    oam[4 * 4], oam[4 * 4 + 2] = 0x0000, 0x44
    oam[7 * 4], oam[7 * 4 + 2] = 0x0000, 0x40 | (10 << 12)
    oam[9 * 4], oam[9 * 4 + 2] = 0x0000, 0x100
    return st


def sprite_pos_for(map_tile, cam, total):
    """Where SetSpritePosToMapCoords puts a field sprite for a tile (pokeemerald)."""
    x = (map_tile[0] + 7) * 16 + 8 - total[0] - cam[0]
    y = (map_tile[1] + 7) * 16 + 16 - 16 - total[1] - cam[1]   # ctcY = -16
    return x, y


def test_emerald_player_standing():
    total = (-16, 32)                       # accumulated camera scroll since the map loaded
    sx, sy = sprite_pos_for((10, 5), (160, 80), total)
    st = emerald_state(map_tile=(10, 5), prev_tile=(10, 5), cam_tile=(10, 5), field_cam=(0, 0),
                       total_offset=total, sprite_xy=(sx, sy))
    world = extract_world(find_adapter("BPEE"), st, "BPEE")
    assert world.error is None
    (p,) = world.entities
    assert p.tile == (10, 5)
    assert p.pos == (10 * 16 + 8, 5 * 16 + 16)             # middle of the tile's bottom edge
    assert p.facing == "right"
    assert p.size == (16, 32)
    assert p.oam == [3, 4]                                   # split sprite, not the reflection
    assert p.props["map"] == (0, 9) and p.props["elevation"] == 3
    assert "on_foot" in p.props["avatar"] and "controllable" in p.props["avatar"]
    assert world.camera["origin"][0] == p.pos[0] - p.screen[0]


def test_emerald_player_mid_step_moves_smoothly():
    # Four pixels into a step east: the game has already moved the camera tile (CameraMove at
    # the start of the step), gFieldCamera.x = 4, the scroll total went down by 4 and the
    # sprite moved 4 pixels.
    total0 = (-16, 32)
    sx, sy = sprite_pos_for((10, 5), (160, 80), total0)
    st = emerald_state(map_tile=(11, 5), prev_tile=(10, 5), cam_tile=(11, 5), field_cam=(4, 0),
                       total_offset=(total0[0] - 4, total0[1]), sprite_xy=(sx + 4, sy))
    (p,) = extract_world(pe.ADAPTER, st).entities
    assert p.tile == (11, 5) and p.props["prev_tile"] == (10, 5)
    assert p.pos == (10 * 16 + 8 + 4, 5 * 16 + 16)
    assert p.moving


def test_emerald_not_in_overworld():
    st = emerald_state(map_tile=(1, 1), prev_tile=(1, 1), cam_tile=(1, 1), field_cam=(0, 0),
                       total_offset=(0, 0), sprite_xy=(0, 0))
    poke(st, pe.G_MAIN + 4, "I", 0x08000001)                 # e.g. a battle callback
    world = extract_world(pe.ADAPTER, st)
    assert world.entities == [] and world.error is None


# -- WebSocket server -------------------------------------------------------------------
def test_frame_encoding_roundtrip():
    rng = np.random.default_rng(1)
    img = rng.integers(0, 255, (160, 240, 3), np.uint8)
    hud = rng.integers(0, 255, (160, 240, 4), np.uint8)
    meta, layers = decode_frame(encode_frame({"frame": 3, "entities": []}, {"behind": img, "front": None, "hud": hud}))
    assert meta["frame"] == 3 and meta["width"] == 240 and meta["layers"] == ["behind", "front", "hud"]
    np.testing.assert_array_equal(layers["behind"][..., :3], img)
    assert (layers["behind"][..., 3] == 255).all()
    assert layers["front"] is None
    np.testing.assert_array_equal(layers["hud"], hud)


def test_server_end_to_end():
    from websockets.sync.client import connect

    session = LiveSession(port=0).start()
    server = FrameServer(session, http_port=0)
    loop = asyncio.new_event_loop()
    ready = asyncio.Event()
    thread = threading.Thread(target=lambda: loop.run_until_complete(server.run(ready)), daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not ready.is_set() and time.time() < deadline:
        time.sleep(0.02)
    try:
        html = urllib.request.urlopen(f"http://127.0.0.1:{server.http_port}/").read().decode()
        assert "three" in html and "/ws" in html

        # Fake bridge: hello -> expect the test adapter's watch request -> send one full frame.
        bridge = socket.create_connection(("127.0.0.1", session.port))
        bridge.sendall(hello_message("STST", "SHADOWTEST"))
        req = bridge.recv(64)
        assert req[:4] == b"GBSW" and WATCH_RANGE.unpack_from(req, WATCH_HEADER.size) == (0x02000000, 12)

        video = load_capture(FIXTURES / "frame_000552.gbsh.gz")
        entity = struct.pack("<BBBBhhHH", 1, 1, 3, 0, 40, 60, 32, 32)
        records = [(0, 0, bytes(video.io[:0x60])), (1, 0, bytes(video.palette)), (2, 0, bytes(video.vram)),
                   (3, 0, bytes(video.oam)), (4, 0, entity)]
        payload = b"".join(struct.pack("<BII", r, o, len(d)) + d for r, o, d in records)
        frame = HEADER.pack(b"GBSH", 1, 1, len(records), 77, len(payload)) + payload

        with connect(f"ws://127.0.0.1:{server.http_port}/ws") as ws:
            time.sleep(0.2)                       # let the server register the client
            bridge.sendall(frame)
            meta, layers = decode_frame(ws.recv(timeout=10))
        bridge.close()
        assert meta["game"] == "STST" and meta["frame"] == 77
        (p,) = meta["entities"]
        assert p["id"] == "player" and p["screen"] == [56, 92] and p["facing"] == "left"
        assert meta["hidden_oam"] == [0]
        prio = (int(video.oam16[2]) >> 10) & 3
        assert meta["front_of"] == {"entity": "player", "priority": prio, "slot": 0}
        split = Renderer().render_split(video, hide_oam={0}, front_of=(prio, 0))
        np.testing.assert_array_equal(layers["behind"][..., :3], split.behind)
        assert (layers["front"] is None) == (split.front is None)
        assert layers["hud"] is None                       # the test ROM adapter has no UI layer
    finally:
        loop.call_soon_threadsafe(server.stop)
        thread.join(5)
        session.stop()
        assert json.dumps(meta)                  # metadata is plain JSON
