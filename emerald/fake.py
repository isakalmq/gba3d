"""A tiny stand-in for Pokémon Emerald, for tests and `python -m emerald view --demo`.

It lays out memory exactly where and how the real game does (symbol addresses and
struct layouts from emerald/data/bpee.json and the decomp), and simulates just enough
of the overworld to exercise the host: grid walking with turn-in-place, running,
ledges, elevation, NPCs, message boxes, a yes/no menu, a trainer-style step trigger,
a wild encounter tile and a map connection.

It implements the subset of shadow.core.Core the host uses (memory reads, set_keys,
run_frame, frame).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from . import data as D
from .text import encode_simple
from shadow.core import KEY_A, KEY_B, KEY_DOWN, KEY_LEFT, KEY_RIGHT, KEY_UP

EWRAM, IWRAM, ROM = 0x02000000, 0x03000000, 0x08000000
SB1 = 0x02025A00          # where gSaveBlock1Ptr points
MAP_BUF = 0x02032000      # backup map buffer
ROM_LAYOUT = 0x08100000   # MapLayout struct
ROM_TILESET_P, ROM_TILESET_S = 0x08100100, 0x08100200
ROM_ATTR_P, ROM_ATTR_S = 0x08101000, 0x08102000

KEY_DIRS = {KEY_DOWN: D.DIR_SOUTH, KEY_UP: D.DIR_NORTH, KEY_LEFT: D.DIR_WEST, KEY_RIGHT: D.DIR_EAST}

# metatile ids used by the fake map (attributes table index)
MT_FLOOR, MT_WALL, MT_GRASS, MT_LEDGE_S, MT_WATER = 1, 2, 3, 4, 5


@dataclass
class FakeNPC:
    local_id: int
    x: int
    y: int
    gfx: int = 7
    text: str = "Hello there!"
    yesno: bool = False
    facing: int = D.DIR_SOUTH


@dataclass
class FakeMapSpec:
    rows: list[str]
    group: int = 0
    num: int = 9          # LITTLEROOT_TOWN
    npcs: list[FakeNPC] = field(default_factory=list)
    trigger: dict = field(default_factory=dict)       # (x, y) -> text
    north: "FakeMapSpec | None" = None                # connection to the north (offset 0)


LEGEND = {".": (MT_FLOOR, 0, 3), "#": (MT_WALL, 1, 3), "g": (MT_GRASS, 0, 3), "v": (MT_LEDGE_S, 1, 3),
          "~": (MT_WATER, 0, 1), "E": (MT_GRASS, 0, 3)}


class FakeEmerald:
    def __init__(self, spec: FakeMapSpec, start: tuple[int, int], running_shoes: bool = True):
        self.d = D.load()
        self.S = self.d.sym
        self.mem = {EWRAM: bytearray(0x40000), IWRAM: bytearray(0x8000), ROM: bytearray(0x200000)}
        self.keys = 0
        self._prev_keys = 0
        self.frame = 0
        self.spec = spec
        self.encounter_frames = 0
        self.step_frames = 0
        self.step_len = 16
        self.turn_frames = 0
        self.msg_pages: list[str] = []
        self.menu_pending = False
        self.menu_result: int | None = None
        self.talked: list[int] = []
        self.encounters = 0
        self.encounter_tiles = set()
        self._write_static(running_shoes)
        self._load_map(spec, start)

    # -- memory ---------------------------------------------------------------
    def _loc(self, addr: int):
        for base, buf in self.mem.items():
            if base <= addr < base + len(buf):
                return buf, addr - base
        raise KeyError(hex(addr))

    def read(self, addr: int, n: int) -> bytes:
        buf, off = self._loc(addr)
        return bytes(buf[off:off + n])

    def u8(self, a): buf, o = self._loc(a); return buf[o]
    def u16(self, a): return struct.unpack("<H", self.read(a, 2))[0]
    def u32(self, a): return struct.unpack("<I", self.read(a, 4))[0]
    def s16(self, a): return struct.unpack("<h", self.read(a, 2))[0]

    def w8(self, a, v): buf, o = self._loc(a); buf[o] = v & 0xFF
    def w16(self, a, v): buf, o = self._loc(a); struct.pack_into("<H", buf, o, v & 0xFFFF)
    def w32(self, a, v): buf, o = self._loc(a); struct.pack_into("<I", buf, o, v & 0xFFFFFFFF)
    def ws16(self, a, v): buf, o = self._loc(a); struct.pack_into("<h", buf, o, v)

    def write(self, a, data: bytes):
        buf, o = self._loc(a)
        buf[o:o + len(data)] = data

    # -- setup ------------------------------------------------------------------
    def _write_static(self, running_shoes: bool):
        S = self.S
        self.write(0x080000AC, b"BPEE")
        self.w32(S["gSaveBlock1Ptr"], SB1)
        self.w32(S["gMain"], S["CB1_Overworld"] | 1)
        self.w32(S["gMain"] + 4, S["CB2_Overworld"] | 1)
        self.w8(S["sGlobalScriptContextStatus"], D.SCRIPT_CONTEXT_SHUTDOWN)
        flag = self.d.const["FLAG_SYS_B_DASH"]
        if running_shoes:
            a = SB1 + self.d.const["SAVEBLOCK1_FLAGS"] + (flag >> 3)
            self.w8(a, self.u8(a) | (1 << (flag & 7)))
        mb = self.d.mb
        attrs = {MT_FLOOR: mb["MB_NORMAL"], MT_WALL: mb["MB_NORMAL"], MT_GRASS: mb["MB_TALL_GRASS"],
                 MT_LEDGE_S: mb["MB_JUMP_SOUTH"], MT_WATER: mb["MB_POND_WATER"]}
        for mt, beh in attrs.items():
            self.w16(ROM_ATTR_P + mt * 2, beh)
        for ts, at in ((ROM_TILESET_P, ROM_ATTR_P), (ROM_TILESET_S, ROM_ATTR_S)):
            self.w32(ts + 0x10, at)
        self.w32(ROM_LAYOUT + 0x10, ROM_TILESET_P)
        self.w32(ROM_LAYOUT + 0x14, ROM_TILESET_S)
        self.w32(S["gMapHeader"], ROM_LAYOUT)

    def _load_map(self, spec: FakeMapSpec, start: tuple[int, int]):
        S = self.S
        self.spec = spec
        rows = spec.rows
        h, w = len(rows), len(rows[0])
        O = D.MAP_OFFSET
        bw, bh = w + 2 * O + 1, h + 2 * O
        self.bw, self.bh = bw, bh
        blocks = [[(MT_WALL | (1 << 10) | (3 << 12)) for _ in range(bw)] for _ in range(bh)]
        self.encounter_tiles = set()
        for y, row in enumerate(rows):
            for x, ch in enumerate(row):
                mt, col, elev = LEGEND[ch]
                blocks[y + O][x + O] = mt | (col << 10) | (elev << 12)
                if ch == "E":
                    self.encounter_tiles.add((x, y))
        conn = 0
        if spec.north is not None:
            conn |= 2
            nrows = spec.north.rows
            for y in range(O):
                src = nrows[len(nrows) - O + y]
                for x, ch in enumerate(src):
                    mt, col, elev = LEGEND[ch]
                    blocks[y][x + O] = mt | (col << 10) | (elev << 12)
        flat = b"".join(struct.pack("<H", v) for row in blocks for v in row)
        self.write(MAP_BUF, flat)
        bml = S["gBackupMapLayout"]
        self.w32(bml, bw)
        self.w32(bml + 4, bh)
        self.w32(bml + 8, MAP_BUF)
        self.w8(S["sMapConnectionFlags"], conn)
        self.w8(SB1 + 4, spec.group)
        self.w8(SB1 + 5, spec.num)
        # objects: slot 0 = player
        for slot in range(D.OBJECT_EVENTS_COUNT):
            self.write(S["gObjectEvents"] + slot * D.OBJECT_EVENT_SIZE, bytes(D.OBJECT_EVENT_SIZE))
        self._set_obj(0, 0xFF, 0, start[0], start[1], D.DIR_SOUTH, player=True)
        for i, npc in enumerate(spec.npcs, start=1):
            self._set_obj(i, npc.local_id, npc.gfx, npc.x, npc.y, npc.facing)
        pa = S["gPlayerAvatar"]
        self.w8(pa, D.AVATAR_ON_FOOT)
        self.w8(pa + 5, 0)

    def _obj(self, slot):
        return self.S["gObjectEvents"] + slot * D.OBJECT_EVENT_SIZE

    def _set_obj(self, slot, local_id, gfx, x, y, facing, player=False):
        b = self._obj(slot)
        self.w8(b, 1)
        self.w8(b + 2, 1 if player else 0)
        self.w8(b + 5, gfx)
        self.w8(b + 8, local_id)
        self.w8(b + 0xB, 3)
        for off in (0xC, 0x10, 0x14):
            self.ws16(b + off, x + D.MAP_OFFSET)
            self.ws16(b + off + 2, y + D.MAP_OFFSET)
        self.w16(b + 0x18, facing | (facing << 4))

    # -- player helpers -----------------------------------------------------------
    def player_pos(self):
        b = self._obj(0)
        return self.s16(b + 0x10) - D.MAP_OFFSET, self.s16(b + 0x12) - D.MAP_OFFSET

    def _facing(self):
        return self.u16(self._obj(0) + 0x18) & 0xF

    def _set_facing(self, d):
        b = self._obj(0)
        self.w16(b + 0x18, d | (d << 4))

    def _tile(self, x, y):
        O = D.MAP_OFFSET
        bx, by = x + O, y + O
        if not (0 <= bx < self.bw and 0 <= by < self.bh):
            return MT_WALL | (1 << 10) | (3 << 12)
        return self.u16(MAP_BUF + (by * self.bw + bx) * 2)

    def _npc_at(self, x, y):
        for i, npc in enumerate(self.spec.npcs, start=1):
            b = self._obj(i)
            if (self.s16(b + 0x10) - D.MAP_OFFSET, self.s16(b + 0x12) - D.MAP_OFFSET) == (x, y):
                return i, npc
        return None

    def move_npc(self, local_id, x, y):
        for i, npc in enumerate(self.spec.npcs, start=1):
            if npc.local_id == local_id:
                b = self._obj(i)
                self.ws16(b + 0x10, x + D.MAP_OFFSET)
                self.ws16(b + 0x12, y + D.MAP_OFFSET)
                self.ws16(b + 0x14, x + D.MAP_OFFSET)
                self.ws16(b + 0x16, y + D.MAP_OFFSET)

    # -- core API -------------------------------------------------------------------
    def set_keys(self, keys: int):
        self.keys = keys

    def run_frame(self):
        new = self.keys & ~self._prev_keys
        held = self.keys
        self._simulate(new, held)
        self._prev_keys = self.keys
        self.frame += 1

    # -- simulation -----------------------------------------------------------------
    def _lock(self, on: bool):
        self.w8(self.S["sLockFieldControls"], 1 if on else 0)
        self.w8(self.S["sGlobalScriptContextStatus"], D.SCRIPT_CONTEXT_RUNNING if on else D.SCRIPT_CONTEXT_SHUTDOWN)

    def _show_text(self, pages: list[str], yesno=False):
        self.msg_pages = list(pages)
        self.menu_pending = yesno
        self._lock(True)
        self._draw_page()

    def _draw_page(self):
        S = self.S
        self.write(S["gStringVar4"], encode_simple(self.msg_pages[0], self.d))
        self.w8(S["sFieldMessageBoxMode"], 2)
        p = S["sTextPrinters"]
        self.w32(p, S["gStringVar4"] + len(self.msg_pages[0]))  # instant text
        self.w8(p + 0x1B, 1)
        self.w8(p + 0x1C, 1)  # waiting for button

    def _close_text(self):
        self.w8(self.S["sFieldMessageBoxMode"], 0)
        self.w8(self.S["sTextPrinters"] + 0x1B, 0)

    def _open_menu(self):
        S = self.S
        t = S["gTasks"]
        self.w32(t, S["Task_HandleYesNoInput"] | 1)
        self.w8(t + 4, 1)
        self.w8(S["sMenu"] + 2, 0)
        self.w8(S["sMenu"] + 3, 0)
        self.w8(S["sMenu"] + 4, 1)

    def _close_menu(self):
        self.w8(self.S["gTasks"] + 4, 0)

    def _simulate(self, new, held):
        S = self.S
        pa = S["gPlayerAvatar"]
        if self.encounter_frames:
            self.encounter_frames -= 1
            if self.encounter_frames == 0:
                self.w32(S["gMain"] + 4, S["CB2_Overworld"] | 1)
            return
        # menu
        if self.u8(S["gTasks"] + 4):
            m = S["sMenu"]
            cur = self.u8(m + 2)
            if new & KEY_DOWN:
                self.w8(m + 2, min(cur + 1, 1))
            elif new & KEY_UP:
                self.w8(m + 2, max(cur - 1, 0))
            elif new & (KEY_A | KEY_B):
                self.menu_result = cur if new & KEY_A else 1
                self._close_menu()
                self._close_text()
                self._lock(False)
            return
        # message box
        if self.u8(S["sFieldMessageBoxMode"]):
            if new & (KEY_A | KEY_B):
                self.msg_pages.pop(0)
                if self.msg_pages:
                    self._draw_page()
                elif self.menu_pending:
                    self.menu_pending = False
                    self._open_menu()
                else:
                    self._close_text()
                    self._lock(False)
            return
        if self.u8(S["sLockFieldControls"]):
            return

        b = self._obj(0)
        x, y = self.player_pos()
        was_moving = self.step_frames > 0
        if self.step_frames:
            self.step_frames -= 1
            if self.step_frames == 0:
                self.ws16(b + 0x14, x + D.MAP_OFFSET)
                self.ws16(b + 0x16, y + D.MAP_OFFSET)
                self.w8(pa + 3, 0)
                if self._on_arrive(x, y):
                    return
            else:
                return
        if self.turn_frames:
            self.turn_frames -= 1
            if self.turn_frames:
                return
            self.w8(pa + 2, 0)
        dirs = [dv for k, dv in KEY_DIRS.items() if held & k]
        if new & KEY_A and not self.step_frames:
            dx, dy = D.DIR_VECTORS[self._facing()]
            hit = self._npc_at(x + dx, y + dy)
            if hit:
                _, npc = hit
                self.talked.append(npc.local_id)
                self._show_text([npc.text], yesno=npc.yesno)
            return
        if not dirs:
            return
        direction = dirs[0]
        if self._facing() != direction and not was_moving:
            # turn in place only from a standstill; mid-walk the player just changes direction
            self._set_facing(direction)
            self.turn_frames = 8
            self.w8(pa + 2, 1)  # runningState = TURN_DIRECTION
            return
        self._set_facing(direction)
        dx, dy = D.DIR_VECTORS[direction]
        tx, ty = x + dx, y + dy
        t = self._tile(tx, ty)
        mt, col, elev = t & 0x3FF, (t >> 10) & 3, t >> 12
        if mt == MT_LEDGE_S and direction == D.DIR_SOUTH:
            tx, ty = tx + dx, ty + dy
        elif col or self._npc_at(tx, ty) or (elev not in (0, 15) and elev != 3):
            return  # bump
        if ty < 0 and self.spec.north is not None:
            # cross the connection: switch maps, shift coordinates
            nh = len(self.spec.north.rows)
            self._load_map(self.spec.north, (tx, ty + nh))
            tx, ty = tx, ty + nh
            b = self._obj(0)
        self.ws16(b + 0x14, x + D.MAP_OFFSET)
        self.ws16(b + 0x16, y + D.MAP_OFFSET)
        self.ws16(b + 0x10, tx + D.MAP_OFFSET)
        self.ws16(b + 0x12, ty + D.MAP_OFFSET)
        self.w8(pa + 3, 1)
        running = bool(held & KEY_B)
        self.step_frames = 8 if running else 16

    def _on_arrive(self, x, y) -> bool:
        if (x, y) in self.spec.trigger:
            self._show_text([self.spec.trigger[(x, y)]])
            return True
        if (x, y) in self.encounter_tiles:
            self.encounters += 1
            self.encounter_tiles.discard((x, y))
            self.w32(self.S["gMain"] + 4, self.S["BattleMainCB2"] | 1)
            self.encounter_frames = 60
            return True
        return False

    # -- display (placeholder: the fake has no graphics) ---------------------------
    def video_rgb(self):
        import numpy as np
        img = np.zeros((160, 240, 3), np.uint8)
        battle = self.u32(self.S["gMain"] + 4) & ~1 == self.S["BattleMainCB2"]
        img[:] = (90, 30, 90) if battle else (30, 34, 40)
        img[::16, :] = 60
        img[:, ::16] = 60
        img[72:88, 112:128] = (255, 255, 255)
        return img


def demo() -> FakeEmerald:
    """A small playground map for `python -m emerald view --demo`."""
    route = FakeMapSpec([
        "....................",
        "..gggg........gggg..",
        "..gEEg........gEEg..",
        "..gggg..####..gggg..",
        "........#..#........",
        "........#..#........",
        "....................",
        "....................",
        "....................",
    ], group=0, num=16, npcs=[FakeNPC(1, 10, 6, gfx=12, text="This is ROUTE 101.")])
    town = FakeMapSpec([
        "....................",
        "....................",
        ".####......~~~~~....",
        ".#..#......~~~~~....",
        ".#..#......~~~~~....",
        "....................",
        "vvvvvvvvvv..........",
        "........gggggg......",
        "........gEEEEg......",
        "........gggggg......",
        "....................",
        "......###....###....",
        "......#.#....#.#....",
        "....................",
    ], npcs=[
        FakeNPC(1, 3, 5, gfx=40, text="Right-click me to talk.\nDo you like this demo?", yesno=True),
        FakeNPC(2, 15, 10, gfx=12, text="Tall grass has wild POKEMON!"),
        FakeNPC(3, 17, 1, gfx=30, text="Walk north to cross into ROUTE 101."),
    ], trigger={(12, 12): "Hey! You stepped on a trigger tile."}, north=route)
    return FakeEmerald(town, (5, 9))
