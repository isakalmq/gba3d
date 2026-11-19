"""A snapshot of the GBA's display-related memory, plus register accessors."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# Region ids and mirrored sizes; must match lua/shadow_bridge.lua.
REGION_IO, REGION_PALETTE, REGION_VRAM, REGION_OAM = 0, 1, 2, 3
REGION_SIZES = {
    REGION_IO: 0x400,
    REGION_PALETTE: 0x400,
    REGION_VRAM: 0x18000,
    REGION_OAM: 0x400,
}

SCREEN_W, SCREEN_H = 240, 160


@dataclass
class GBAState:
    """Raw display memory. Arrays are uint8 views of the mirrored regions."""

    io: np.ndarray = field(default_factory=lambda: np.zeros(REGION_SIZES[REGION_IO], np.uint8))
    palette: np.ndarray = field(default_factory=lambda: np.zeros(REGION_SIZES[REGION_PALETTE], np.uint8))
    vram: np.ndarray = field(default_factory=lambda: np.zeros(REGION_SIZES[REGION_VRAM], np.uint8))
    oam: np.ndarray = field(default_factory=lambda: np.zeros(REGION_SIZES[REGION_OAM], np.uint8))
    frame: int = 0

    def region(self, rid: int) -> np.ndarray:
        return (self.io, self.palette, self.vram, self.oam)[rid]

    def copy(self) -> "GBAState":
        return GBAState(self.io.copy(), self.palette.copy(), self.vram.copy(), self.oam.copy(), self.frame)

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
