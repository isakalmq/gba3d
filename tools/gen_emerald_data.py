#!/usr/bin/env python3
"""Generate emerald/data/bpee.json from a pokeemerald checkout and its symbol file.

    python tools/gen_emerald_data.py POKEEMERALD_DIR POKEEMERALD_SYM [-o emerald/data/bpee.json]

The symbol file is the `pokeemerald.sym` that `make` produces, or the one pret
publishes on the repository's `symbols` branch. The output holds only addresses,
enum values, the text encoding and names: no game code or assets. It is committed so
users don't need the decomp.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# Symbols the snapshot reader needs. A (name, size) pair picks one of several
# statics that share a name.
SYMBOLS = [
    "gSaveBlock1Ptr", "gSaveBlock2Ptr", "gObjectEvents", "gPlayerAvatar", "gMain",
    "gMapHeader", "gBackupMapLayout", "gStringVar4", "sTextPrinters", "gTasks",
    ("sMenu", 0xC), "sLockFieldControls", "sGlobalScriptContextStatus", "sFieldMessageBoxMode",
    "gPaletteFade", "gBattleTypeFlags", "gBattleOutcome", "gSpecialVar_Result", "gSpecialVar_LastTalked",
    "gSelectedObjectEvent", "gCamera", "gActionSelectionCursor", "gMoveSelectionCursor",
    "gPlayerPartyCount", "sMapConnectionFlags",
    # code addresses compared against gMain.callback1/2 and task funcs
    "CB1_Overworld", "CB2_Overworld", "BattleMainCB2", "CB2_InitBattle", "CB2_LoadMap",
    "CB2_DoChangeMap", "CB2_MainMenu", "CB2_InitTitleScreen",
    "Task_HandleYesNoInput", "Task_HandleMultichoiceInput", "StartMenuTask", "Task_DrawFieldMessage",
    "RomHeaderGameCode", "RomHeaderSoftwareVersion",
]

CONSTANTS = {
    # include/constants/flags.h
    "FLAG_SYS_B_DASH": 0x860 + 0x60,
    # include/global.h: struct SaveBlock1
    "SAVEBLOCK1_FLAGS": 0x1270,
    "SAVEBLOCK1_VARS": 0x139C,
}


def parse_sym(path: Path) -> dict[str, list[tuple[int, int]]]:
    out: dict[str, list[tuple[int, int]]] = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        addr, _, size, name = parts
        out.setdefault(name, []).append((int(addr, 16), int(size, 16)))
    return out


def pick_symbols(syms: dict[str, list[tuple[int, int]]]) -> dict[str, dict[str, int]]:
    out = {}
    for entry in SYMBOLS:
        name, size = entry if isinstance(entry, tuple) else (entry, None)
        cands = syms.get(name, [])
        if size is not None:
            cands = [c for c in cands if c[1] == size]
        if len(cands) != 1:
            raise SystemExit(f"symbol {name}: expected exactly one match, found {cands}")
        addr, sz = cands[0]
        out[name] = {"addr": addr, "size": sz}
    return out


def parse_enum(path: Path, prefix: str) -> dict[str, int]:
    """Values of a plain C enum (implicit numbering, `= N` overrides)."""
    text = re.sub(r"//.*", "", path.read_text())
    body = text[text.index("enum {") + 6:text.index("};")]
    out, value = {}, -1
    for item in body.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            name, expr = (s.strip() for s in item.split("=", 1))
            value = int(expr, 0)
        else:
            name, value = item, value + 1
        if name.startswith(prefix):
            out[name] = value
    return out


def parse_defines(path: Path, prefix: str) -> dict[str, int]:
    out = {}
    for m in re.finditer(rf"^#define\s+({prefix}\w+)\s+(0x[0-9A-Fa-f]+|\d+)\b", path.read_text(), re.M):
        out[m.group(1)] = int(m.group(2), 0)
    return out


def parse_tile_flags(path: Path, behaviors: dict[str, int], flag: str) -> list[int]:
    text = path.read_text()
    table = text[text.index("sTileBitAttributes["):]
    table = table[:table.index("};")]
    return sorted(behaviors[m.group(1)] for m in re.finditer(r"\[(MB_\w+)\]\s*=\s*([^,\n]+)", table)
                  if flag in m.group(2))


def parse_charmap(path: Path) -> dict[int, str]:
    """Single-byte characters only; multi-byte entries are control codes."""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^'(.+?)'\s*=\s*([0-9A-Fa-f]{2})\s*(?:@.*)?$", line)
        if m and len(m.group(1)) == 1:
            out.setdefault(int(m.group(2), 16), m.group(1))
    return out


def parse_maps(root: Path) -> dict[str, str]:
    groups = json.loads((root / "data/maps/map_groups.json").read_text())
    out = {}
    for gi, gname in enumerate(groups["group_order"]):
        for mi, map_dir in enumerate(groups[gname]):
            header = json.loads((root / "data/maps" / map_dir / "map.json").read_text())
            out[f"{gi}.{mi}"] = header["id"].removeprefix("MAP_")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("decomp", type=Path)
    ap.add_argument("sym", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path(__file__).resolve().parent.parent / "emerald/data/bpee.json")
    args = ap.parse_args()
    root = args.decomp

    behaviors = parse_enum(root / "include/constants/metatile_behaviors.h", "MB_")
    gfx = parse_defines(root / "include/constants/event_objects.h", "OBJ_EVENT_GFX_")
    data = {
        "game": "pokeemerald (BPEE, US rev 0)",
        "game_code": "BPEE",
        "symbols": pick_symbols(parse_sym(args.sym)),
        "constants": CONSTANTS,
        "metatile_behaviors": behaviors,
        "surfable_behaviors": parse_tile_flags(root / "src/metatile_behavior.c", behaviors, "TILE_FLAG_SURFABLE"),
        "encounter_behaviors": parse_tile_flags(root / "src/metatile_behavior.c", behaviors, "TILE_FLAG_HAS_ENCOUNTERS"),
        "object_gfx": {name.removeprefix("OBJ_EVENT_GFX_"): v for name, v in gfx.items()
                       if not name.startswith("OBJ_EVENT_GFX_VAR_") and name != "OBJ_EVENT_GFX_VARS"},
        "maps": parse_maps(root),
        "charmap": {f"{k:02X}": v for k, v in sorted(parse_charmap(root / "charmap.txt").items())},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {args.out}: {len(data['symbols'])} symbols, {len(behaviors)} behaviors, "
          f"{len(data['maps'])} maps, {len(data['object_gfx'])} object gfx, {len(data['charmap'])} chars")


if __name__ == "__main__":
    main()
