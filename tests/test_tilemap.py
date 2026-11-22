"""3D map: diorama geometry, the Emerald map reader and the server's map messages."""
import asyncio
import struct
import threading
import time

import numpy as np
import pytest

from shadow.games import pokeemerald as pe
from shadow.rom import Rom
from shadow.server import FrameServer, decode_blob, decode_frame
from shadow.state import GBAState
from shadow.tilemap import (CELL, KIND_CARD, KIND_FACE, Metatile, TileMap, build_atlas, build_diorama,
                            project, rasterize)
from test_entities import emerald_state, poke

# A tiny synthetic tileset: tile t is filled with colour index (t % 15) + 1, except tile 0
# (transparent) and "sprinkled" tiles 100+ that are transparent except a diagonal.
N_TILES = 128


def synthetic_vram(st: GBAState) -> None:
    for t in range(1, N_TILES):
        idx = np.full((8, 8), (t % 15) + 1, np.uint8)
        if t >= 100:
            idx = np.where(np.eye(8, dtype=bool), idx, 0)
        packed = (idx[:, 0::2] | (idx[:, 1::2] << 4)).astype(np.uint8)
        st.vram[t * 32:(t + 1) * 32] = packed.ravel()
    for p in range(16):
        st.palette16[p * 16:(p + 1) * 16] = [(i * 2 + p) & 31 | (((i + p * 3) & 31) << 5) | ((p * 2) << 10)
                                             for i in range(16)]


def mt(base: int, over: int | None = None, pal: int = 0) -> Metatile:
    e = tuple((base + i) | (pal << 12) for i in range(4))
    o = None if over is None else tuple((over + i) | (pal << 12) for i in range(4))
    return Metatile(under=e, over=o)


def synthetic_map():
    # 0 floor, 1 wall, 2 table (raised), 3 floor with an over-layer (bed cover), 4 tall wall
    metatiles = {0: mt(4), 1: mt(8, pal=1), 2: mt(12, over=100, pal=2), 3: mt(20, over=104), 4: mt(24, pal=3)}
    cells = np.array([
        [4, 4, 4, 4, 4, 4],
        [4, 1, 1, 1, 1, 4],
        [4, 1, 1, 1, 1, 4],
        [4, 0, 2, 0, 3, 4],
        [4, 0, 2, 0, 3, 4],
        [4, 0, 0, 0, 0, 4],
    ], np.uint16)
    raised = np.isin(cells, [1, 2, 4])
    return TileMap(("k",), cells, raised, (-16, -16), metatiles)


def expected_picture(tm: TileMap, atlas, origin, size=(240, 160)) -> np.ndarray:
    """What the game shows: every cell's art (under + over) at its map position."""
    w, h = size
    out = np.zeros((h, w, 3), np.uint8)
    for (row, col), mid in np.ndenumerate(tm.cells):
        slot = atlas.slots[(int(mid), "full")]
        x, y = (slot % 32) * 16, (slot // 32) * 16
        art = atlas.image[y:y + 16, x:x + 16, :3]
        sx, sy = tm.origin[0] + col * CELL - origin[0], tm.origin[1] + row * CELL - origin[1]
        for r in range(16):
            for c in range(16):
                if 0 <= sx + c < w and 0 <= sy + r < h:
                    out[sy + r, sx + c] = art[r, c]
    return out


@pytest.mark.parametrize("origin", [(-40, -30), (-21, -7), (0, 0)])
def test_diorama_projects_to_the_original_picture(origin):
    st = GBAState()
    synthetic_vram(st)
    tm = synthetic_map()
    atlas = build_atlas(tm, st)
    dio = build_diorama(tm, atlas)
    np.testing.assert_array_equal(rasterize(dio, atlas, origin), expected_picture(tm, atlas, origin))


def test_diorama_structure():
    st = GBAState()
    synthetic_vram(st)
    tm = synthetic_map()
    atlas = build_atlas(tm, st)
    dio = build_diorama(tm, atlas)
    faces = dio.positions[dio.kinds == KIND_FACE]
    # The 2-cell wall (rows 1-2) on top of nothing: 24 px tall; the outer 6-cell wall
    # column is split into runs of at most 4 cells (56 px).
    heights = {round(float(f[:, 1].max())) for f in faces}
    assert {8, 24, 56} <= heights
    cards = dio.positions[dio.kinds == KIND_CARD]
    assert len(cards) == 2                              # the two bed-cover cells
    # Cards stand just south of their cell, so something standing in the cell is behind.
    assert all(abs(c[0, 2] - (tm.origin[1] + 4 * CELL) - 0.25) < 1e-6 or
               abs(c[0, 2] - (tm.origin[1] + 5 * CELL) - 0.25) < 1e-6 for c in cards)
    # Projection: a face's top edge lands exactly where its run starts on screen.
    p = project(faces[0], (0, 0))
    assert p.shape == (4, 2)


# -- Emerald map reader ------------------------------------------------------------------
def emerald_map_state():
    """Emerald RAM + a fake ROM with a 4x3 map, a primary and a secondary tileset."""
    st = emerald_state(map_tile=(1, 1), prev_tile=(1, 1), cam_tile=(1, 1), field_cam=(0, 0),
                       total_offset=(0, 0), sprite_xy=(0, 0))
    synthetic_vram(st)
    rom = bytearray(0x10000)
    rom[0xAC:0xB0] = b"BPEE"

    def at(off):
        return 0x08000000 + off
    # primary metatiles 0..3, secondary 512..513
    prim_tbl = b"".join(struct.pack("<8H", *[4 * i + j for j in range(4)], *([0] * 4)) for i in range(4))
    prim_attr = struct.pack("<4H", 0x0000, 0x0010, 0x1000, 0x2000)   # normal, water, covered, split
    sec_tbl = struct.pack("<8H", 40, 41, 42, 43, 100, 101, 102, 103) * 2
    sec_attr = struct.pack("<2H", 0x0000, 0x0000)
    rom[0x1000:0x1000 + len(prim_tbl)] = prim_tbl
    rom[0x1800:0x1808] = prim_attr
    rom[0x2000:0x2000 + len(sec_tbl)] = sec_tbl
    rom[0x2800:0x2804] = sec_attr
    rom[0x3000:0x3008] = struct.pack("<4H", 3, 3, 3, 3)                # border: metatile 3
    rom[0x0100:0x0118] = struct.pack("<BBxxIIIII", 0, 0, 0, 0, at(0x1000), at(0x1800), 0)
    rom[0x0200:0x0218] = struct.pack("<BBxxIIIII", 0, 1, 0, 0, at(0x2000), at(0x2800), 0)
    rom[0x0300:0x0318] = struct.pack("<iiIIII", 4, 3, at(0x3000), 0, at(0x0100), at(0x0200))
    for addr, n in ((pe.S_BACKUP_MAP_DATA, pe.BACKUP_MAP_DATA_SIZE), (pe.G_MAP_HEADER, 0x1C)):
        poke(st, addr, f"{n}s", bytes(n))
    bw, bh = 4 + 15, 3 + 14
    grid = np.full((bh, bw), 0x3FF, np.uint16)
    grid[7:10, 7:11] = [[0, 1, 0x400 | 512, 0],
                        [0x400 | 1, 2, 0x400 | 3, 513],
                        [0, 0, 0, 0]]
    poke(st, pe.G_BACKUP_MAP_LAYOUT, "iiI", bw, bh, pe.S_BACKUP_MAP_DATA)
    poke(st, pe.S_BACKUP_MAP_DATA, f"{grid.size}H", *grid.ravel().tolist())
    poke(st, pe.G_MAP_HEADER, "I", at(0x0300))
    return st, Rom(bytes(rom)), grid


def test_emerald_tilemap_reader():
    st, rom, grid = emerald_map_state()
    tm = pe.ADAPTER.tilemap(st, rom)
    assert tm is not None and tm.shape == grid.shape and tm.origin == (-112, -112)
    inner = tm.cells[7:10, 7:11]
    assert inner.tolist() == [[0, 1, 512, 0], [1, 2, 3, 513], [0, 0, 0, 0]]
    assert int(tm.cells[0, 0]) == 3                            # undefined -> border metatile
    raised = tm.raised[7:10, 7:11]
    # impassable secondary 512 is raised; impassable water (metatile 1) stays flat;
    # the impassable border around the map stays flat too.
    assert raised.tolist() == [[False, False, True, False], [False, False, True, False], [False] * 4]
    assert not tm.raised[0, 0]
    assert tm.metatiles[2].over is None and tm.metatiles[2].under_extra is not None   # covered
    assert tm.metatiles[3].over is not None                                              # split
    assert tm.metatiles[512].under == (40, 41, 42, 43)
    # Without the ROM, or outside the overworld, there is no map.
    assert pe.ADAPTER.tilemap(st, None) is None
    key = tm.key
    poke(st, pe.S_BACKUP_MAP_DATA + 2 * (8 * grid.shape[1] + 9), "H", 0)   # a door opens
    assert pe.ADAPTER.tilemap(st, rom).key != key


def test_server_sends_map_in_3d_view():
    from websockets.sync.client import connect

    st, rom, _ = emerald_map_state()

    class Session:
        adapter, port = pe.ADAPTER, 0

        def __init__(self):
            self.rom, self.seq = rom, 0

        def latest(self):
            self.seq += 1
            return st, self.seq

        def world(self, s):
            from shadow.entities import extract_world
            return extract_world(self.adapter, s, "BPEE")

    server = FrameServer(Session(), http_port=0)
    loop = asyncio.new_event_loop()
    ready = asyncio.Event()
    thread = threading.Thread(target=lambda: loop.run_until_complete(server.run(ready)), daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not ready.is_set() and time.time() < deadline:
        time.sleep(0.02)
    try:
        with connect(f"ws://127.0.0.1:{server.http_port}/ws") as ws:
            ws.send('{"view": "3d"}')
            got_map = got_frame = None
            for _ in range(200):
                msg = ws.recv(timeout=10)
                meta, _ = decode_blob(msg)
                if meta["type"] == "map" and meta.get("key"):
                    got_map = decode_blob(msg)
                elif meta["type"] == "frame" and meta.get("view") == "3d":
                    got_frame = decode_frame(msg)
                if got_map and got_frame:
                    break
    finally:
        loop.call_soon_threadsafe(server.stop)
        thread.join(5)
    meta, sec = got_map
    assert meta["shape"] == list(pe.ADAPTER.tilemap(st, rom).shape)
    assert sec["positions"].shape == (meta["quads"], 4, 3)
    assert sec["uvs"].shape == (meta["quads"], 4, 2)
    assert sec["atlas"].shape[2] == 4
    fmeta, layers = got_frame
    assert fmeta["layers"] == ["sprites", "hud"] and fmeta["camera"]["origin"]


# -- prop kit ---------------------------------------------------------------------------------
def test_props_replace_furniture_cells():
    from shadow.props import PropDef, place_props
    st = GBAState()
    synthetic_vram(st)
    tm = synthetic_map()
    # The table (metatile 2) is drawn on a second layer above a plain floor, like Emerald's
    # "covered" metatiles; the kit replaces the 1x2 column of it with a model.
    tm.metatiles[2] = Metatile(under=tuple(range(4, 8)), over=None, under_extra=tuple(range(100, 104)))
    tm.tilesets = ("ts",)
    kit = [PropDef("table", "ts", ((2,), (2,)), (8, 26)),
           PropDef("other", "elsewhere", ((0,),), (8, 8))]          # another tileset: ignored
    props = place_props(tm, kit)
    assert [(p.model, p.position, p.cells) for p in props] == [("table", (-16 + 32 + 8, -16 + 48 + 26), [(3, 2), (4, 2)])]
    assert props[0].to_dict()["cells"] == [[3, 2], [4, 2]]

    atlas = build_atlas(tm, st)
    assert atlas.slots[(2, "base")] != atlas.slots[(2, "under")]      # floor without the furniture
    assert atlas.slots[(0, "base")] == atlas.slots[(0, "under")]
    with_props = build_diorama(tm, atlas, props)
    without = build_diorama(tm, atlas)
    # No block for the table any more: its cells are floor with the bottom layer's art.
    def table_faces(d):   # front faces in column 2 standing on row 4's south edge
        return [f for f in d.positions[d.kinds == KIND_FACE] if f[0, 0] == 16 and f[0, 2] == 64]
    assert table_faces(without) and not table_faces(with_props)
    pic = rasterize(with_props, atlas, (-16, -16))
    slot = atlas.slots[(2, "base")]
    x, y = (slot % 32) * 16, (slot // 32) * 16
    np.testing.assert_array_equal(pic[48:64, 32:48], atlas.image[y:y + 16, x:x + 16, :3])


def test_emerald_prop_kit_matches_brendans_house_table():
    from shadow.props import place_props
    tm = TileMap(("k",), np.array([[0x201, 0x219, 0x21A, 0x21B, 0x21C],
                                   [0x201, 0x221, 0x222, 0x223, 0x224]], np.uint16),
                 np.zeros((2, 5), bool), (0, 0), {}, tilesets=(0x083DF884, pe.TILESET_BRENDANS_MAYS_HOUSE))
    props = {(p.model, p.position, p.yaw) for p in place_props(tm, pe.PROP_KIT)}
    assert props == {("prop_table", (48, 20), 0), ("prop_chair", (24, 13), 0), ("prop_chair", (24, 29), 0),
                     ("prop_chair", (72, 13), 180), ("prop_chair", (72, 29), 180)}
