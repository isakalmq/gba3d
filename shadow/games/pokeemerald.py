"""Pokemon Emerald (US, game code BPEE): the player and the other object events (NPCs).

Addresses and struct layouts come from the pret/pokeemerald decompilation
(`symbols` branch: pokeemerald.sym; include/global.fieldmap.h, include/sprite.h).
Other languages/revisions use different addresses and are not supported.

What is read:
  gMain.callback2         is the overworld running? (CB2_Overworld)
  gPlayerAvatar           which object event / sprite is the player, bike/surf state
  gObjectEvents[16]       every loaded character: logical tile, facing, elevation,
                          graphics id (include/constants/event_objects.h), local id
  gSprites[spriteId]      exact pixel position, jump offset, OAM attributes
  gSpriteCoordOffsetX/Y   camera offset applied to field sprites (-> screen position)
  gSaveBlock1Ptr->pos     camera tile, gFieldCamera.x/y sub-tile camera offset,
  gTotalCameraPixelOffset accumulated camera scroll (-> world position)
  gBackupMapLayout        the live map grid (sBackupMapData): metatile id, collision,
                          elevation per 16x16 cell, including a 7-cell border
  gMapHeader.mapLayout    (ROM) -> tilesets -> metatile definitions and attributes,
                          read from the ROM file in roms/ (see tilemap())

Timing: Emerald runs its logic, builds a RAM copy of OAM, and uploads it in the
next VBlank (VBlankCB_Overworld -> LoadOam), so RAM read at VBlank is one frame
ahead of the picture: ram_delay = 1.
"""
from __future__ import annotations

import hashlib

import numpy as np

from ..entities import FACING_NAMES, Entity, oam_size, oam_slots_for_tiles
from ..state import GBAState, MemoryNotWatched
from ..props import PropDef
from ..tilemap import Metatile, TileMap

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
G_MAP_HEADER = 0x02037318          # struct MapHeader; mapLayout (ROM pointer) at +0
G_BACKUP_MAP_LAYOUT = 0x03005DC0   # struct BackupMapLayout { s32 width, height; u16 *map }
S_BACKUP_MAP_DATA = 0x02032318     # u16[MAX_MAP_DATA_SIZE = 10240]
BACKUP_MAP_DATA_SIZE = 0x5000
NUM_METATILES_IN_PRIMARY = 512
# Metatile layer types (include/global.fieldmap.h, DrawMetatile in src/field_camera.c):
# which BG each half of a metatile goes to. BG1 is drawn above characters, BG2/BG3 below.
LAYER_NORMAL, LAYER_COVERED, LAYER_SPLIT = 0, 1, 2
# Impassable but flat: water (MB_POND_WATER..MB_SHALLOW_WATER) and ledges (MB_JUMP_*).
FLAT_BEHAVIORS = frozenset(range(0x10, 0x18)) | frozenset(range(0x38, 0x40))

# Graphics ids with a kind of their own (include/constants/event_objects.h); other
# characters are kind "npc" and carry their graphics id in props.
KIND_BY_GRAPHICS_ID = {215: "mom"}           # OBJ_EVENT_GFX_MOM

# Prop kit: models in place of furniture drawn into the tiles (shadow/props.py). Keyed by
# tileset (ROM address) + metatile ids; anchors and sizes measured from the original art.
TILESET_BRENDANS_MAYS_HOUSE = 0x083DFAF4     # gTileset_BrendansMaysHouse (secondary)
PROP_KIT = (
    # Brendan's house 1F: the 2x2 dining table on the rug (28x24 px, top 8 px up)...
    PropDef("prop_table", TILESET_BRENDANS_MAYS_HOUSE, ((0x21A, 0x21B), (0x222, 0x223)), (16, 20)),
    # ...and its green chairs: the left ones face east, the right ones west.
    *(PropDef("prop_chair", TILESET_BRENDANS_MAYS_HOUSE, ((m,),), (8, 13), 0) for m in (0x219, 0x221)),
    *(PropDef("prop_chair", TILESET_BRENDANS_MAYS_HOUSE, ((m,),), (8, 13), 180) for m in (0x21C, 0x224)),
    # May's house 1F (mirrored layout, same chairs; its table has another cloth colour).
    PropDef("prop_chair", TILESET_BRENDANS_MAYS_HOUSE, ((0x2C0,),), (8, 13), 0),
    PropDef("prop_chair", TILESET_BRENDANS_MAYS_HOUSE, ((0x2C3,),), (8, 13), 180),
)

AVATAR_FLAGS = ["on_foot", "mach_bike", "acro_bike", "surfing", "underwater",
                "controllable", "forced_move", "dash"]


def _s16(v: int) -> int:
    return ((v + 0x8000) & 0xFFFF) - 0x8000


class PokemonEmeraldAdapter:
    name = "pokeemerald"
    game_codes = ("BPEE",)
    ram_ranges = [
        (G_SPRITES, SPRITE_SIZE * N_SPRITES),
        (G_SPRITE_COORD_OFFSET_X, 4),
        (G_SAVEBLOCK1, 0x88),
        (G_OBJECT_EVENTS, G_PLAYER_AVATAR + 0x24 - G_OBJECT_EVENTS),   # object events + avatar
        (G_MAIN, 8),
        (G_SAVEBLOCK1_PTR, G_TOTAL_CAMERA_PIXEL_OFFSET_X + 2 - G_SAVEBLOCK1_PTR),   # incl. gBackupMapLayout
        (G_MAP_HEADER, 0x1C),
        (S_BACKUP_MAP_DATA, BACKUP_MAP_DATA_SIZE),
    ]
    ram_delay = 1
    # Overworld BGs (sOverworldBgTemplates): BG0 = text boxes / menus (priority 0),
    # BG1 = map top layer (1), BG2/BG3 = map middle/bottom layers (2/3).
    hud_bgs = frozenset({0})
    prop_kit = PROP_KIT

    def __init__(self):
        self._tileset_cache: dict = {}

    # -- map ---------------------------------------------------------------------------
    def tilemap(self, st: GBAState, rom) -> TileMap | None:
        """The current map as metatile cells (None outside the overworld or without the ROM)."""
        if rom is None or (st.u32(G_MAIN + 4) & ~1) != CB2_OVERWORLD:
            return None
        width, height = st.s32(G_BACKUP_MAP_LAYOUT), st.s32(G_BACKUP_MAP_LAYOUT + 4)
        ptr = st.u32(G_BACKUP_MAP_LAYOUT + 8)
        if width <= 0 or height <= 0 or width * height * 2 > BACKUP_MAP_DATA_SIZE:
            return None
        try:
            grid = np.frombuffer(st.ram(ptr, width * height * 2).tobytes(), "<u2").reshape(height, width)
        except MemoryNotWatched:
            return None
        layout = st.u32(G_MAP_HEADER)
        try:
            primary, secondary = rom.u32(layout + 0x10), rom.u32(layout + 0x14)
            # Cells nothing was loaded into are MAPGRID_UNDEFINED; the game shows the
            # layout's 2x2 border pattern there instead, impassable (GetBorderBlockAt).
            undefined = (grid & 0x3FF) == 0x3FF
            if undefined.any():   # (kept as a mask below)
                border = np.array(rom.u16_array(rom.u32(layout + 0x08), 4), np.uint16) | 0x0C00
                ys, xs = np.mgrid[0:height, 0:width]
                grid = np.where(undefined, border[((xs + 1) & 1) + (((ys + 1) & 1) << 1)], grid)
            ids = grid & 0x3FF
            metatiles, flat = {}, set()
            for mid in np.unique(ids).tolist():
                mt, behavior = self._metatile(rom, primary, secondary, mid)
                if mt is not None:
                    metatiles[mid] = mt
                    if behavior in FLAT_BEHAVIORS:
                        flat.add(mid)
        except ValueError:
            return None                                # not this game's ROM, or a bad pointer
        collision = (grid >> 10) & 3
        # The border around the map is scenery past the edge, not walls: keep it flat.
        raised = (collision != 0) & ~np.isin(ids, list(flat)) & ~undefined
        key = (layout, width, height, hashlib.blake2b(grid.tobytes(), digest_size=8).hexdigest())
        origin = (-MAP_OFFSET * 16, -MAP_OFFSET * 16)  # cell (0, 0) is map tile (-7, -7)
        return TileMap(key, ids.astype(np.uint16), raised, origin, metatiles, name=f"layout {layout:08X}",
                       tilesets=(primary, secondary))

    def _metatile(self, rom, primary: int, secondary: int, mid: int):
        cache_key = (primary, secondary, mid)
        if cache_key not in self._tileset_cache:
            tileset, i = (primary, mid) if mid < NUM_METATILES_IN_PRIMARY else (secondary, mid - NUM_METATILES_IN_PRIMARY)
            table, attrs = rom.u32(tileset + 0x0C), rom.u32(tileset + 0x10)
            e = rom.u16_array(table + i * 16, 8)
            attr = rom.u16(attrs + i * 2)
            layer = attr >> 12
            if layer == LAYER_COVERED:                   # both halves below characters
                mt = Metatile(under=e[:4], over=None, under_extra=e[4:])
            else:                                        # NORMAL / SPLIT: top half on BG1
                mt = Metatile(under=e[:4], over=e[4:])
            self._tileset_cache[cache_key] = (mt, attr & 0xFF)
        return self._tileset_cache[cache_key]

    def extract(self, st: GBAState):
        """The player first (renderers layer relative to the first entity), then the
        other active object events whose sprite is on screen."""
        if (st.u32(G_MAIN + 4) & ~1) != CB2_OVERWORLD:
            return [], None                         # battle, menu screen, title...

        av = G_PLAYER_AVATAR
        player_oe = st.u8(av + 5)
        player = self._object_event(st, player_oe, sprite_id=st.u8(av + 4)) if player_oe < 16 else None
        if player is None:
            return [], None
        entities = [player]
        for i in range(16):
            if i != player_oe and (e := self._object_event(st, i)) is not None:
                entities.append(e)

        x, y, off_x, off_y, ctc_y = self._sprite_coords(st, player.props["sprite_id"])
        map_id = player.props["map"]
        camera = {
            # World pixel shown at the top-left of the screen, and the map it is on.
            "origin": (player.pos[0] - (x + off_x), player.pos[1] - (y + off_y - ctc_y)),
            "map": map_id,
        }
        return entities, camera

    @staticmethod
    def _sprite_coords(st: GBAState, sprite_id: int):
        spr = G_SPRITES + sprite_id * SPRITE_SIZE
        return (st.s16(spr + 0x20), st.s16(spr + 0x22),
                st.s16(G_SPRITE_COORD_OFFSET_X), st.s16(G_SPRITE_COORD_OFFSET_Y), st.s8(spr + 0x29))

    def _object_event(self, st: GBAState, index: int, sprite_id: int | None = None) -> Entity | None:
        """One object event as an Entity. `sprite_id` is given for the player (from
        gPlayerAvatar), which also marks it as the player."""
        player = sprite_id is not None
        oe = G_OBJECT_EVENTS + index * OBJECT_EVENT_SIZE
        if not st.u8(oe) & 1:                       # ObjectEvent.active
            return None
        if sprite_id is None:
            sprite_id = st.u8(oe + 4)
        if sprite_id >= N_SPRITES:
            return None
        spr = G_SPRITES + sprite_id * SPRITE_SIZE
        sprite_flags = st.u8(spr + 0x3E)
        if not sprite_flags & 1:                    # Sprite.inUse
            return None
        oe_flags = st.u8(oe + 1)                    # frozen, ..., invisible (bit 5), offScreen (bit 6)
        if not player and oe_flags & 0x40:
            return None

        # Sprite position (see UpdateOamCoords in src/sprite.c).
        x, y, off_x, off_y, ctc_y = self._sprite_coords(st, sprite_id)
        x2, y2 = st.s16(spr + 0x24), st.s16(spr + 0x26)
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
        graphics_id = st.u8(oe + 5)
        local_id = st.u8(oe + 8)
        oe_map = (st.u8(oe + 0x0A), st.u8(oe + 9))  # (mapGroup, mapNum) the event belongs to

        # Hardware sprites drawn from this sprite's tiles + palette (covers the
        # two-piece split the game uses when a character stands in tall grass).
        a0, a1, a2 = st.u16(spr), st.u16(spr + 2), st.u16(spr + 4)
        w, h = oam_size(a0 >> 14, a1 >> 14)
        n_tiles = (w * h) // 64 * (2 if a0 & 0x2000 else 1)
        oam = oam_slots_for_tiles(st, a2 & 0x3FF, n_tiles, a2 >> 12) if not sprite_flags & 4 else []

        props = {
            "prev_tile": prev,
            "movement_direction": FACING_NAMES.get((dirs >> 4) & 0xF),
            "elevation": st.u8(oe + 0x0B) & 0xF,
            "graphics_id": graphics_id,
            "local_id": local_id,
            "object_event": index,
            "sprite_id": sprite_id,
            "jump_offset": (x2, y2),                 # y2 < 0 while hopping a ledge
            "anim": (st.u8(spr + 0x2A), st.u8(spr + 0x2B)),
            "invisible": bool(sprite_flags & 4) or bool(oe_flags & 0x20),
            "map": map_id,
        }
        moving = cur != prev
        if player:
            av = G_PLAYER_AVATAR
            flags = st.u8(av + 0)
            moving = st.u8(av + 3) != 0 or moving         # tileTransitionState
            props.update({
                "avatar": [name for i, name in enumerate(AVATAR_FLAGS) if flags & (1 << i)],
                "running_state": st.u8(av + 2),
                "tile_transition_state": st.u8(av + 3),
                "gender": "female" if st.u8(av + 7) else "male",
            })
            ident, kind = "player", "player"
        else:
            kind = KIND_BY_GRAPHICS_ID.get(graphics_id, "npc")
            # Local ids are unique within a map; the map makes them unique while
            # the objects of a neighbouring map are loaded too.
            ident = f"{kind}:{oe_map[0]}.{oe_map[1]}.{local_id}"
            props["event_map"] = oe_map
        return Entity(
            id=ident,
            kind=kind,
            screen=screen,
            pos=pos,
            tile=cur,
            facing=FACING_NAMES.get(dirs & 0xF),
            moving=moving,
            size=(w, h),
            oam=oam,
            props=props,
        )


ADAPTER = PokemonEmeraldAdapter()
