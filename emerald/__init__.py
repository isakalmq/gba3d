"""Pokémon Emerald (US) semantic bridge: snapshots up, intents down.

The GBA is the only source of truth. Each frame the host reads a `Snapshot` of what
is true (map, player, NPCs, control mode, text, menu) and a `Controller` turns
high-level intents (go to a tile, talk to an NPC, answer a menu) into button presses.
"""
