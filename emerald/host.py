"""The frame-locked host loop: intents -> keys -> one emulated frame -> snapshot -> events.

    host = EmeraldHost("emerald.gba", save="emerald.sav")
    host.submit(goto(10, 8))
    while True:
        result = host.step()            # exactly one GBA frame
        draw(result.snapshot, result.events)

There are no sockets, threads or dropped frames: the client calls step() and the
emulator advances by exactly one frame. Holding the game (e.g. to let a 3D door
animation finish) is simply not calling step().
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from shadow.core import Core

from .controller import Controller, Intent
from .data import EmeraldData, load
from .events import Event, diff
from .mapgrid import GridReader, MapGrid
from .snapshot import Mode, Snapshot, read_snapshot

GAME_CODE_ADDR = 0x080000AC


@dataclass
class FrameResult:
    snapshot: Snapshot
    events: list[Event]
    finished: list[Intent] = field(default_factory=list)   # intents that ended this frame
    current: Intent | None = None
    keys: int = 0


class EmeraldHost:
    def __init__(self, rom: str | os.PathLike, save: str | os.PathLike | None = None,
                 data: EmeraldData | None = None, core=None, check_rom: bool = True):
        self.data = data or load()
        self.core = core if core is not None else Core(rom, save)
        if check_rom:
            code = self.core.read(GAME_CODE_ADDR, 4)
            if code != self.data.raw["game_code"].encode():
                raise ValueError(f"ROM game code is {code!r}; this needs Pokémon Emerald (US), "
                                 f"game code {self.data.raw['game_code']}")
        self.controller = Controller()
        self.grids = GridReader(self.data)
        self.snapshot: Snapshot | None = None
        self.grid: MapGrid | None = None
        self.raw_keys = 0   # passthrough from the client (DIRECT/SCREEN modes, manual play)
        self._refresh()

    def _refresh(self) -> None:
        self.snapshot = read_snapshot(self.core, self.data, self.core.frame)
        if self.snapshot.mode in (Mode.OVERWORLD, Mode.SCRIPT, Mode.DIRECT):
            g = self.grids.read(self.core)
            if g is not None:
                self.grid = g

    def submit(self, intent: Intent, replace: bool = True) -> Intent:
        return self.controller.submit(intent, replace)

    def cancel(self, reason: str = "cancelled") -> None:
        self.controller.cancel(reason)

    def step(self, raw_keys: int | None = None) -> FrameResult:
        if raw_keys is not None:
            self.raw_keys = raw_keys
        prev = self.snapshot
        # Manual d-pad input in the overworld overrides a running walk.
        if self.raw_keys and self.controller.current is not None and prev is not None and prev.mode == Mode.OVERWORLD:
            self.controller.cancel("user_input")
        keys = self.controller.step(prev, self.grid) | self.raw_keys
        self.core.set_keys(keys)
        self.core.run_frame()
        self._refresh()
        events = diff(prev, self.snapshot)
        return FrameResult(self.snapshot, events, self.controller.drain_finished(), self.controller.current, keys)

    def run_until(self, predicate, max_frames: int = 600, raw_keys: int | None = None) -> FrameResult | None:
        """Step until predicate(FrameResult) is true. Returns that result or None on timeout."""
        for _ in range(max_frames):
            r = self.step(raw_keys)
            if predicate(r):
                return r
        return None

    def run_intent(self, intent: Intent, max_frames: int = 3000) -> Intent:
        """Submit and step until the intent finishes (blocking helper for scripts/tests)."""
        self.submit(intent)
        for _ in range(max_frames):
            self.step()
            if intent.finished:
                return intent
        self.cancel("timeout")
        return intent
