"""Best-effort: press through the title screen and intro until the player is in the
overworld with control. Used to make a savestate for testing.

This only presses buttons and watches the snapshot (A to advance, Start on screens
that need it, e.g. to finish the naming screen), so the game's own logic does the rest.
It has not yet been run against a real ROM; if it stalls, play to the overworld in
`python -m emerald view ROM` and press F5 instead.
"""
from __future__ import annotations

from shadow.core import KEY_A, KEY_START

from .snapshot import Mode


def boot_to_overworld(host, max_frames: int, log=lambda *a: None) -> bool:
    stable = 0
    last_screen = None
    for f in range(max_frames):
        s = host.snapshot
        if s.screen != last_screen:
            log(f"[boot] frame {s.frame}: {s.mode.value} {s.screen} {s.map_name}")
            last_screen = s.screen
        if s.mode == Mode.OVERWORLD and s.player is not None:
            stable += 1
            if stable > 90:
                return True
            host.step(0)
            continue
        stable = 0
        phase = f % 240
        if phase < 200:
            keys = KEY_A if f % 10 == 0 else 0
        else:
            keys = KEY_START if f % 20 == 0 else 0
        host.step(keys)
    return False
