"""Live collision grid from the game's map buffer, plus pathfinding.

The grid is read from gBackupMapLayout (the map plus a 7-tile margin holding the
connected maps), so it always reflects runtime changes such as opened doors, cut
trees and `setmetatile` in scripts. Passability follows the game's own rules
(GetCollisionAtCoords / CheckForObjectEventCollision): collision bits, elevation,
one-way walls, ledges, and the map-connection edge. Public coordinates are map
coordinates; buffer coordinates are +7.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass

import numpy as np

from . import data as D
from .data import EmeraldData
from .snapshot import Memory


@dataclass
class Step:
    x: int          # map coordinates of the tile reached by this step
    y: int
    direction: int  # DIR_* to press
    jump: bool = False


class MapGrid:
    def __init__(self, blocks: np.ndarray, behavior: np.ndarray, connections: int, d: EmeraldData):
        self.blocks = blocks                      # (H, W) uint16, buffer coords
        self.behavior = behavior                  # (H, W) uint8
        self.height, self.width = blocks.shape
        self.connections = connections            # bit0 south, bit1 north, bit2 west, bit3 east
        self.d = d
        mb = d.mb
        self._jump = {D.DIR_SOUTH: mb["MB_JUMP_SOUTH"], D.DIR_NORTH: mb["MB_JUMP_NORTH"],
                      D.DIR_WEST: mb["MB_JUMP_WEST"], D.DIR_EAST: mb["MB_JUMP_EAST"]}
        blocked = {
            "N": {"MB_IMPASSABLE_NORTH", "MB_IMPASSABLE_NORTHEAST", "MB_IMPASSABLE_NORTHWEST",
                  "MB_IMPASSABLE_SOUTH_AND_NORTH"},
            "S": {"MB_IMPASSABLE_SOUTH", "MB_IMPASSABLE_SOUTHEAST", "MB_IMPASSABLE_SOUTHWEST",
                  "MB_IMPASSABLE_SOUTH_AND_NORTH"},
            "E": {"MB_IMPASSABLE_EAST", "MB_IMPASSABLE_NORTHEAST", "MB_IMPASSABLE_SOUTHEAST",
                  "MB_IMPASSABLE_WEST_AND_EAST", "MB_SECRET_BASE_BREAKABLE_DOOR"},
            "W": {"MB_IMPASSABLE_WEST", "MB_IMPASSABLE_NORTHWEST", "MB_IMPASSABLE_SOUTHWEST",
                  "MB_IMPASSABLE_WEST_AND_EAST", "MB_SECRET_BASE_BREAKABLE_DOOR"},
        }
        ids = {k: frozenset(mb[n] for n in v) for k, v in blocked.items()}
        # gOppositeDirectionBlockedMetatileFuncs (current tile) / gDirectionBlockedMetatileFuncs (dest)
        self._blocked_from = {D.DIR_SOUTH: ids["S"], D.DIR_NORTH: ids["N"], D.DIR_WEST: ids["W"], D.DIR_EAST: ids["E"]}
        self._blocked_into = {D.DIR_SOUTH: ids["N"], D.DIR_NORTH: ids["S"], D.DIR_WEST: ids["E"], D.DIR_EAST: ids["W"]}
        self._encounter = d.encounter

    # -- cell queries (buffer coords) -------------------------------------
    def _inside(self, bx: int, by: int) -> bool:
        return 0 <= bx < self.width and 0 <= by < self.height

    def collision(self, bx: int, by: int) -> int:
        if not self._inside(bx, by):
            return 1
        b = int(self.blocks[by, bx])
        return 1 if b == D.GRID_UNDEFINED else (b & D.GRID_COLLISION_MASK) >> 10

    def elevation(self, bx: int, by: int) -> int:
        if not self._inside(bx, by):
            return 0
        b = int(self.blocks[by, bx])
        return 0 if b == D.GRID_UNDEFINED else b >> D.GRID_ELEVATION_SHIFT

    def behavior_at(self, bx: int, by: int) -> int:
        return int(self.behavior[by, bx]) if self._inside(bx, by) else 0

    def _edge_ok(self, bx: int, by: int) -> bool:
        """GetMapBorderIdAt != CONNECTION_INVALID."""
        if not self._inside(bx, by) or int(self.blocks[by, bx]) == D.GRID_UNDEFINED:
            return False
        c = self.connections
        if bx >= self.width - (D.MAP_OFFSET + 1):
            return bool(c & 8)
        if bx < D.MAP_OFFSET:
            return bool(c & 4)
        if by >= self.height - D.MAP_OFFSET:
            return bool(c & 1)
        if by < D.MAP_OFFSET:
            return bool(c & 2)
        return True

    @staticmethod
    def _elev_mismatch(cur: int, tile: int) -> bool:
        if cur == D.ELEVATION_TRANSITION:
            return False
        if tile in (D.ELEVATION_TRANSITION, D.ELEVATION_MULTI_LEVEL):
            return False
        return tile != cur

    def neighbors(self, bx: int, by: int, elev: int, surfing: bool, blocked: set[tuple[int, int]]):
        """Yield (nbx, nby, new_elev, direction, jump, cost) the player can reach in one input."""
        cur_beh = self.behavior_at(bx, by)
        for direction, (dx, dy) in D.DIR_VECTORS.items():
            tx, ty = bx + dx, by + dy
            beh = self.behavior_at(tx, ty)
            if beh == self._jump[direction] and not surfing:
                lx, ly = tx + dx, ty + dy
                if (lx, ly) not in blocked and self._inside(lx, ly):
                    yield lx, ly, elev, direction, True, 2
                continue
            if (tx, ty) in blocked:
                continue
            if self.collision(tx, ty) or not self._edge_ok(tx, ty):
                continue
            if cur_beh in self._blocked_from[direction] or beh in self._blocked_into[direction]:
                continue
            tile_elev = self.elevation(tx, ty)
            if self._elev_mismatch(elev, tile_elev):
                # CanStopSurfing: surfing onto default-elevation land is allowed
                if not (surfing and tile_elev == D.ELEVATION_DEFAULT):
                    continue
            new_elev = elev if tile_elev in (D.ELEVATION_TRANSITION, D.ELEVATION_MULTI_LEVEL) else tile_elev
            cost = 1
            if beh in self._encounter:
                cost += 2  # mild preference for avoiding grass
            yield tx, ty, new_elev, direction, False, cost

    # -- searches (map coords in, map coords out) ---------------------------
    def find_path(self, start: tuple[int, int], elev: int, goal: tuple[int, int], surfing: bool = False,
                  blocked: set[tuple[int, int]] | None = None, max_nodes: int = 40000) -> list[Step] | None:
        """A* from `start` to `goal` (map coords). None if unreachable.

        Costs are in tenths of a step; a change of direction costs a little, so of the
        equally short paths the straightest one wins (fewer turns, more natural)."""
        O = D.MAP_OFFSET
        sx, sy = start[0] + O, start[1] + O
        gx, gy = goal[0] + O, goal[1] + O
        blocked_b = {(x + O, y + O) for x, y in (blocked or ())}
        blocked_b.discard((sx, sy))
        if (sx, sy) == (gx, gy):
            return []
        h = lambda x, y: (abs(x - gx) + abs(y - gy)) * 10
        start_state = (sx, sy, elev, 0)
        frontier = [(h(sx, sy), 0, start_state)]
        came: dict[tuple, tuple] = {start_state: None}
        cost_so_far = {start_state: 0}
        expanded = 0
        while frontier:
            _, g, state = heapq.heappop(frontier)
            x, y, e, last_dir = state
            if (x, y) == (gx, gy):
                return self._reconstruct(came, state)
            if g > cost_so_far.get(state, 1 << 30):
                continue
            expanded += 1
            if expanded > max_nodes:
                return None
            for nx, ny, ne, direction, jump, c in self.neighbors(x, y, e, surfing, blocked_b):
                ns = (nx, ny, ne, direction)
                ng = g + c * 10 + (1 if last_dir and direction != last_dir else 0)
                if ng < cost_so_far.get(ns, 1 << 30):
                    cost_so_far[ns] = ng
                    came[ns] = (state, direction, jump)
                    heapq.heappush(frontier, (ng + h(nx, ny), ng, ns))
        return None

    def _reconstruct(self, came, state) -> list[Step]:
        O = D.MAP_OFFSET
        steps = []
        while came[state] is not None:
            prev, direction, jump = came[state]
            steps.append(Step(state[0] - O, state[1] - O, direction, jump))
            state = prev
        steps.reverse()
        return steps

    def reachable(self, start: tuple[int, int], elev: int, surfing: bool = False,
                  blocked: set[tuple[int, int]] | None = None) -> set[tuple[int, int]]:
        """All tiles (map coords) reachable from `start`."""
        O = D.MAP_OFFSET
        blocked_b = {(x + O, y + O) for x, y in (blocked or ())}
        s = (start[0] + O, start[1] + O, elev)
        seen = {s}
        stack = [s]
        while stack:
            x, y, e = stack.pop()
            for nx, ny, ne, *_ in self.neighbors(x, y, e, surfing, blocked_b):
                if (nx, ny, ne) not in seen:
                    seen.add((nx, ny, ne))
                    stack.append((nx, ny, ne))
        return {(x - O, y - O) for x, y, _ in seen}

    def nearest_reachable(self, start: tuple[int, int], elev: int, target: tuple[int, int],
                          surfing: bool = False, blocked: set[tuple[int, int]] | None = None) -> tuple[int, int] | None:
        """The reachable tile closest to `target`: how an approximate location is snapped."""
        tiles = self.reachable(start, elev, surfing, blocked)
        if not tiles:
            return None
        tx, ty = target
        return min(tiles, key=lambda p: ((p[0] - tx) ** 2 + (p[1] - ty) ** 2, abs(p[0] - start[0]) + abs(p[1] - start[1])))

    def map_bounds(self) -> tuple[int, int]:
        """(width, height) of the map proper, in map coordinates."""
        return self.width - 2 * D.MAP_OFFSET - 1, self.height - 2 * D.MAP_OFFSET


class GridReader:
    """Reads MapGrid from memory, caching the metatile attribute table per tileset pair."""

    def __init__(self, d: EmeraldData):
        self.d = d
        self._attr_cache: dict[tuple[int, int], np.ndarray] = {}

    def _attributes(self, mem: Memory, layout: int) -> np.ndarray:
        primary, secondary = mem.u32(layout + 0x10), mem.u32(layout + 0x14)
        key = (primary, secondary)
        lut = self._attr_cache.get(key)
        if lut is None:
            lut = np.zeros(D.NUM_METATILES_TOTAL, np.uint16)
            for base, ts in ((0, primary), (D.NUM_METATILES_IN_PRIMARY, secondary)):
                if ts:
                    attrs = mem.u32(ts + 0x10)
                    if attrs:
                        lut[base:base + D.NUM_METATILES_IN_PRIMARY] = np.frombuffer(
                            mem.read(attrs, D.NUM_METATILES_IN_PRIMARY * 2), "<u2")
            self._attr_cache[key] = lut
        return lut

    def read(self, mem: Memory) -> MapGrid | None:
        S = self.d.sym
        bml = S["gBackupMapLayout"]
        w, h, ptr = mem.u32(bml), mem.u32(bml + 4), mem.u32(bml + 8)
        if not (0 < w <= 0x200 and 0 < h <= 0x200 and 0x02000000 <= ptr < 0x02040000):
            return None
        blocks = np.frombuffer(mem.read(ptr, w * h * 2), "<u2").reshape(h, w).copy()
        layout = mem.u32(S["gMapHeader"])
        lut = self._attributes(mem, layout)
        ids = blocks & D.GRID_METATILE_MASK
        behavior = (lut[np.minimum(ids, D.NUM_METATILES_TOTAL - 1)] & 0xFF).astype(np.uint8)
        return MapGrid(blocks, behavior, mem.u8(S["sMapConnectionFlags"]), self.d)
