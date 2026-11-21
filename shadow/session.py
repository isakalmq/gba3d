"""A live connection to the mGBA bridge plus the game adapter it selected."""
from __future__ import annotations

from .entities import Adapter, World, extract_world
from .games import find_adapter
from .protocol import DEFAULT_PORT, FrameReceiver
from .state import GBAState


class LiveSession:
    """Receives frames from the bridge. When the bridge says hello, picks the
    adapter for that game code and asks the bridge for the adapter's RAM ranges."""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT, adapters: bool = True):
        self.use_adapters = adapters
        self.adapter: Adapter | None = None
        self.receiver = FrameReceiver(host, port, on_hello=self._on_hello)

    def _on_hello(self, code: str, title: str):
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
