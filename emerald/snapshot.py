"""Read a semantic snapshot of Emerald's state from memory, once per frame.

A snapshot says what is true right now. It holds no history: anything that looks
like an event (map changed, dialogue opened) is derived by comparing two snapshots
(see events.py).

Coordinates are *map* coordinates (the ones in the decomp's map.json and in
gSaveBlock1Ptr->pos), not the +7 map-buffer coordinates the game uses internally.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Protocol

from . import data as D
from .data import EmeraldData
from .text import decode


class Memory(Protocol):
    def u8(self, address: int) -> int: ...
    def u16(self, address: int) -> int: ...
    def u32(self, address: int) -> int: ...
    def s16(self, address: int) -> int: ...
    def read(self, address: int, length: int) -> bytes: ...


class Mode(str, enum.Enum):
    """Who may act, and which intents make sense. Exactly one per frame."""

    OVERWORLD = "overworld"    # player has control; goto/face/interact accepted
    SCRIPT = "script"          # dialogue or cutscene; only text/menu intents accepted
    DIRECT = "direct"          # bikes etc.: raw d-pad passthrough, client mirrors
    SCREEN = "screen"          # battle, menus, title...: show the GBA screen, raw buttons
    TRANSITION = "transition"  # warps, fades, map loads: nothing accepted, wait


@dataclass(frozen=True)
class ObjectInfo:
    slot: int
    local_id: int
    gfx_id: int
    gfx: str
    x: int
    y: int
    prev_x: int
    prev_y: int
    elevation: int
    facing: str
    movement_dir: str
    movement_type: int
    trainer_type: int
    moving: bool
    invisible: bool
    offscreen: bool
    is_player: bool


@dataclass(frozen=True)
class PlayerInfo:
    x: int
    y: int
    elevation: int
    facing: str
    flags: int
    on_bike: bool
    surfing: bool
    moving: bool            # mid-step between tiles (tileTransitionState != 0)
    turning: bool           # turning in place (runningState == TURN_DIRECTION)
    behavior: int           # metatile behavior under the player
    has_running_shoes: bool
    object_slot: int


@dataclass(frozen=True)
class TextInfo:
    full: str               # the whole current message (gStringVar4)
    shown: str              # what has been printed so far
    printing: bool          # text printer still running
    waiting: bool           # printer is waiting for a button press


@dataclass(frozen=True)
class MenuInfo:
    kind: str               # "yesno" | "multichoice"
    cursor: int
    min: int
    max: int


@dataclass(frozen=True)
class BattleInfo:
    type_flags: int
    outcome: int
    action_cursor: int
    move_cursor: int


@dataclass(frozen=True)
class Snapshot:
    frame: int
    mode: Mode
    screen: str                     # gMain.callback2 name, or "start_menu"
    map_group: int
    map_num: int
    map_name: str
    player: PlayerInfo | None
    objects: tuple[ObjectInfo, ...] = ()
    text: TextInfo | None = None
    menu: MenuInfo | None = None
    battle: BattleInfo | None = None
    field_locked: bool = False
    script_running: bool = False
    fading: bool = False
    map_layout_id: int = 0
    tasks: tuple[str, ...] = field(default=(), repr=False)

    def object_by_local_id(self, local_id: int) -> ObjectInfo | None:
        for o in self.objects:
            if o.local_id == local_id and not o.is_player:
                return o
        return None

    def to_dict(self) -> dict:
        """JSON-friendly form (for sending to an out-of-process client)."""
        from dataclasses import asdict
        d = asdict(self)
        d["mode"] = self.mode.value
        d.pop("tasks", None)
        return d


def _bit(v: int, n: int) -> bool:
    return bool((v >> n) & 1)


def _flag(mem: Memory, sb1: int, d: EmeraldData, flag: int) -> bool:
    return _bit(mem.u8(sb1 + d.const["SAVEBLOCK1_FLAGS"] + (flag >> 3)), flag & 7)


def read_object(mem: Memory, d: EmeraldData, slot: int) -> ObjectInfo | None:
    base = d.sym["gObjectEvents"] + slot * D.OBJECT_EVENT_SIZE
    b0 = mem.u8(base)
    if not b0 & 1:
        return None
    b1, b2 = mem.u8(base + 1), mem.u8(base + 2)
    dirs = mem.u16(base + 0x18)
    gfx = mem.u8(base + 5)
    held_active, held_finished, single_active = _bit(b0, 6), _bit(b0, 7), _bit(b0, 1)
    x, y = mem.s16(base + 0x10), mem.s16(base + 0x12)
    px, py = mem.s16(base + 0x14), mem.s16(base + 0x16)
    return ObjectInfo(
        slot=slot,
        local_id=mem.u8(base + 8),
        gfx_id=gfx,
        gfx=d.gfx_names.get(gfx, str(gfx)),
        x=x - D.MAP_OFFSET, y=y - D.MAP_OFFSET,
        prev_x=px - D.MAP_OFFSET, prev_y=py - D.MAP_OFFSET,
        elevation=mem.u8(base + 0xB) & 0xF,
        facing=D.DIR_NAMES.get(dirs & 0xF, "none"),
        movement_dir=D.DIR_NAMES.get((dirs >> 4) & 0xF, "none"),
        movement_type=mem.u8(base + 6),
        trainer_type=mem.u8(base + 7),
        moving=(held_active and not held_finished) or single_active,
        invisible=_bit(b1, 5),
        offscreen=_bit(b1, 6),
        is_player=_bit(b2, 0),
    )


def active_tasks(mem: Memory, d: EmeraldData) -> list[int]:
    out = []
    for i in range(D.NUM_TASKS):
        base = d.sym["gTasks"] + i * D.TASK_SIZE
        if mem.u8(base + 4):
            out.append(mem.u32(base) & ~1)
    return out


def read_text(mem: Memory, d: EmeraldData) -> TextInfo | None:
    if mem.u8(d.sym["sFieldMessageBoxMode"]) == D.MSGBOX_HIDDEN:
        return None
    buf_addr = d.sym["gStringVar4"]
    raw = mem.read(buf_addr, 0x3E8)
    printer = d.sym["sTextPrinters"]  # field messages print in window 0 = printer 0
    active = bool(mem.u8(printer + 0x1B))
    state = mem.u8(printer + 0x1C)
    cur = mem.u32(printer)
    full = decode(raw, d.charmap)
    if active and buf_addr <= cur < buf_addr + len(raw):
        shown = decode(raw, d.charmap, stop=cur - buf_addr)
    else:
        shown = full
    # RENDER_STATE_WAIT = 1: waiting for A/B before continuing to the next page
    return TextInfo(full=full, shown=shown, printing=active, waiting=active and state == 1)


def read_snapshot(mem: Memory, d: EmeraldData, frame: int = 0) -> Snapshot:
    S = d.sym
    cb2 = mem.u32(S["gMain"] + 4) & ~1
    tasks = active_tasks(mem, d)
    sb1 = mem.u32(S["gSaveBlock1Ptr"])
    in_wram = 0x02000000 <= sb1 < 0x02040000
    group = mem.u8(sb1 + 4) if in_wram else 0
    num = mem.u8(sb1 + 5) if in_wram else 0
    field_locked = bool(mem.u8(S["sLockFieldControls"]))
    script_running = mem.u8(S["sGlobalScriptContextStatus"]) != D.SCRIPT_CONTEXT_SHUTDOWN
    fading = _bit(mem.u8(S["gPaletteFade"] + 7), 7)
    in_overworld = cb2 == S["CB2_Overworld"]

    player = None
    objects: list[ObjectInfo] = []
    if in_overworld or cb2 == S["BattleMainCB2"]:
        for slot in range(D.OBJECT_EVENTS_COUNT):
            o = read_object(mem, d, slot)
            if o is not None:
                objects.append(o)
        pa = S["gPlayerAvatar"]
        slot = mem.u8(pa + 5)
        po = next((o for o in objects if o.slot == slot), None)
        if po is not None and po.is_player:
            flags = mem.u8(pa)
            base = S["gObjectEvents"] + slot * D.OBJECT_EVENT_SIZE
            player = PlayerInfo(
                x=po.x, y=po.y, elevation=po.elevation, facing=po.facing, flags=flags,
                on_bike=bool(flags & (D.AVATAR_MACH_BIKE | D.AVATAR_ACRO_BIKE)),
                surfing=bool(flags & D.AVATAR_SURFING),
                moving=mem.u8(pa + 3) != 0,
                turning=mem.u8(pa + 2) == 1,
                behavior=mem.u8(base + 0x1E),
                has_running_shoes=in_wram and _flag(mem, sb1, d, d.const["FLAG_SYS_B_DASH"]),
                object_slot=slot,
            )

    text = read_text(mem, d) if in_overworld else None
    menu = None
    for kind, fn in (("yesno", "Task_HandleYesNoInput"), ("multichoice", "Task_HandleMultichoiceInput")):
        if S[fn] in tasks:
            m = S["sMenu"]
            def s8(a):
                v = mem.u8(a)
                return v - 256 if v & 0x80 else v
            menu = MenuInfo(kind=kind, cursor=s8(m + 2), min=s8(m + 3), max=s8(m + 4))
    battle = None
    if cb2 == S["BattleMainCB2"]:
        battle = BattleInfo(
            type_flags=mem.u32(S["gBattleTypeFlags"]), outcome=mem.u8(S["gBattleOutcome"]),
            action_cursor=mem.u8(S["gActionSelectionCursor"]), move_cursor=mem.u8(S["gMoveSelectionCursor"]))

    screen = d.code_name(cb2)
    if in_overworld:
        if S["StartMenuTask"] in tasks:
            mode, screen = Mode.SCREEN, "start_menu"
        elif fading:
            mode = Mode.TRANSITION
        elif not field_locked:
            mode = Mode.DIRECT if player is not None and player.on_bike else Mode.OVERWORLD
        elif script_running or text is not None or menu is not None:
            mode = Mode.SCRIPT
        else:
            mode = Mode.TRANSITION
    elif cb2 in (S["CB2_LoadMap"], S["CB2_DoChangeMap"]):
        mode = Mode.TRANSITION
    else:
        mode = Mode.SCREEN

    return Snapshot(
        frame=frame, mode=mode, screen=screen,
        map_group=group, map_num=num, map_name=d.map_name(group, num),
        player=player, objects=tuple(objects), text=text, menu=menu, battle=battle,
        field_locked=field_locked, script_running=script_running, fading=fading,
        map_layout_id=mem.u16(S["gMapHeader"] + 0x12),
        tasks=tuple(d.code_name(t) for t in tasks),
    )
