"""Build a fake-but-realistic Pokemon Emerald game state for any map, from the pret/pokeemerald
decompilation's assets: no ROM or emulator needed. Used for screenshots and for checking the
3D map against the original picture.

The decomp is fetched on demand (sparse, only what a map needs) into build/decomp/, which is
git-ignored: its graphics are the game's, so they must never be committed.

    python tools/emerald_fixture.py LittlerootTown_BrendansHouse_1F --player 5,4 -o out.png

writes the original 2D picture and prints how the 3D diorama compares to it.
"""
from __future__ import annotations

import argparse
import json
import re
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from shadow.games import pokeemerald as pe          # noqa: E402
from shadow.rom import Rom                          # noqa: E402
from shadow.state import GBAState                   # noqa: E402
from test_entities import emerald_state, poke       # noqa: E402

DECOMP = REPO / "build" / "decomp" / "pokeemerald"
DECOMP_SYMBOLS = REPO / "build" / "decomp" / "pokeemerald-symbols"
DECOMP_URL = "https://github.com/pret/pokeemerald"


# -- decomp access -------------------------------------------------------------------
def ensure_decomp(paths: list[str]) -> Path:
    """Sparse-clone pret/pokeemerald (once) and make sure `paths` are checked out."""
    if not (DECOMP / ".git").exists():
        DECOMP.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse",
                        DECOMP_URL, str(DECOMP)], check=True)
        subprocess.run(["git", "-C", str(DECOMP), "sparse-checkout", "set", "--no-cone", "/README.md"], check=True)
    missing = [p for p in paths if not (DECOMP / p).exists()]
    if missing:
        subprocess.run(["git", "-C", str(DECOMP), "sparse-checkout", "add", *[f"/{p}" for p in missing]],
                       check=True, capture_output=True)
    return DECOMP


def symbol_address(name: str) -> int | None:
    """Address of a symbol in the real ROM/RAM (pokeemerald.sym from the decomp's
    `symbols` branch, fetched once). Used so fixture structures sit where the real
    game has them, e.g. tilesets, which the prop kit identifies by address."""
    sym = DECOMP_SYMBOLS / "pokeemerald.sym"
    if not sym.exists():
        DECOMP_SYMBOLS.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--depth", "1", "-b", "symbols", DECOMP_URL, str(DECOMP_SYMBOLS)],
                       check=True)
    for line in sym.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[3] == name:
            return int(parts[0], 16)
    return None


def layout_info(name: str) -> dict:
    """The layouts.json entry for a layout, by directory name (e.g. LittlerootTown_BrendansHouse_1F)."""
    ensure_decomp(["data/layouts/layouts.json"])
    for entry in json.loads((DECOMP / "data/layouts/layouts.json").read_text())["layouts"]:
        if entry and Path(entry.get("blockdata_filepath", "")).parent.name == name:
            return entry
    raise SystemExit(f"no layout named {name!r} in data/layouts/layouts.json")


def tileset_dir(symbol: str) -> Path:
    """gTileset_BrendansMaysHouse -> data/tilesets/secondary/brendans_mays_house."""
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", symbol.removeprefix("gTileset_")).lower()
    for kind in ("primary", "secondary"):
        rel = f"data/tilesets/{kind}/{snake}"
        ensure_decomp([rel])
        if (DECOMP / rel / "tiles.png").exists():
            return DECOMP / rel
    raise SystemExit(f"can't find tileset {symbol} ({snake}) in the decomp")


# -- asset conversion ------------------------------------------------------------------
def read_pal(path: Path) -> list[int]:
    vals = list(map(int, path.read_text().split()[3:3 + 48]))
    return [(r >> 3) | ((g >> 3) << 5) | ((b >> 3) << 10) for r, g, b in zip(vals[0::3], vals[1::3], vals[2::3])]


def tiles_4bpp(png: Path) -> np.ndarray:
    im = np.asarray(Image.open(png))                 # palette indices, 16 tiles per row
    rows, cols = im.shape[0] // 8, im.shape[1] // 8
    return im.reshape(rows, 8, cols, 8).transpose(0, 2, 1, 3).reshape(-1, 8, 8) & 15


def pack_tiles(tiles: np.ndarray) -> bytes:
    return (tiles[:, :, 0::2] | (tiles[:, :, 1::2] << 4)).astype(np.uint8).tobytes()


def _poke_io(st: GBAState, off: int, value: int) -> None:
    st.io[off], st.io[off + 1] = value & 0xFF, value >> 8


# -- the fixture -------------------------------------------------------------------------
def build(layout: str, player_tile=(5, 4), facing: int = 1) -> tuple[GBAState, Rom, dict]:
    """(state, rom, info) for `layout` with the player standing at `player_tile` (map tiles).

    The state looks like the game mid-overworld: RAM for the adapter (player, camera, map
    grid), a small fake ROM with the layout and tileset tables, tiles and palettes in VRAM,
    and BG tilemaps filled the way the game's DrawMetatile does, so the 2D renderer shows
    the original picture. The player is a placeholder 16x32 magenta sprite in OAM slot 3."""
    info = layout_info(layout)
    w, h = info["width"], info["height"]
    primary, secondary = tileset_dir(info["primary_tileset"]), tileset_dir(info["secondary_tileset"])
    ensure_decomp([str(Path(info["blockdata_filepath"]).parent)])
    blockdata = (DECOMP / info["blockdata_filepath"]).read_bytes()
    border = (DECOMP / info["border_filepath"]).read_bytes()

    # Fake ROM: layout and metatile tables at fixed offsets; the Tileset structs at their
    # real addresses when the symbol map has them (the prop kit matches tilesets by address).
    rom = bytearray(0x800000)
    rom[0xAC:0xB0] = b"BPEE"

    def put(off, data):
        rom[off:off + len(data)] = data
        return 0x08000000 + off
    p_meta = put(0x110000, (primary / "metatiles.bin").read_bytes())
    p_attr = put(0x118000, (primary / "metatile_attributes.bin").read_bytes())
    s_meta = put(0x120000, (secondary / "metatiles.bin").read_bytes())
    s_attr = put(0x128000, (secondary / "metatile_attributes.bin").read_bytes())
    b_ptr, m_ptr = put(0x130000, border), put(0x131000, blockdata)
    p_addr = symbol_address(info["primary_tileset"]) or 0x08100100
    s_addr = symbol_address(info["secondary_tileset"]) or 0x08100200
    prim = put(p_addr - 0x08000000, struct.pack("<BBxxIIIII", 0, 0, 0, 0, p_meta, p_attr, 0))
    sec = put(s_addr - 0x08000000, struct.pack("<BBxxIIIII", 0, 1, 0, 0, s_meta, s_attr, 0))
    layout_ptr = put(0x100000, struct.pack("<iiIIII", w, h, b_ptr, m_ptr, prim, sec))
    rom = Rom(bytes(rom))

    # Player + camera centred like the game: feet at screen (120, 88).
    px, py = player_tile
    pos = (px * 16 + 8, py * 16 + 16)
    total = (0, 40)
    sx = pos[0] + 112 - total[0] - px * 16
    sy = pos[1] + 112 - 16 - total[1] - py * 16
    st = emerald_state(map_tile=player_tile, prev_tile=player_tile, cam_tile=player_tile, field_cam=(0, 0),
                       total_offset=total, sprite_xy=(sx, sy), facing=facing)
    poke(st, pe.G_SPRITE_COORD_OFFSET_X, "hh", 120 - sx, 88 - (sy + 16))
    for addr, n in pe.ADAPTER.ram_ranges:
        if addr in (pe.S_BACKUP_MAP_DATA, pe.G_MAP_HEADER):
            poke(st, addr, f"{n}s", bytes(n))

    # Live map grid: the layout inside a 7-cell margin of MAPGRID_UNDEFINED.
    bw, bh = w + 15, h + 14
    grid = np.full((bh, bw), 0x3FF, np.uint16)
    grid[7:7 + h, 7:7 + w] = np.frombuffer(blockdata, "<u2").reshape(h, w)
    poke(st, pe.G_BACKUP_MAP_LAYOUT, "iiI", bw, bh, pe.S_BACKUP_MAP_DATA)
    poke(st, pe.S_BACKUP_MAP_DATA, f"{grid.size}H", *grid.ravel().tolist())
    poke(st, pe.G_MAP_HEADER, "I", layout_ptr)

    # VRAM tiles (primary 0-511, secondary 512+) and palettes (0-5 primary, 6-12 secondary).
    st.vram[:] = 0
    pt, sct = tiles_4bpp(primary / "tiles.png"), tiles_4bpp(secondary / "tiles.png")
    st.vram[0:len(pt) * 32] = np.frombuffer(pack_tiles(pt), np.uint8)
    st.vram[512 * 32:512 * 32 + len(sct) * 32] = np.frombuffer(pack_tiles(sct), np.uint8)
    for i in range(6):
        st.palette16[i * 16:i * 16 + 16] = read_pal(primary / f"palettes/{i:02d}.pal")
    for i in range(6, 13):
        st.palette16[i * 16:i * 16 + 16] = read_pal(secondary / f"palettes/{i:02d}.pal")

    # BG tilemaps like DrawMetatile: BG1 sb29 (top), BG2 sb28 (middle), BG3 sb30 (bottom).
    origin = (pos[0] - 120, pos[1] - 88)
    tm = pe.ADAPTER.tilemap(st, rom)
    for sb in (28, 29, 30):
        st.vram16[sb * 0x400:(sb + 1) * 0x400] = 0
    for ty in range(32):
        for tx in range(32):
            wx, wy = origin[0] + tx * 8, origin[1] + ty * 8
            cx, cy = (wx + 112) // 16, (wy + 112) // 16
            if not (0 <= cy < tm.cells.shape[0] and 0 <= cx < tm.cells.shape[1]):
                continue
            mid = int(tm.cells[cy, cx])
            tbl, attrs = (p_meta, p_attr) if mid < 512 else (s_meta, s_attr)
            i = mid % 512
            e = rom.u16_array(tbl + i * 16, 8)
            layer = rom.u16(attrs + i * 2) >> 12
            q = ((wy + 112) % 16) // 8 * 2 + ((wx + 112) % 16) // 8
            sb_bottom, sb_top = {1: (30, 28), 2: (30, 29)}.get(layer, (28, 29))
            st.vram16[sb_bottom * 0x400 + ty * 32 + tx] = e[q]
            st.vram16[sb_top * 0x400 + ty * 32 + tx] = e[4 + q]
    _poke_io(st, 0x00, 0x1000 | 0x0E00 | 0x40)          # mode 0, BG1-3 + OBJ, 1D OBJ mapping
    _poke_io(st, 0x0A, 1 | (29 << 8))
    _poke_io(st, 0x0C, 2 | (28 << 8))
    _poke_io(st, 0x0E, 3 | (30 << 8))

    # Placeholder player sprite: OAM slot 3, 16x32, feet at (120, 88), magenta.
    oam = st.oam16
    oam[:] = 0
    oam[0::4] = 0x200
    oam[3 * 4:3 * 4 + 3] = [56 | (2 << 14), 112 | (2 << 14), 0x40 | (2 << 10)]
    obj = st.vram[0x10000:]
    for t in range(8):
        for r in range(2 if t < 2 else 0, 8):
            obj[(0x40 + t) * 32 + r * 4:(0x40 + t) * 32 + r * 4 + 4] = 0x11
    st.palette16[256 + 1] = 0x7C1F
    return st, rom, info


def compare_3d(st: GBAState, rom: Rom) -> int:
    """Pixels where the 3D diorama (seen from the game's view) differs from the 2D picture."""
    from shadow.render import Renderer
    from shadow.tilemap import build_atlas, build_diorama, rasterize
    tm = pe.ADAPTER.tilemap(st, rom)
    atlas = build_atlas(tm, st)
    origin = pe.ADAPTER.extract(st)[1]["origin"]
    ours = rasterize(build_diorama(tm, atlas), atlas, origin)
    ref = Renderer().render(st, hide_oam=set(range(128))).image
    return int(np.any(ours != ref, -1).sum())


def parse_xy(text: str) -> tuple[int, int]:
    x, y = text.split(",")
    return int(x), int(y)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("layout", help="layout directory name in data/layouts/, e.g. LittlerootTown_BrendansHouse_1F")
    ap.add_argument("--player", type=parse_xy, default=(5, 4), help="player tile x,y (default 5,4)")
    ap.add_argument("-o", "--output", default="build/fixture_2d.png", help="2D picture to write (3x scale)")
    args = ap.parse_args()
    from shadow.render import Renderer
    st, rom, info = build(args.layout, args.player)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(Renderer().render(st).image).resize((720, 480), Image.NEAREST).save(args.output)
    tm = pe.ADAPTER.tilemap(st, rom)
    print(f"{args.layout}: {info['width']}x{info['height']} tiles, {len(tm.metatiles)} metatiles, "
          f"{int(tm.raised.sum())} raised cells -> {args.output}")
    print(f"3D vs 2D from the game's view: {compare_3d(st, rom)} differing pixels")
