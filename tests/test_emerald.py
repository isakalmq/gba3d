"""Emerald bridge tests against tests/fake_emerald.py (no ROM needed).

The fake uses the real symbol addresses and struct layouts, so these tests cover the
snapshot reader, the grid/pathfinder, the controller's closed loop and the event
diff. Behaviour that depends on the real game is covered by test_emerald_rom.py,
which runs only when EMERALD_ROM is set.
"""
from __future__ import annotations

from emerald import data as D
from emerald.controller import Status, advance_text, choose, face, goto, interact, press
from emerald.host import EmeraldHost
from emerald.snapshot import Mode
from emerald.text import decode, encode_simple
from emerald.fake import FakeEmerald, FakeMapSpec, FakeNPC
from shadow.core import KEY_START

ROWS = [
    "..........",
    "..####....",
    "..#..#....",
    "..........",
    "vvvvvv....",
    "..........",
    "....~~....",
    "..........",
]


def make(rows=ROWS, start=(0, 0), **kw):
    fake = FakeEmerald(FakeMapSpec(rows, **kw), start)
    return fake, EmeraldHost(None, core=fake)


# -- data / text ---------------------------------------------------------------
def test_data_has_expected_symbols():
    d = D.load()
    assert d.sym["gObjectEvents"] == 0x02037350
    assert d.sym["gSaveBlock1Ptr"] == 0x03005D8C
    assert d.maps[(0, 9)] == "LITTLEROOT_TOWN"
    assert d.mb["MB_JUMP_SOUTH"] in d.mb.values()


def test_text_roundtrip_and_control_codes():
    d = D.load()
    raw = encode_simple("Hi PROF.", d)[:-1] + bytes([0xFC, 0x01, 0x02, 0xFB]) + encode_simple("Bye", d)
    assert decode(raw, d.charmap) == "Hi PROF.\n\nBye"
    assert decode(raw, d.charmap, stop=2) == "Hi"


# -- snapshot --------------------------------------------------------------------
def test_snapshot_reads_player_npcs_and_map():
    fake, host = make(npcs=[FakeNPC(3, 8, 1, gfx=7)])
    s = host.snapshot
    assert s.mode == Mode.OVERWORLD
    assert s.map_name == "LITTLEROOT_TOWN"
    assert (s.player.x, s.player.y, s.player.facing) == (0, 0, "south")
    assert s.player.has_running_shoes
    npc = s.object_by_local_id(3)
    assert (npc.x, npc.y, npc.gfx_id) == (8, 1, 7)
    assert host.grid.map_bounds() == (10, 8)


# -- grid --------------------------------------------------------------------------
def test_ledges_are_one_way():
    fake, host = make(start=(1, 7))
    g = host.grid
    assert g.find_path((1, 7), 3, (1, 0)) is not None          # around the ledge row via x>=6
    down = g.find_path((1, 0), 3, (1, 7))
    assert any(s.jump for s in down)
    up = g.find_path((1, 7), 3, (1, 0))
    assert all(s.x >= 6 for s in up if s.y == 4)                # never climbs a ledge


def test_water_needs_surf_and_approximate_target_snaps():
    fake, host = make()
    g = host.grid
    assert g.find_path((0, 0), 3, (4, 6)) is None               # water tile
    assert g.nearest_reachable((0, 0), 3, (3, 2)) == (3, 2)     # inside the hut, reachable via the gap
    snapped = g.nearest_reachable((0, 0), 3, (4, 6))
    assert snapped in {(4, 5), (4, 7), (3, 6)}


# -- controller ------------------------------------------------------------------------
def test_goto_walks_and_runs():
    fake, host = make()
    it = host.run_intent(goto(9, 7))
    assert it.status == Status.DONE and fake.player_pos() == (9, 7)
    walked_frames = fake.frame
    fake2, host2 = make()
    it = host2.run_intent(goto(9, 7, run=False))
    assert it.status == Status.DONE
    assert fake2.frame > walked_frames * 1.5                   # running really is faster


def test_goto_approximate_location():
    fake, host = make()
    it = host.run_intent(goto(4, 6))                            # water: snapped to the shore
    assert it.status == Status.DONE
    assert it.args["snapped_to"] == fake.player_pos()
    fake, host = make()
    it = host.run_intent(goto(4, 6, exact=True))
    assert it.status == Status.ABORTED and it.reason == "unreachable"


def test_goto_replans_around_npc_that_steps_in():
    fake, host = make(npcs=[FakeNPC(5, 9, 0)])
    it = host.submit(goto(9, 3))
    for _ in range(20):
        host.step()
    fake.move_npc(5, 9, 2)                                      # block the obvious route
    for _ in range(2000):
        host.step()
        if it.finished:
            break
    assert it.status == Status.DONE and fake.player_pos() == (9, 3)


def test_trigger_aborts_goto_with_script_reason():
    fake, host = make(trigger={(5, 0): "Trainer spotted you!"})
    it = host.run_intent(goto(9, 0))
    assert it.status == Status.ABORTED and it.reason == "script"
    assert host.snapshot.text.full == "Trainer spotted you!"


def test_encounter_aborts_with_screen_reason_then_resumes():
    rows = [r for r in ROWS]
    rows[0] = "...E......"
    fake, host = make(rows=rows)
    it = host.run_intent(goto(9, 0))
    assert it.status == Status.ABORTED and it.reason.startswith("screen:BattleMainCB2")
    r = host.run_until(lambda r: r.snapshot.mode == Mode.OVERWORLD, 200)
    assert r is not None
    assert host.run_intent(goto(9, 0)).status == Status.DONE


def test_interact_dialogue_and_yes_no():
    fake, host = make(npcs=[FakeNPC(1, 8, 1, text="Want a potion?", yesno=True)])
    it = host.run_intent(interact(local_id=1))
    assert it.status == Status.DONE and fake.talked == [1]
    assert host.snapshot.mode == Mode.SCRIPT
    assert host.snapshot.text.full == "Want a potion?"
    assert host.run_intent(advance_text(until_closed=True)).reason == "menu"
    assert host.snapshot.menu.kind == "yesno"
    assert host.run_intent(choose(1)).status == Status.DONE
    assert fake.menu_result == 1 and host.snapshot.mode == Mode.OVERWORLD


def test_interact_across_water_and_missing_npc():
    fake, host = make(npcs=[FakeNPC(2, 4, 6)])                  # standing in the water
    it = host.run_intent(interact(local_id=2))
    assert it.status == Status.DONE                             # talked to from the shore
    assert fake.talked == [2]
    fake, host = make(npcs=[FakeNPC(2, 0, 0)], start=(9, 7))
    it = host.run_intent(interact(local_id=99))
    assert it.status == Status.ABORTED and it.reason == "no_such_object"


def test_face_and_intents_rejected_in_wrong_mode():
    fake, host = make(npcs=[FakeNPC(1, 1, 0, text="Hi")])
    assert host.run_intent(face("east")).status == Status.DONE
    assert host.snapshot.player.facing == "east"
    assert fake.player_pos() == (0, 0)                          # turned, didn't walk
    it = host.run_intent(advance_text())
    assert it.status == Status.ABORTED and it.reason == "overworld"
    host.run_intent(interact(local_id=1))
    it = host.run_intent(goto(5, 5))
    assert it.status == Status.ABORTED and it.reason == "script"


def test_map_connection_translates_goal():
    north = FakeMapSpec(["." * 10] * 9, group=0, num=16)        # ROUTE101
    fake, host = make(start=(2, 1), north=north)
    it = host.run_intent(goto(2, -3))                           # 3 tiles into the next map
    assert it.status == Status.DONE, it.reason
    assert host.snapshot.map_name == "ROUTE101"
    assert fake.player_pos() == (2, 6)


def test_events_and_replacement():
    fake, host = make(npcs=[FakeNPC(1, 8, 1, text="Yo")])
    first = host.submit(goto(9, 7))
    host.step()
    host.submit(goto(0, 3))
    r = host.step()
    assert first.status == Status.ABORTED and first.reason == "replaced"
    kinds = []
    host.submit(interact(local_id=1))
    for _ in range(600):
        r = host.step()
        kinds += [e.kind for e in r.events]
        if host.snapshot.mode == Mode.SCRIPT:
            break
    assert "player_step" in kinds and "text_opened" in kinds and "mode_changed" in kinds


def test_raw_input_cancels_walk():
    fake, host = make()
    it = host.submit(goto(9, 7))
    host.step()
    host.step(raw_keys=KEY_START)
    assert it.status == Status.ABORTED and it.reason == "user_input"
    host.raw_keys = 0
    assert host.run_intent(press(KEY_START, 2)).status == Status.DONE
