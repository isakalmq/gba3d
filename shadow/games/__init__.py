"""Game adapters, looked up by the 4-letter game code in the ROM header."""
from __future__ import annotations

from ..entities import Adapter
from . import pokeemerald, shadowtest

ADAPTERS: list[Adapter] = [pokeemerald.ADAPTER, shadowtest.ADAPTER]


def find_adapter(game_code: str | None) -> Adapter | None:
    for adapter in ADAPTERS:
        if game_code in adapter.game_codes:
            return adapter
    return None
