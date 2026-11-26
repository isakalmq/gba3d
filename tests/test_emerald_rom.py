"""Checks against a real Pokémon Emerald (US) ROM. Skipped unless EMERALD_ROM is set.

    EMERALD_ROM=path/to/emerald.gba EMERALD_STATE=path/to/overworld.ss1 pytest tests/test_emerald_rom.py

EMERALD_STATE is a savestate in the overworld with the player in control (make one
with `python -m emerald boot ROM -o STATE` or F5 in the debug view). Without it, only
the boot checks run.
"""
import os
from pathlib import Path

import pytest

ROM = os.environ.get("EMERALD_ROM")
STATE = os.environ.get("EMERALD_STATE")
LIB = Path(__file__).resolve().parent.parent / "host" / "libgbahost.so"

pytestmark = [
    pytest.mark.skipif(not ROM, reason="set EMERALD_ROM to a Pokémon Emerald (US) ROM"),
    pytest.mark.skipif(not LIB.exists(), reason="build host/libgbahost.so with `make -C host`"),
]


@pytest.fixture
def host():
    from emerald.host import EmeraldHost
    h = EmeraldHost(ROM)
    yield h
    h.core.close()


@pytest.fixture
def overworld(host):
    if not STATE:
        pytest.skip("set EMERALD_STATE to an overworld savestate")
    host.core.load_state(Path(STATE).read_bytes())
    host.step()
    from emerald.snapshot import Mode
    assert host.snapshot.mode == Mode.OVERWORLD, host.snapshot
    return host


def test_boot_reaches_title_screen(host):
    from emerald.snapshot import Mode
    seen = set()
    for _ in range(60 * 30):
        r = host.step()
        seen.add(r.snapshot.screen)
    assert host.snapshot.mode == Mode.SCREEN
    assert "CB2_InitTitleScreen" in seen or any("Title" in s or s == "MainCB2" for s in seen), seen


def test_overworld_snapshot_is_consistent(overworld):
    s = overworld.snapshot
    p = s.player
    assert p is not None and s.map_name != f"MAP_{s.map_group}_{s.map_num}"
    sb1 = overworld.core.u32(overworld.data.sym["gSaveBlock1Ptr"])
    assert (overworld.core.s16(sb1), overworld.core.s16(sb1 + 2)) == (p.x, p.y)
    g = overworld.grid
    assert g is not None
    w, h = g.map_bounds()
    assert 0 <= p.x < w and 0 <= p.y < h
    assert g.collision(p.x + 7, p.y + 7) == 0


def test_walk_to_reachable_tile(overworld):
    from emerald.controller import Status, goto
    s, g = overworld.snapshot, overworld.grid
    p = s.player
    tiles = sorted(g.reachable((p.x, p.y), p.elevation, p.surfing),
                   key=lambda t: -abs(t[0] - p.x) - abs(t[1] - p.y))
    w, h = g.map_bounds()
    target = next(t for t in tiles if 0 <= t[0] < w and 0 <= t[1] < h and abs(t[0] - p.x) + abs(t[1] - p.y) <= 8)
    it = overworld.run_intent(goto(*target, exact=True), max_frames=1200)
    assert it.status == Status.DONE or it.reason in ("script", "screen:BattleMainCB2"), it.to_dict()
    if it.status == Status.DONE:
        assert (overworld.snapshot.player.x, overworld.snapshot.player.y) == target
