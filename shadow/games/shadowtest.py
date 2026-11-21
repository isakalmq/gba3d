"""Adapter for testrom/shadowtest.gba: one "player" entity in a fixed EWRAM table.

The test ROM builds sprites in a RAM copy of OAM during its logic and uploads
them at the next VBlank, like most commercial games, so RAM is one frame ahead
of the picture and `ram_delay` is 1.
"""
from __future__ import annotations

from ..entities import FACING_NAMES, Entity
from ..state import GBAState

ENTITY_TABLE = 0x02000000   # struct Entity in testrom/main.c, 12 bytes each
ENTITY_SIZE = 12


class ShadowTestAdapter:
    name = "shadowtest"
    game_codes = ("STST",)
    ram_ranges = [(ENTITY_TABLE, ENTITY_SIZE)]
    ram_delay = 1
    hud_bgs = frozenset()        # no UI layer in the test ROM

    def extract(self, st: GBAState):
        base = ENTITY_TABLE
        if not st.u8(base):
            return [], None
        x, y = st.s16(base + 4), st.s16(base + 6)
        w, h = st.u16(base + 8), st.u16(base + 10)
        player = Entity(
            id="player",
            kind="player",
            screen=(x + w / 2, y + h),
            pos=(x + w / 2, y + h),          # the test ROM has no camera: world == screen
            facing=FACING_NAMES.get(st.u8(base + 2)),
            moving=True,
            size=(w, h),
            oam=[st.u8(base + 3)],
        )
        return [player], None


ADAPTER = ShadowTestAdapter()
