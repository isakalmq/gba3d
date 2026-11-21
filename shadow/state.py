"""A snapshot of the GBA's display memory and watched work RAM, plus accessors."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# Region ids and mirrored sizes; must match lua/shadow_bridge.lua.
REGION_IO, REGION_PALETTE, REGION_VRAM, REGION_OAM = 0, 1, 2, 3
REGION_EWRAM, REGION_IWRAM = 4, 5
REGION_SIZES = {
    REGION_IO: 0x400,
    REGION_PALETTE: 0x400,
    REGION_VRAM: 0x18000,
    REGION_OAM: 0x400,
    REGION_EWRAM: 0x40000,
    REGION_IWRAM: 0x8000,
}
# Bus address of each work-RAM region.
RAM_BASES = {REGION_EWRAM: 0x02000000, REGION_IWRAM: 0x03000000}

SCREEN_W, SCREEN_H = 240, 160


class MemoryNotWatched(LookupError):
    """Raised when an adapter reads RAM the bridge has not streamed (yet)."""


def _zeros(region: int) -> np.ndarray:
    return np.zeros(REGION_SIZES[region], np.uint8)


@dataclass
class GBAState:
    """Raw display memory and work RAM. Arrays are uint8 mirrors of the regions.

    Work RAM is only partially mirrored (the ranges a game adapter asked for);
    `ram_valid` marks which bytes have actually been received.
    """

    io: np.ndarray = field(default_factory=lambda: _zeros(REGION_IO))
    palette: np.ndarray = field(default_factory=lambda: _zeros(REGION_PALETTE))
    vram: np.ndarray = field(default_factory=lambda: _zeros(REGION_VRAM))
    oam: np.ndarray = field(default_factory=lambda: _zeros(REGION_OAM))
    ewram: np.ndarray = field(default_factory=lambda: _zeros(REGION_EWRAM))
    iwram: np.ndarray = field(default_factory=lambda: _zeros(REGION_IWRAM))
    ram_valid: dict = field(default_factory=lambda: {r: np.zeros(REGION_SIZES[r], bool) for r in RAM_BASES})
    frame: int = 0

    def region(self, rid: int) -> np.ndarray:
        return (self.io, self.palette, self.vram, self.oam, self.ewram, self.iwram)[rid]

    def write(self, rid: int, offset: int, data: np.ndarray) -> None:
        self.region(rid)[offset:offset + len(data)] = data
        if rid in self.ram_valid:
            self.ram_valid[rid][offset:offset + len(data)] = True

    def copy(self) -> "GBAState":
        return GBAState(self.io.copy(), self.palette.copy(), self.vram.copy(), self.oam.copy(),
                        self.ewram.copy(), self.iwram.copy(),
                        {r: v.copy() for r, v in self.ram_valid.items()}, self.frame)

    # -- work RAM by bus address --------------------------------------------
    def ram(self, addr: int, n: int) -> np.ndarray:
        """`n` bytes of EWRAM/IWRAM at bus address `addr`. Raises MemoryNotWatched if not streamed."""
        for rid, base in RAM_BASES.items():
            off = addr - base
            if 0 <= off and off + n <= REGION_SIZES[rid]:
                if not self.ram_valid[rid][off:off + n].all():
                    raise MemoryNotWatched(f"{addr:08X}+{n:X} has not been streamed")
                return self.region(rid)[off:off + n]
        raise MemoryNotWatched(f"{addr:08X}+{n:X} is not in EWRAM/IWRAM")

    def u8(self, addr: int) -> int:
        return int(self.ram(addr, 1)[0])

    def s8(self, addr: int) -> int:
        v = self.u8(addr)
        return v - 0x100 if v & 0x80 else v

    def u16(self, addr: int) -> int:
        b = self.ram(addr, 2)
        return int(b[0]) | (int(b[1]) << 8)

    def s16(self, addr: int) -> int:
        v = self.u16(addr)
        return v - 0x10000 if v & 0x8000 else v

    def u32(self, addr: int) -> int:
        b = self.ram(addr, 4)
        return int(b[0]) | (int(b[1]) << 8) | (int(b[2]) << 16) | (int(b[3]) << 24)

    def s32(self, addr: int) -> int:
        v = self.u32(addr)
        return v - 0x100000000 if v & 0x80000000 else v

    # -- register helpers -------------------------------------------------
    def reg(self, offset: int) -> int:
        """Unsigned 16-bit I/O register at `offset` (e.g. 0x00 for DISPCNT)."""
        return int(self.io[offset]) | (int(self.io[offset + 1]) << 8)

    def reg_s16(self, offset: int) -> int:
        v = self.reg(offset)
        return v - 0x10000 if v & 0x8000 else v

    def reg_s28(self, offset: int) -> int:
        """Signed 28-bit affine reference point (BGxX / BGxY), 8 fractional bits."""
        v = self.reg(offset) | (self.reg(offset + 2) << 16)
        v &= 0x0FFFFFFF
        return v - 0x10000000 if v & 0x08000000 else v

    @property
    def dispcnt(self) -> int:
        return self.reg(0x00)

    @property
    def mode(self) -> int:
        return self.dispcnt & 7

    # -- typed views ------------------------------------------------------
    @property
    def palette16(self) -> np.ndarray:
        return self.palette.view("<u2")

    @property
    def vram16(self) -> np.ndarray:
        return self.vram.view("<u2")

    @property
    def oam16(self) -> np.ndarray:
        return self.oam.view("<u2")
