"""Pokemon Emerald (US, game code BPEE): the player only.

Addresses and struct layouts come from the pret/pokeemerald decompilation
(`symbols` branch: pokeemerald.sym; include/global.fieldmap.h, include/sprite.h).
Other languages/revisions use different addresses and are not supported.

What is read:
  gMain.callback2         is the overworld running? (CB2_Overworld)
  gPlayerAvatar           which object event / sprite is the player, bike/surf state
  gObjectEvents[id]       logical tile, facing, elevation
  gSprites[spriteId]      exact pixel position, jump offset, OAM attributes
  gSpriteCoordOffsetX/Y   camera offset applied to field sprites (-> screen position)
  gSaveBlock1Ptr->pos     camera tile, gFieldCamera.x/y sub-tile camera offset,
  gTotalCameraPixelOffset accumulated camera scroll (-> world position)

Timing: Emerald runs its logic, builds a RAM copy of OAM, and uploads it in the
next VBlank (VBlankCB_Overworld -> LoadOam), so RAM read at VBlank is one frame
ahead of the picture: ram_delay = 1.
"""
from __future__ import annotations

from ..entities import FACING_NAMES, Entity, oam_size, oam_slots_for_tiles
from ..state import GBAState

G_MAIN = 0x030022C0                # struct Main; callback2 at +4
CB2_OVERWORLD = 0x08085E5C
G_SPRITES = 0x02020630             # struct Sprite[65], 0x44 bytes each
SPRITE_SIZE = 0x44
N_SPRITES = 65
G_SPRITE_COORD_OFFSET_X = 0x02021BBC
G_SPRITE_COORD_OFFSET_Y = 0x02021BBE
G_SAVEBLOCK1 = 0x02025A00          # gSaveBlock1Ptr points here + a random offset (< 0x80)
G_OBJECT_EVENTS = 0x02037350       # struct ObjectEvent[16], 0x24 bytes each
OBJECT_EVENT_SIZE = 0x24
G_PLAYER_AVATAR = 0x02037590       # struct PlayerAvatar
G_SAVEBLOCK1_PTR = 0x03005D8C
G_FIELD_CAMERA = 0x03005DD0        # struct CameraObject; x at +0x10, y at +0x14 (s32)
G_TOTAL_CAMERA_PIXEL_OFFSET_Y = 0x03005DE8
G_TOTAL_CAMERA_PIXEL_OFFSET_X = 0x03005DEC
MAP_OFFSET = 7                     # object event coords include a 7-tile border

AVATAR_FLAGS = ["on_foot", "mach_bike", "acro_bike", "surfing", "underwater",
                "controllable", "forced_move", "dash"]


def _s16(v: int) -> int:
    return ((v + 0x8000) & 0xFFFF) - 0x8000


class PokemonEmeraldAdapter:
    name = "pokeemerald (player only)"
    game_codes = ("BPEE",)
    ram_ranges = [
        (G_SPRITES, SPRITE_SIZE * N_SPRITES),
        (G_SPRITE_COORD_OFFSET_X, 4),
        (G_SAVEBLOCK1, 0x88),
        (G_OBJECT_EVENTS, G_PLAYER_AVATAR + 0x24 - G_OBJECT_EVENTS),   # object events + avatar
        (G_MAIN, 8),
        (G_SAVEBLOCK1_PTR, G_TOTAL_CAMERA_PIXEL_OFFSET_X + 2 - G_SAVEBLOCK1_PTR),
    ]
    ram_delay = 1
    # Overworld BGs (sOverworldBgTemplates): BG0 = text boxes / menus (priority 0),
    # BG1 = map top layer (1), BG2/BG3 = map middle/bottom layers (2/3).
    hud_bgs = frozenset({0})

    def extract(self, st: GBAState):
        if (st.u32(G_MAIN + 4) & ~1) != CB2_OVERWORLD:
            return [], None                         # battle, menu screen, title...

        av = G_PLAYER_AVATAR
        sprite_id, oe_id = st.u8(av + 4), st.u8(av + 5)
        if oe_id >= 16 or sprite_id >= N_SPRITES:
            return [], None
        oe = G_OBJECT_EVENTS + oe_id * OBJECT_EVENT_SIZE
        if not st.u8(oe) & 1:                       # ObjectEvent.active
            return [], None
        spr = G_SPRITES + sprite_id * SPRITE_SIZE
        sprite_flags = st.u8(spr + 0x3E)
        if not sprite_flags & 1:                    # Sprite.inUse
            return [], None

        # Sprite position (see UpdateOamCoords in src/sprite.c).
        x, y = st.s16(spr + 0x20), st.s16(spr + 0x22)
        x2, y2 = st.s16(spr + 0x24), st.s16(spr + 0x26)
        ctc_y = st.s8(spr + 0x29)
        off_x, off_y = st.s16(G_SPRITE_COORD_OFFSET_X), st.s16(G_SPRITE_COORD_OFFSET_Y)
        screen = (x + x2 + off_x, y + y2 + off_y - ctc_y)     # middle of the feet

        # World position. Field sprites are created at
        #   sprite.x = mapX*16 + 8 - T - cam,  sprite.y = mapY*16 + 16 + ctcY - T - cam
        # (SetSpritePosToMapCoords), where T = gTotalCameraPixelOffset and cam is
        # the camera's smooth position; T + cam stays constant while the camera
        # scrolls, so this recovers map pixels for any frame.
        sav = st.u32(G_SAVEBLOCK1_PTR)
        cam_tile = (st.s16(sav), st.s16(sav + 2))
        map_id = (st.s8(sav + 4), st.s8(sav + 5))
        fx, fy = st.s32(G_FIELD_CAMERA + 0x10), st.s32(G_FIELD_CAMERA + 0x14)
        tx, ty = st.u16(G_TOTAL_CAMERA_PIXEL_OFFSET_X), st.u16(G_TOTAL_CAMERA_PIXEL_OFFSET_Y)
        cam_x = cam_tile[0] * 16 + fx - (16 if fx > 0 else 0) + (16 if fx < 0 else 0)
        cam_y = cam_tile[1] * 16 + fy - (16 if fy > 0 else 0) + (16 if fy < 0 else 0)
        pos = (_s16(x + tx + cam_x) - MAP_OFFSET * 16,
               _s16(y - ctc_y + ty + cam_y) - MAP_OFFSET * 16)

        cur = (st.s16(oe + 0x10) - MAP_OFFSET, st.s16(oe + 0x12) - MAP_OFFSET)
        prev = (st.s16(oe + 0x14) - MAP_OFFSET, st.s16(oe + 0x16) - MAP_OFFSET)
        dirs = st.u16(oe + 0x18)
        flags = st.u8(av + 0)

        # Hardware sprites drawn from this sprite's tiles + palette (covers the
        # two-piece split the game uses when the player stands in tall grass).
        a0, a1, a2 = st.u16(spr), st.u16(spr + 2), st.u16(spr + 4)
        w, h = oam_size(a0 >> 14, a1 >> 14)
        n_tiles = (w * h) // 64 * (2 if a0 & 0x2000 else 1)
        oam = oam_slots_for_tiles(st, a2 & 0x3FF, n_tiles, a2 >> 12) if not sprite_flags & 4 else []

        player = Entity(
            id="player",
            kind="player",
            screen=screen,
            pos=pos,
            tile=cur,
            facing=FACING_NAMES.get(dirs & 0xF),
            moving=st.u8(av + 3) != 0 or cur != prev,   # tileTransitionState
            size=(w, h),
            oam=oam,
            props={
                "prev_tile": prev,
                "movement_direction": FACING_NAMES.get((dirs >> 4) & 0xF),
                "avatar": [name for i, name in enumerate(AVATAR_FLAGS) if flags & (1 << i)],
                "running_state": st.u8(av + 2),
                "tile_transition_state": st.u8(av + 3),
                "gender": "female" if st.u8(av + 7) else "male",
                "elevation": st.u8(oe + 0x0B) & 0xF,
                "graphics_id": st.u8(oe + 5),
                "jump_offset": (x2, y2),             # y2 < 0 while hopping a ledge
                "anim": (st.u8(spr + 0x2A), st.u8(spr + 0x2B)),
                "invisible": bool(sprite_flags & 4),
                "map": map_id,
            },
        )
        camera = {
            # World pixel shown at the top-left of the screen, and the map it is on.
            "origin": (pos[0] - (x + off_x), pos[1] - (y + off_y - ctc_y)),
            "map": map_id,
        }
        return [player], camera


ADAPTER = PokemonEmeraldAdapter()
