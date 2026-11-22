"""Turn a game's tile map into a 3D "diorama" that projects exactly onto the original picture.

A game adapter describes the current map as a grid of 16x16 *metatiles* (Pokemon's
term): for each cell, which metatile it shows and whether it is a raised obstacle
(wall, furniture). Each metatile is a 2x2 block of 8x8 BG tiles in two layers:
`under` is drawn below characters, `over` (if any) above them.

The 3D scene uses world units of GBA pixels: x east, z south (both map pixels),
y up. Viewed with the game's own oblique projection (screen x = x, screen y = z - y;
see the web client), it reproduces the original picture exactly:

* walkable cells: a floor quad at y = 0 with the cell's `under` art
* raised cells: runs of up to 4 raised cells, going north from the first one, become one
  block standing on the south half of the southern cell. Its front face carries the art
  of the whole run (stacked), its top carries the top half of the northmost cell.
  A run of k cells is 16k-8 px tall and 8 px deep, so it covers exactly the k cells it
  replaces on screen.
* `over` layers of walkable cells: an upright card on the cell's south edge, so a
  character standing in the cell is behind it, like the game's top BG layer.
* for views that orbit away from the game's own: block sides (edge-on, so invisible in
  the original view) and a plain floor under blocks.

Textures come from live VRAM and palette RAM, so tile animations and palette fades
show up in the atlas.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np

from .render import _decode_4bpp, bgr555_to_rgb8
from .state import GBAState

CELL = 16
SLOT = 16                 # atlas slot size (one metatile image)
ATLAS_COLUMNS = 32
MAX_RUN = 4
CARD_OFFSET = 0.25        # cards sit just south of the cell edge, in front of what stands on it


@dataclass
class Metatile:
    under: tuple[int, ...]        # 4 BG tilemap entries (TL, TR, BL, BR) drawn below characters
    over: tuple[int, ...] | None  # 4 entries drawn above characters, or None
    under_extra: tuple[int, ...] | None = None   # a second layer still below characters


@dataclass
class TileMap:
    key: tuple                    # changes when the map, its tilesets or its cells change
    cells: np.ndarray             # (rows, cols) metatile ids
    raised: np.ndarray            # (rows, cols) bool: obstacles to build as blocks
    origin: tuple[int, int]       # world pixel of cell (0, 0)'s top-left corner
    metatiles: dict[int, Metatile]
    name: str = ""
    tilesets: tuple = ()          # ids of the tilesets the metatiles come from (for prop kits)

    @property
    def shape(self) -> tuple[int, int]:
        return self.cells.shape


# -- atlas ---------------------------------------------------------------------------------
def _compose(entries, tiles: np.ndarray, palette: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """16x16 (colour indices into the 256-entry BG palette, opaque mask) from 4 tilemap entries."""
    idx = np.zeros((16, 16), np.int64)
    opaque = np.zeros((16, 16), bool)
    for n, e in enumerate(entries):
        t = tiles[e & 0x3FF] if (e & 0x3FF) < len(tiles) else np.zeros((8, 8), np.uint8)
        if e & 0x400:
            t = t[:, ::-1]
        if e & 0x800:
            t = t[::-1]
        y, x = (n // 2) * 8, (n % 2) * 8
        idx[y:y + 8, x:x + 8] = (e >> 12) * 16 + t
        opaque[y:y + 8, x:x + 8] = t != 0
    return idx, opaque


@dataclass
class Atlas:
    image: np.ndarray             # (H, W, 4) RGBA
    slots: dict                   # (metatile id, "under" | "full" | "over" | "base") -> slot index
    digest: str

    def uv(self, slot: int, row0: float = 0, row1: float = SLOT) -> tuple[float, float, float, float]:
        """(u0, v0, u1, v1) for rows [row0, row1) of a slot; v measured from the top."""
        h, w = self.image.shape[:2]
        x, y = (slot % ATLAS_COLUMNS) * SLOT, (slot // ATLAS_COLUMNS) * SLOT
        return x / w, (y + row0) / h, (x + SLOT) / w, (y + row1) / h


def build_atlas(tm: TileMap, st: GBAState) -> Atlas:
    """Render every metatile the map uses from current VRAM (BG char block 0) + palettes."""
    tiles = _decode_4bpp(st.vram[:0x8000])               # 1024 tiles: primary 0-511, secondary 512+
    pal = bgr555_to_rgb8(st.palette16[:256]).astype(np.uint8)
    backdrop = pal[0]
    images = [np.dstack([np.broadcast_to(backdrop, (16, 16, 3)), np.full((16, 16), 255, np.uint8)])]
    slots = {"fill": 0}                                  # plain backdrop colour
    for mid in sorted(set(int(v) for v in np.unique(tm.cells)) & set(tm.metatiles)):
        mt = tm.metatiles[mid]
        idx, op = _compose(mt.under, tiles, pal)
        under = np.where(op[..., None], pal[idx], backdrop)
        if mt.under_extra is not None:
            i2, o2 = _compose(mt.under_extra, tiles, pal)
            under = np.where(o2[..., None], pal[i2], under)
        rgba = np.dstack([under, np.full((16, 16), 255, np.uint8)])
        slots[(mid, "under")] = len(images)
        images.append(rgba)
        if mt.under_extra is not None:                   # bottom layer alone: floor under a prop
            base = np.where(op[..., None], pal[idx], backdrop)
            slots[(mid, "base")] = len(images)
            images.append(np.dstack([base, np.full((16, 16), 255, np.uint8)]))
        slots[(mid, "base")] = slots.get((mid, "base"), slots[(mid, "under")])
        if mt.over is not None:
            i3, o3 = _compose(mt.over, tiles, pal)
            if o3.any():
                full = np.where(o3[..., None], pal[i3], under)
                slots[(mid, "full")] = len(images)
                images.append(np.dstack([full, np.full((16, 16), 255, np.uint8)]))
                slots[(mid, "over")] = len(images)
                images.append(np.dstack([pal[i3], np.where(o3, 255, 0).astype(np.uint8)]))
        slots.setdefault((mid, "full"), slots[(mid, "under")])
    rows = max(1, -(-len(images) // ATLAS_COLUMNS))
    atlas = np.zeros((rows * SLOT, ATLAS_COLUMNS * SLOT, 4), np.uint8)
    for i, img in enumerate(images):
        y, x = (i // ATLAS_COLUMNS) * SLOT, (i % ATLAS_COLUMNS) * SLOT
        atlas[y:y + SLOT, x:x + SLOT] = img
    return Atlas(atlas, slots, hashlib.blake2b(atlas.tobytes(), digest_size=8).hexdigest())


# -- geometry --------------------------------------------------------------------------------
class _Quads:
    def __init__(self):
        self.pos, self.uv, self.normal, self.kind = [], [], [], []

    def add(self, corners, uv, normal, kind: int) -> None:
        """corners: top-left, top-right, bottom-right, bottom-left as seen on the texture."""
        u0, v0, u1, v1 = uv
        self.pos.append(corners)
        self.uv.append([(u0, v0), (u1, v0), (u1, v1), (u0, v1)])
        self.normal.append(normal)
        self.kind.append(kind)


KIND_FLOOR, KIND_FACE, KIND_TOP, KIND_CARD, KIND_SIDE = 0, 1, 2, 3, 4


@dataclass
class Diorama:
    positions: np.ndarray        # (n, 4, 3) float32
    uvs: np.ndarray              # (n, 4, 2) float32
    normals: np.ndarray          # (n, 3) float32
    kinds: np.ndarray            # (n,) uint8: floor, face, top, card, side


def build_diorama(tm: TileMap, atlas: Atlas, props=()) -> Diorama:
    """`props`: placed props (shadow/props.py). Their cells become plain floor (the
    metatile's bottom layer) for the client to stand the models on."""
    rows, cols = tm.shape
    ox, oz = tm.origin
    q = _Quads()
    consumed = np.zeros((rows, cols), bool)
    prop_cells = np.zeros((rows, cols), bool)
    for p in props:
        for r, c in p.cells:
            prop_cells[r, c] = True
    raised = tm.raised & ~prop_cells

    for x in range(cols):
        y = rows - 1
        while y >= 0:
            if not raised[y, x] or consumed[y, x]:
                y -= 1
                continue
            k = 1
            while k < MAX_RUN and y - k >= 0 and raised[y - k, x]:
                k += 1
            run = [y - j for j in range(k - 1, -1, -1)]          # north -> south
            consumed[run, x] = True
            x0, x1 = ox + x * CELL, ox + (x + 1) * CELL
            zf = oz + (y + 1) * CELL                               # south edge of the run
            height = CELL * k - CELL // 2
            north = int(tm.cells[run[0], x])
            # Top: the upper half of the northmost cell's art.
            q.add([(x0, height, zf - 8), (x1, height, zf - 8), (x1, height, zf), (x0, height, zf)],
                  atlas.uv(atlas.slots[(north, "full")], 0, 8), (0, 1, 0), KIND_TOP)
            # Front face, top to bottom: lower half of the north cell, then whole cells.
            # Each band also gets left/right sides coloured by the art's edge columns.
            top = height
            for j, row in enumerate(run):
                mid = int(tm.cells[row, x])
                r0 = 8 if j == 0 else 0
                bottom = top - (SLOT - r0)
                u0, v0, u1, v1 = atlas.uv(atlas.slots[(mid, "full")], r0, SLOT)
                q.add([(x0, top, zf), (x1, top, zf), (x1, bottom, zf), (x0, bottom, zf)],
                      (u0, v0, u1, v1), (0, 0, 1), KIND_FACE)
                du = (u1 - u0) / (2 * SLOT)                  # half a texel: sample the edge column
                for xs, uc, nx in ((x0, u0 + du, -1), (x1, u1 - du, 1)):
                    q.add([(xs, top, zf - 8), (xs, top, zf), (xs, bottom, zf), (xs, bottom, zf - 8)],
                          (uc, v0, uc, v1), (nx, 0, 0), KIND_SIDE)
                top = bottom
            y -= k

    for y in range(rows):
        for x in range(cols):
            if consumed[y, x]:
                # Hidden in the game's view (the block covers it); seen when orbiting.
                # Continue the floor of the nearest walkable cell in this column.
                x0, x1 = ox + x * CELL, ox + (x + 1) * CELL
                z0, z1 = oz + y * CELL, oz + (y + 1) * CELL
                slot = atlas.slots["fill"]
                for yy in [*range(y + 1, rows), *range(y - 1, -1, -1)]:
                    if not raised[yy, x] and (int(tm.cells[yy, x]), "under") in atlas.slots:
                        slot = atlas.slots[(int(tm.cells[yy, x]), "under")]
                        break
                q.add([(x0, 0, z0), (x1, 0, z0), (x1, 0, z1), (x0, 0, z1)],
                      atlas.uv(slot), (0, 1, 0), KIND_FLOOR)
                continue
            mid = int(tm.cells[y, x])
            if (mid, "under") not in atlas.slots:
                continue
            x0, x1 = ox + x * CELL, ox + (x + 1) * CELL
            z0, z1 = oz + y * CELL, oz + (y + 1) * CELL
            q.add([(x0, 0, z0), (x1, 0, z0), (x1, 0, z1), (x0, 0, z1)],
                  atlas.uv(atlas.slots[(mid, "base" if prop_cells[y, x] else "under")]), (0, 1, 0), KIND_FLOOR)
            if (mid, "over") in atlas.slots and not prop_cells[y, x]:
                zc = z1 + CARD_OFFSET
                q.add([(x0, CELL, zc), (x1, CELL, zc), (x1, 0, zc), (x0, 0, zc)],
                      atlas.uv(atlas.slots[(mid, "over")]), (0, 0, 1), KIND_CARD)

    n = len(q.pos)
    return Diorama(np.asarray(q.pos, np.float32).reshape(n, 4, 3), np.asarray(q.uv, np.float32).reshape(n, 4, 2),
                   np.asarray(q.normal, np.float32).reshape(n, 3), np.asarray(q.kind, np.uint8))


def project(points: np.ndarray, camera_origin: tuple[float, float]) -> np.ndarray:
    """The game's oblique projection: world (x, y, z) -> screen (x - ox, z - y - oy)."""
    ox, oy = camera_origin
    return np.stack([points[..., 0] - ox, points[..., 2] - points[..., 1] - oy], axis=-1)


def rasterize(dio: Diorama, atlas: Atlas, camera_origin: tuple[float, float],
              size: tuple[int, int] = (240, 160)) -> np.ndarray:
    """Reference software renderer for a diorama in the game's projection (nearest
    depth y + z wins, alpha < 128 discarded). For tests and debugging: the result
    should equal the original game picture's map layers exactly."""
    w, h = size
    img = np.zeros((h, w, 3), np.uint8)
    depth = np.full((h, w), -np.inf)
    ah, aw = atlas.image.shape[:2]
    ys, xs = np.mgrid[0:h, 0:w] + 0.5
    for p, uv in zip(dio.positions, dio.uvs):
        sxy = project(p, camera_origin)
        x0, x1, y0, y1 = sxy[:, 0].min(), sxy[:, 0].max(), sxy[:, 1].min(), sxy[:, 1].max()
        if x1 - x0 < 1e-6 or y1 - y0 < 1e-6:
            continue                                   # edge-on in this projection
        m = (xs >= x0) & (xs < x1) & (ys >= y0) & (ys < y1)
        if not m.any():
            continue
        fx, fy = (xs[m] - x0) / (x1 - x0), (ys[m] - y0) / (y1 - y0)   # quads project to rectangles
        tx = np.clip(((uv[0, 0] + fx * (uv[1, 0] - uv[0, 0])) * aw).astype(int), 0, aw - 1)
        ty = np.clip(((uv[0, 1] + fy * (uv[3, 1] - uv[0, 1])) * ah).astype(int), 0, ah - 1)
        texel = atlas.image[ty, tx]
        d = (p[0, 1] + p[0, 2]) + fy * ((p[3, 1] + p[3, 2]) - (p[0, 1] + p[0, 2]))
        ri, ci = np.nonzero(m)
        ok = (texel[:, 3] >= 128) & (d > depth[ri, ci])
        img[ri[ok], ci[ok]] = texel[ok, :3]
        depth[ri[ok], ci[ok]] = d[ok]
    return img
