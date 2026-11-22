"""Prop kit: real 3D models in place of the furniture drawn into a map's tiles.

The automatic diorama (tilemap.py) only knows "walkable" and "impassable", so furniture
becomes boxes. A prop kit names patterns of metatiles that draw one piece of furniture
(a 2x2 table, a chair) and the model that replaces them. Where a pattern matches:

* the cells it covers lose their block / card and get a plain floor drawn from the
  metatile's bottom layer only (the furniture is on the layer above it in the games
  this is made for: Pokemon's "covered" metatiles draw floor + furniture);
* the client places the model on that floor.

Models live in shadow/web/models/<model>.glb (exported from models/props/*.blend with
`make models`). Model space: 1 unit = one 16 px cell, origin at the centre of the
footprint on the floor, +x east, +z south (glTF; Blender's -y), +y up. The sizes are
taken from the original art seen through the game's oblique projection (screen y = z - y),
so from the game's point of view the model covers about the same pixels as the art.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

CELL = 16


@dataclass(frozen=True)
class PropDef:
    model: str                              # file name without .glb under shadow/web/models/
    tileset: int                            # tileset (adapter's id) the metatiles belong to
    pattern: tuple[tuple[int, ...], ...]    # metatile ids, rows north -> south
    anchor: tuple[float, float]             # model origin, px from the pattern's top-left corner
    yaw: float = 0.0                        # degrees, counter-clockwise seen from above


@dataclass
class PlacedProp:
    model: str
    position: tuple[float, float]           # world px (x, z) of the model origin
    yaw: float
    cells: list[tuple[int, int]]            # (row, col) cells it replaces

    def to_dict(self) -> dict:
        return {"model": self.model, "x": self.position[0], "z": self.position[1], "yaw": self.yaw,
                "cells": [list(c) for c in self.cells]}


def place_props(tm, kit) -> list[PlacedProp]:
    """Every match of the kit's patterns in the map. A cell is used by one prop at most;
    larger patterns are matched first."""
    tilesets = set(getattr(tm, "tilesets", ()))
    rows, cols = tm.shape
    used = np.zeros((rows, cols), bool)
    out = []
    for p in sorted((p for p in kit if p.tileset in tilesets), key=lambda p: -sum(map(len, p.pattern))):
        pat = np.array(p.pattern, np.int64)
        ph, pw = pat.shape
        for y in range(rows - ph + 1):
            for x in range(cols - pw + 1):
                if used[y:y + ph, x:x + pw].any() or not np.array_equal(tm.cells[y:y + ph, x:x + pw], pat):
                    continue
                used[y:y + ph, x:x + pw] = True
                ox, oz = tm.origin[0] + x * CELL, tm.origin[1] + y * CELL
                out.append(PlacedProp(p.model, (ox + p.anchor[0], oz + p.anchor[1]), p.yaw,
                                      [(y + j, x + i) for j in range(ph) for i in range(pw)]))
    return out
