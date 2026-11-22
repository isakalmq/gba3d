"""A live connection to the mGBA bridge plus the game adapter it selected."""
from __future__ import annotations

from .entities import Adapter, World, extract_world
from .games import find_adapter
from .protocol import DEFAULT_PORT, FrameReceiver
from .rom import Rom, find_rom
from .state import GBAState


class LiveSession:
    """Receives frames from the bridge. When the bridge says hello, picks the
    adapter for that game code and asks the bridge for the adapter's RAM ranges."""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT, adapters: bool = True,
                 rom: str | None = None):
        self.use_adapters = adapters
        self.adapter: Adapter | None = None
        self.rom_path = rom
        self.rom: Rom | None = None
        self.receiver = FrameReceiver(host, port, on_hello=self._on_hello)

    def _on_hello(self, code: str, title: str):
        # The ROM file gives adapters static data (e.g. Pokemon's tilesets for 3D maps).
        try:
            self.rom = Rom.open(self.rom_path) if self.rom_path else find_rom(code)
        except (OSError, ValueError) as e:
            print(f"[shadow] can't read ROM file {self.rom_path}: {e}")
            self.rom = None
        if self.rom is not None and self.rom.game_code != code:
            print(f"[shadow] {self.rom.path} is {self.rom.game_code!r}, not {code!r}; ignoring it")
            self.rom = None
        print(f"[shadow] ROM file: {self.rom.path if self.rom else 'not found (3D maps need it in roms/)'}")
        self.adapter = find_adapter(code) if self.use_adapters else None
        if self.adapter is None:
            print(f"[shadow] no entity adapter for {code!r} - rendering only")
            return None
        print(f"[shadow] using adapter {self.adapter.name!r}")
        return self.adapter.ram_ranges, self.adapter.ram_delay

    def start(self) -> "LiveSession":
        self.receiver.start()
        return self

    def stop(self) -> None:
        self.receiver.stop()

    @property
    def port(self) -> int:
        return self.receiver.port

    @property
    def connected(self) -> bool:
        return self.receiver.connected

    @property
    def game_code(self) -> str | None:
        return self.receiver.game_code

    def latest(self) -> tuple[GBAState | None, int]:
        return self.receiver.latest()

    def world(self, st: GBAState) -> World:
        return extract_world(self.adapter, st, self.game_code)
