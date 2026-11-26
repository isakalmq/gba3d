"""Events derived by comparing two snapshots. Nothing is hooked in the game.

Because events are derived from consecutive full snapshots, they can't be lost or
arrive out of order: a client that drops frames can always resync from the latest
snapshot alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .snapshot import Snapshot


@dataclass(frozen=True)
class Event:
    kind: str
    data: dict = field(default_factory=dict)

    def __str__(self) -> str:
        args = " ".join(f"{k}={v}" for k, v in self.data.items())
        return f"{self.kind} {args}".strip()


def diff(prev: Snapshot | None, cur: Snapshot) -> list[Event]:
    ev: list[Event] = []
    if prev is None:
        ev.append(Event("sync", {"map": cur.map_name, "mode": cur.mode.value}))
        return ev
    if (prev.map_group, prev.map_num) != (cur.map_group, cur.map_num):
        ev.append(Event("map_changed", {"from": prev.map_name, "to": cur.map_name}))
    if prev.mode != cur.mode:
        ev.append(Event("mode_changed", {"from": prev.mode.value, "to": cur.mode.value, "screen": cur.screen}))
    elif prev.screen != cur.screen:
        ev.append(Event("screen_changed", {"from": prev.screen, "to": cur.screen}))

    if prev.battle is None and cur.battle is not None:
        ev.append(Event("battle_started", {"type_flags": hex(cur.battle.type_flags)}))
    elif prev.battle is not None and cur.battle is None:
        ev.append(Event("battle_ended", {"outcome": prev.battle.outcome}))

    if prev.text is None and cur.text is not None:
        ev.append(Event("text_opened", {"text": cur.text.full}))
    elif prev.text is not None and cur.text is None:
        ev.append(Event("text_closed"))
    elif prev.text is not None and cur.text is not None and prev.text.full != cur.text.full:
        ev.append(Event("text_changed", {"text": cur.text.full}))

    if prev.menu is None and cur.menu is not None:
        ev.append(Event("menu_opened", {"kind": cur.menu.kind, "cursor": cur.menu.cursor,
                                        "options": cur.menu.max - cur.menu.min + 1}))
    elif prev.menu is not None and cur.menu is None:
        ev.append(Event("menu_closed", {"cursor": prev.menu.cursor}))
    elif prev.menu is not None and cur.menu is not None and prev.menu.cursor != cur.menu.cursor:
        ev.append(Event("menu_cursor", {"cursor": cur.menu.cursor}))

    if prev.player and cur.player and (prev.player.x, prev.player.y) != (cur.player.x, cur.player.y):
        ev.append(Event("player_step", {"x": cur.player.x, "y": cur.player.y, "facing": cur.player.facing}))

    before = {(o.slot, o.local_id, o.gfx_id) for o in prev.objects if not o.is_player}
    after = {(o.slot, o.local_id, o.gfx_id) for o in cur.objects if not o.is_player}
    for slot, local_id, gfx in sorted(after - before):
        ev.append(Event("object_spawned", {"slot": slot, "local_id": local_id, "gfx": gfx}))
    for slot, local_id, gfx in sorted(before - after):
        ev.append(Event("object_despawned", {"slot": slot, "local_id": local_id, "gfx": gfx}))
    return ev
