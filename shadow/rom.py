"""Read-only access to a GBA ROM file, by bus address (0x08000000 + offset).

Adapters use it for static data the bridge doesn't stream, such as Pokemon's
tileset tables. ROMs are found in roms/ by the game code in their header.
"""
from __future__ import annotations

import struct
import zipfile
from pathlib import Path

ROM_BASE = 0x08000000
DEFAULT_ROM_DIR = Path(__file__).resolve().parent.parent / "roms"


def _load_bytes(path: Path) -> bytes:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".gba")]
            if not names:
                raise ValueError(f"{path}: no .gba file inside")
            return z.read(names[0])
    return path.read_bytes()


class Rom:
    def __init__(self, data: bytes, path: Path | None = None):
        self.data = data
        self.path = path

    @classmethod
    def open(cls, path: str | Path) -> "Rom":
        path = Path(path)
        return cls(_load_bytes(path), path)

    @property
    def game_code(self) -> str:
        return self.data[0xAC:0xB0].decode("ascii", "replace")

    def bytes(self, addr: int, n: int) -> bytes:
        off = addr - ROM_BASE
        if off < 0 or off + n > len(self.data):
            raise ValueError(f"{addr:08X}+{n:X} is outside the ROM")
        return self.data[off:off + n]

    def u16(self, addr: int) -> int:
        return struct.unpack("<H", self.bytes(addr, 2))[0]

    def u32(self, addr: int) -> int:
        return struct.unpack("<I", self.bytes(addr, 4))[0]

    def s32(self, addr: int) -> int:
        return struct.unpack("<i", self.bytes(addr, 4))[0]

    def u16_array(self, addr: int, n: int) -> tuple[int, ...]:
        return struct.unpack(f"<{n}H", self.bytes(addr, 2 * n))


def find_rom(game_code: str, directory: str | Path = DEFAULT_ROM_DIR) -> Rom | None:
    """The first .gba/.zip in `directory` whose header has `game_code`."""
    directory = Path(directory)
    if not directory.is_dir():
        return None
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in (".gba", ".zip"):
            continue
        try:
            rom = Rom.open(path)
        except (OSError, ValueError, zipfile.BadZipFile):
            continue
        if rom.game_code == game_code:
            return rom
    return None
