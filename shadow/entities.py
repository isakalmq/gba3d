"""Entity model: what game adapters produce and renderers consume.

A game adapter (see shadow/games/) knows where one game keeps its objects in
work RAM. It tells the bridge which RAM ranges to stream, and turns a GBAState
into a list of `Entity` objects each frame. Renderers only see this model, never
raw memory.

Coordinates:
  * `screen`: an anchor point on the GBA screen in pixels (for characters: the
    middle of the feet). Use it to draw things over the 2D game.
  * `pos`: the same anchor in the game's own world space, in pixels (for
    Pokemon: map pixels). Use it to place things in a 3D scene.
  * `tile`: the logical tile the game considers the entity to be on, if any.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Protocol

from .render import OBJ_SIZES
from .state import GBAState, MemoryNotWatched

FACING_NAMES = {1: "down", 2: "up", 3: "left", 4: "right"}


@dataclass
class Entity:
    id: str                                   # stable while the entity exists, e.g. "player"
    kind: str                                 # e.g. "player"
    screen: tuple[float, float] | None = None
    pos: tuple[float, float] | None = None
    tile: tuple[int, int] | None = None
    facing: str | None = None                 # "down" | "up" | "left" | "right"
    moving: bool = False
    size: tuple[int, int] | None = None       # on-screen sprite size in GBA pixels
    oam: list[int] = field(default_factory=list)   # OAM slots that draw this entity
    props: dict = field(default_factory=dict)      # anything else the adapter knows

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class World:
    frame: int
    game: str | None
    entities: list[Entity] = field(default_factory=list)
    camera: dict | None = None                # adapter-specific camera info (world pixels)
    error: str | None = None                  # why extraction failed this frame, if it did

    @property
    def entity_oam(self) -> set[int]:
        return {i for e in self.entities for i in e.oam}

    def to_dict(self) -> dict:
        return {
            "frame": self.frame,
            "game": self.game,
            "camera": self.camera,
            "entities": [e.to_dict() for e in self.entities],
            "error": self.error,
        }


class Adapter(Protocol):
    name: str
    game_codes: tuple[str, ...]
    ram_ranges: list[tuple[int, int]]     # (bus address, length) to stream
    ram_delay: int                        # frames RAM runs ahead of the picture (0 or 1)

    def extract(self, st: GBAState) -> tuple[list[Entity], dict | None]:
        """Entities and camera for this frame. May raise MemoryNotWatched."""
        ...


def extract_world(adapter: Adapter | None, st: GBAState, game: str | None = None) -> World:
    if adapter is None:
        return World(st.frame, game)
    try:
        entities, camera = adapter.extract(st)
        return World(st.frame, game, entities, camera)
    except MemoryNotWatched as e:
        return World(st.frame, game, error=f"waiting for RAM: {e}")


def oam_slots_for_tiles(st: GBAState, tile_start: int, n_tiles: int, palette: int | None) -> list[int]:
    """Visible OAM slots whose graphics come from tiles [tile_start, tile_start + n_tiles)
    (and use `palette`, if given). A simple way to find the hardware sprites that
    belong to one logical sprite, including when the game splits it into pieces."""
    oam = st.oam16
    out = []
    for i in range(128):
        a0, a2 = int(oam[i * 4]), int(oam[i * 4 + 2])
        if (a0 & 0x300) == 0x200:          # disabled
            continue
        tile = a2 & 0x3FF
        if tile_start <= tile < tile_start + n_tiles and (palette is None or (a2 >> 12) == palette):
            out.append(i)
    return out


def oam_size(shape: int, size: int) -> tuple[int, int]:
    if shape > 2:
        return (0, 0)
    return OBJ_SIZES[shape][size]
