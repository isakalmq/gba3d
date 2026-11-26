"""Turns intents ("go here", "talk to NPC 3", "pick option 1") into button presses.

The client says *what*; the controller works out *how*, one frame at a time, next to
the emulator: it plans a path, presses the d-pad, checks the result every frame,
replans when blocked, and gives up cleanly when the game takes control away.

Every intent ends as DONE or ABORTED with a reason, e.g.:

    aborted: script        (a trainer spotted you, a step trigger fired)
    aborted: screen:battle (wild encounter)
    aborted: blocked       (path stayed blocked after replanning)
    aborted: unreachable   (no path to the target)
    aborted: replaced      (a newer intent took over)
"""
from __future__ import annotations

import enum
import itertools
from dataclasses import dataclass, field

from shadow.core import KEY_A, KEY_B, KEY_DOWN, KEY_LEFT, KEY_RIGHT, KEY_UP

from . import data as D
from .mapgrid import MapGrid, Step
from .snapshot import Mode, Snapshot

DIR_KEYS = {D.DIR_SOUTH: KEY_DOWN, D.DIR_NORTH: KEY_UP, D.DIR_WEST: KEY_LEFT, D.DIR_EAST: KEY_RIGHT}

STUCK_FRAMES = 40        # holding a direction this long without moving = blocked
MAX_REPLANS = 4
SCRIPT_START_TIMEOUT = 40  # frames to wait for a script after pressing A on something
MENU_STEP_TIMEOUT = 30


class Status(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ABORTED = "aborted"


_ids = itertools.count(1)


@dataclass
class Intent:
    kind: str
    args: dict = field(default_factory=dict)
    id: int = field(default_factory=lambda: next(_ids))
    status: Status = Status.QUEUED
    reason: str = ""
    # controller-private working state
    state: dict = field(default_factory=dict, repr=False)

    @property
    def finished(self) -> bool:
        return self.status in (Status.DONE, Status.ABORTED)

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "args": self.args, "status": self.status.value, "reason": self.reason}


# -- constructors (the whole downward vocabulary) ---------------------------
def goto(x: int, y: int, run: bool = True, exact: bool = False) -> Intent:
    """Walk to (x, y) in map coordinates. Unless `exact`, an unreachable target is
    snapped to the nearest reachable tile (an "approximate location")."""
    return Intent("goto", {"x": x, "y": y, "run": run, "exact": exact})


def face(direction: str | int) -> Intent:
    d = D.DIR_BY_NAME[direction] if isinstance(direction, str) else direction
    return Intent("face", {"direction": d})


def interact(local_id: int | None = None, x: int | None = None, y: int | None = None, run: bool = True) -> Intent:
    """Walk next to an NPC (by local id) or a tile (sign, PC, item ball...), face it, press A."""
    if local_id is None and (x is None or y is None):
        raise ValueError("interact needs local_id or x and y")
    return Intent("interact", {"local_id": local_id, "x": x, "y": y, "run": run})


def advance_text(until_closed: bool = False) -> Intent:
    """Press A once the text box is ready. With `until_closed`, keep going until the
    message box closes, a menu appears or the script ends (never answers menus)."""
    return Intent("advance_text", {"until_closed": until_closed})


def choose(index: int) -> Intent:
    """Move the yes/no or multichoice cursor to `index` and confirm. -1 cancels (B)."""
    return Intent("choose", {"index": index})


def press(buttons: int, frames: int = 1) -> Intent:
    """Raw button press: hold `buttons` for `frames`, then release for one frame."""
    return Intent("press", {"buttons": buttons, "frames": frames})


ALLOWED_MODES = {
    "goto": {Mode.OVERWORLD},
    "face": {Mode.OVERWORLD},
    "interact": {Mode.OVERWORLD},
    "advance_text": {Mode.SCRIPT},
    "choose": {Mode.SCRIPT},
    "press": set(Mode),
}


class Controller:
    def __init__(self):
        self.current: Intent | None = None
        self.queue: list[Intent] = []
        self.finished: list[Intent] = []   # drained by the host each frame
        self._tap_release = False          # force one released frame between taps

    # -- queue management -----------------------------------------------
    def submit(self, intent: Intent, replace: bool = True) -> Intent:
        if replace:
            self.cancel("replaced")
            self.queue.clear()
        self.queue.append(intent)
        return intent

    def cancel(self, reason: str = "cancelled") -> None:
        if self.current is not None and not self.current.finished:
            self._finish(self.current, Status.ABORTED, reason)

    def busy(self) -> bool:
        return self.current is not None or bool(self.queue)

    def _finish(self, intent: Intent, status: Status, reason: str = "") -> None:
        intent.status, intent.reason = status, reason
        self.finished.append(intent)
        if intent is self.current:
            self.current = None

    def drain_finished(self) -> list[Intent]:
        out, self.finished = self.finished, []
        return out

    # -- per frame --------------------------------------------------------
    def step(self, snap: Snapshot | None, grid: MapGrid | None) -> int:
        """Keys to hold for the next frame."""
        if snap is None:
            return 0
        if self.current is None and self.queue:
            self.current = self.queue.pop(0)
            self.current.status = Status.RUNNING
        it = self.current
        if it is None:
            return 0
        st = it.state
        st["frames"] = st.get("frames", 0) + 1

        # The game took control away (or never gave it): abort, unless that was the point.
        if snap.mode not in ALLOWED_MODES[it.kind]:
            outcome = self._on_mode_left(it, snap)
            if outcome is not None:
                self._finish(it, *outcome)
                return 0
        handler = getattr(self, f"_step_{it.kind}")
        return handler(it, snap, grid)

    def _on_mode_left(self, it: Intent, snap: Snapshot):
        st = it.state
        if it.kind == "interact" and st.get("pressed") and snap.mode in (Mode.SCRIPT, Mode.SCREEN, Mode.TRANSITION):
            return Status.DONE, f"started:{snap.mode.value}"
        if it.kind == "goto" and snap.mode == Mode.TRANSITION and st.get("final_step"):
            return Status.DONE, "warped"
        if it.kind in ("advance_text", "choose") and st.get("pressed"):
            return Status.DONE, f"closed:{snap.mode.value}"
        if snap.mode == Mode.TRANSITION and st.get("frames", 0) < 2 and it.kind != "goto":
            return None  # let it settle for a frame
        reason = snap.mode.value if snap.mode != Mode.SCREEN else f"screen:{snap.screen}"
        return Status.ABORTED, reason

    # -- taps ---------------------------------------------------------------
    def _tap(self, keys: int) -> int:
        """One frame pressed, then (next call) one frame released, so the game sees a new press."""
        if self._tap_release:
            self._tap_release = False
            return 0
        self._tap_release = True
        return keys

    # -- goto -------------------------------------------------------------
    def _blocked_tiles(self, snap: Snapshot, extra: set | None = None) -> set[tuple[int, int]]:
        tiles = set(extra or ())
        for o in snap.objects:
            if o.is_player:
                continue
            tiles.add((o.x, o.y))
            if o.moving:
                tiles.add((o.prev_x, o.prev_y))
        return tiles

    def _plan(self, it: Intent, snap: Snapshot, grid: MapGrid, goal: tuple[int, int]) -> bool:
        p = snap.player
        st = it.state
        blocked = self._blocked_tiles(snap, st.get("temp_blocked"))
        path = grid.find_path((p.x, p.y), p.elevation, goal, p.surfing, blocked)
        if path is None:
            # NPCs may be standing in a corridor: plan through them and let replanning sort it out
            path = grid.find_path((p.x, p.y), p.elevation, goal, p.surfing, st.get("temp_blocked"))
        st["path"] = path
        st["stuck"] = 0
        return path is not None

    def _walk(self, it: Intent, snap: Snapshot, grid: MapGrid, goal: tuple[int, int]) -> int | str:
        """Advance along a path to `goal`. Returns keys, or "arrived" / "failed:<reason>"."""
        st = it.state
        p = snap.player
        pos = (p.x, p.y)

        # Crossed into a connected map: the game shifted coordinates. Shift the goal too.
        map_id = (snap.map_group, snap.map_num)
        if st.get("map") is not None and st["map"] != map_id:
            exp = st.get("expect")
            if exp is not None:
                dx, dy = pos[0] - exp[0], pos[1] - exp[1]
                goal = (goal[0] + dx, goal[1] + dy)
                st["goal"] = goal
            st["path"] = None
        st["map"] = map_id

        path: list[Step] | None = st.get("path")
        # The game moves currentCoords to the destination when a step *starts*, so a
        # position change means "a step began". Consume the path up to the new tile,
        # or replan if we ended up somewhere unexpected (forced movement, shoves).
        if st.get("last_pos") != pos:
            if path is not None:
                tiles = [(s.x, s.y) for s in path]
                if pos in tiles:
                    del path[:tiles.index(pos) + 1]
                elif st.get("last_pos") is not None:
                    path = st["path"] = None
            st["stuck"] = 0
            st["last_pos"] = pos
        if pos == goal:
            return "arrived" if not p.moving else 0
        if path is None:
            if not self._plan(it, snap, grid, goal):
                return "failed:unreachable"
            path = st["path"]
        if not path:
            return self._replan_or_fail(it, snap, grid, goal, "lost")

        nxt = path[0]
        blocked_now = self._blocked_tiles(snap)
        if (nxt.x, nxt.y) in blocked_now and not p.moving:
            if (nxt.x, nxt.y) == goal:
                st["wait"] = st.get("wait", 0) + 1
                if st["wait"] > 120:
                    return "failed:goal_occupied"
                return 0
            return self._replan_or_fail(it, snap, grid, goal, "npc", bump=(nxt.x, nxt.y))

        if not p.moving:
            st["stuck"] = st.get("stuck", 0) + 1
            if st["stuck"] > STUCK_FRAMES:
                return self._replan_or_fail(it, snap, grid, goal, "blocked", bump=(nxt.x, nxt.y))
        st["expect"] = (nxt.x, nxt.y)
        st["final_step"] = len(path) == 1
        keys = DIR_KEYS[nxt.direction]
        if it.args.get("run") and p.has_running_shoes and not p.surfing and not nxt.jump:
            keys |= KEY_B
        return keys

    def _replan_or_fail(self, it, snap, grid, goal, why, bump=None):
        st = it.state
        st["replans"] = st.get("replans", 0) + 1
        if st["replans"] > MAX_REPLANS:
            return f"failed:{'blocked' if why != 'lost' else 'lost'}"
        if bump is not None and why == "blocked":
            st.setdefault("temp_blocked", set()).add(bump)
        if not self._plan(it, snap, grid, goal):
            return "failed:unreachable"
        return 0

    def _resolve_goal(self, it: Intent, snap: Snapshot, grid: MapGrid) -> tuple[int, int] | None:
        st = it.state
        if "goal" not in st:
            target = (it.args["x"], it.args["y"])
            if it.args.get("exact"):
                st["goal"] = target
            else:
                p = snap.player
                st["goal"] = grid.nearest_reachable((p.x, p.y), p.elevation, target, p.surfing,
                                                    self._blocked_tiles(snap) - {target})
                it.args["snapped_to"] = st["goal"]
        return st["goal"]

    def _step_goto(self, it: Intent, snap: Snapshot, grid: MapGrid | None) -> int:
        if grid is None or snap.player is None:
            return 0
        goal = self._resolve_goal(it, snap, grid)
        if goal is None:
            self._finish(it, Status.ABORTED, "unreachable")
            return 0
        r = self._walk(it, snap, grid, it.state.get("goal", goal))
        if r == "arrived":
            self._finish(it, Status.DONE, "arrived")
            return 0
        if isinstance(r, str):
            self._finish(it, Status.ABORTED, r.split(":", 1)[1])
            return 0
        return r

    # -- face ---------------------------------------------------------------
    def _turn(self, it: Intent, snap: Snapshot, direction: int) -> int | None:
        """Keys to turn toward `direction`, or None when already facing it."""
        p = snap.player
        if p.facing == D.DIR_NAMES[direction]:
            return 0 if p.turning else None
        if p.moving or p.turning:
            return 0
        # A short tap turns in place; holding longer would walk.
        st = it.state
        st["turn_wait"] = st.get("turn_wait", 0) + 1
        if st["turn_wait"] % 12 == 1:
            return DIR_KEYS[direction]
        return 0

    def _step_face(self, it: Intent, snap: Snapshot, grid) -> int:
        if snap.player is None:
            return 0
        keys = self._turn(it, snap, it.args["direction"])
        if keys is None and not snap.player.moving and not snap.player.turning:
            self._finish(it, Status.DONE)
            return 0
        if it.state.get("turn_wait", 0) > 60:
            self._finish(it, Status.ABORTED, "could_not_turn")
            return 0
        return keys or 0

    # -- interact -----------------------------------------------------------
    def _step_interact(self, it: Intent, snap: Snapshot, grid: MapGrid | None) -> int:
        st = it.state
        p = snap.player
        if grid is None or p is None:
            return 0
        if st.get("pressed"):
            # Wait for the script to start; re-press A a few times in case the press
            # landed on a frame where the game wasn't reading field input.
            st["since_press"] = st.get("since_press", 0) + 1
            if st["since_press"] > SCRIPT_START_TIMEOUT:
                self._finish(it, Status.ABORTED, "nothing_happened")
                return 0
            if st["since_press"] % 10 == 0:
                return KEY_A
            return 0

        if it.args["local_id"] is not None:
            obj = snap.object_by_local_id(it.args["local_id"])
            if obj is None:
                self._finish(it, Status.ABORTED, "no_such_object")
                return 0
            target = (obj.x, obj.y)
        else:
            target = (it.args["x"], it.args["y"])

        dx, dy = target[0] - p.x, target[1] - p.y
        if abs(dx) + abs(dy) == 1 and not p.moving:
            direction = next(dd for dd, v in D.DIR_VECTORS.items() if v == (dx, dy))
            keys = self._turn(it, snap, direction)
            if keys is not None:
                return keys
            st["pressed"] = True
            return KEY_A

        # Walk to the best tile next to the target; re-pick if the target moves.
        if st.get("target") != target or st.get("approach") is None:
            st["target"] = target
            blocked = self._blocked_tiles(snap) - {(p.x, p.y)}
            cands = []
            for vx, vy in D.DIR_VECTORS.values():
                t = (target[0] + vx, target[1] + vy)
                if t in blocked:
                    continue
                path = grid.find_path((p.x, p.y), p.elevation, t, p.surfing, blocked)
                if path is not None:
                    cands.append((len(path), t))
            if not cands:
                self._finish(it, Status.ABORTED, "unreachable")
                return 0
            st["approach"] = min(cands)[1]
            st["path"] = None
            st["replans"] = 0
        r = self._walk(it, snap, grid, st["approach"])
        if r == "arrived":
            st["approach"] = None if (abs(target[0] - p.x) + abs(target[1] - p.y)) != 1 else st["approach"]
            return 0
        if isinstance(r, str):
            self._finish(it, Status.ABORTED, r.split(":", 1)[1])
            return 0
        return r

    # -- text and menus -------------------------------------------------------
    def _step_advance_text(self, it: Intent, snap: Snapshot, grid) -> int:
        st = it.state
        if snap.menu is not None:
            self._finish(it, Status.DONE, "menu")
            return 0
        if not it.args["until_closed"] and st.get("pressed") and not self._tap_release:
            self._finish(it, Status.DONE, "pressed")
            return 0
        if st.get("frames", 0) > 60 * 60:
            self._finish(it, Status.ABORTED, "timeout")
            return 0
        if it.args["until_closed"] and st.get("pressed") and snap.text is None and not snap.script_running:
            self._finish(it, Status.DONE, "closed")
            return 0
        keys = self._tap(KEY_A)
        if keys:
            st["pressed"] = True
        return keys

    def _step_choose(self, it: Intent, snap: Snapshot, grid) -> int:
        st = it.state
        m = snap.menu
        if st.get("pressed"):
            if m is None or not self._tap_release:
                self._finish(it, Status.DONE, "chosen")
            return 0
        if m is None:
            st["no_menu"] = st.get("no_menu", 0) + 1
            if st["no_menu"] > MENU_STEP_TIMEOUT:
                self._finish(it, Status.ABORTED, "no_menu")
            return 0
        idx = it.args["index"]
        if idx < 0:
            st["pressed"] = True
            return self._tap(KEY_B)
        idx = max(m.min, min(m.max, idx))
        if m.cursor == idx:
            keys = self._tap(KEY_A)
            if keys:
                st["pressed"] = True
            return keys
        st["moves"] = st.get("moves", 0) + 1
        if st["moves"] > 4 * (m.max - m.min + 2) + MENU_STEP_TIMEOUT:
            self._finish(it, Status.ABORTED, "cursor_stuck")
            return 0
        return self._tap(KEY_DOWN if m.cursor < idx else KEY_UP)

    def _step_press(self, it: Intent, snap: Snapshot, grid) -> int:
        st = it.state
        held = st.get("held", 0)
        if held < it.args["frames"]:
            st["held"] = held + 1
            return it.args["buttons"]
        self._finish(it, Status.DONE)
        return 0
