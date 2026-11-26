"""In-process mGBA core (host/libgbahost.so via ctypes), stepped one frame at a time.

The host owns the emulator: set the keys, run a frame, read memory, repeat. Memory
regions are exposed as zero-copy NumPy views of mGBA's own buffers, so reading game
state between frames is just array indexing.
"""
from __future__ import annotations

import ctypes as C
import os
from pathlib import Path

import numpy as np

from .state import SCREEN_H, SCREEN_W, GBAState

KEY_A, KEY_B, KEY_SELECT, KEY_START = 1 << 0, 1 << 1, 1 << 2, 1 << 3
KEY_RIGHT, KEY_LEFT, KEY_UP, KEY_DOWN = 1 << 4, 1 << 5, 1 << 6, 1 << 7
KEY_R, KEY_L = 1 << 8, 1 << 9
KEY_NAMES = {
    "A": KEY_A, "B": KEY_B, "SELECT": KEY_SELECT, "START": KEY_START,
    "RIGHT": KEY_RIGHT, "LEFT": KEY_LEFT, "UP": KEY_UP, "DOWN": KEY_DOWN, "R": KEY_R, "L": KEY_L,
}

_LIB_DEFAULT = Path(__file__).resolve().parent.parent / "host" / "libgbahost.so"

# address-space prefix (addr >> 24) -> mGBA memory block name
_BLOCKS = {0x02: "wram", 0x03: "iwram", 0x05: "palette", 0x06: "vram", 0x07: "oam"}
_ROM_PREFIXES = range(0x08, 0x0E)


class CoreError(RuntimeError):
    pass


def _load_lib(path: str | os.PathLike | None = None) -> C.CDLL:
    path = Path(path or os.environ.get("GBAHOST_LIB") or _LIB_DEFAULT)
    if not path.exists():
        raise CoreError(f"{path} not found; build it with `make -C host` (needs libmgba-dev)")
    lib = C.CDLL(str(path))
    vp, u32, sz = C.c_void_p, C.c_uint32, C.c_size_t
    sigs = {
        "gh_open": (vp, [C.c_char_p, C.c_char_p]),
        "gh_close": (None, [vp]),
        "gh_reset": (None, [vp]),
        "gh_set_keys": (None, [vp, u32]),
        "gh_run_frame": (None, [vp]),
        "gh_frame": (u32, [vp]),
        "gh_video": (vp, [vp, C.POINTER(C.c_uint), C.POINTER(C.c_uint)]),
        "gh_color_bits": (C.c_int, []),
        "gh_block": (vp, [vp, C.c_char_p, C.POINTER(sz), C.POINTER(u32)]),
        "gh_raw_read16": (u32, [vp, u32]),
        "gh_raw_write8": (None, [vp, u32, C.c_uint8]),
        "gh_state_size": (sz, [vp]),
        "gh_save_state": (C.c_int, [vp, vp]),
        "gh_load_state": (C.c_int, [vp, vp]),
    }
    for name, (res, args) in sigs.items():
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = res, args
    return lib


class Core:
    """One emulated GBA. Not thread-safe; drive it from one loop."""

    def __init__(self, rom: str | os.PathLike, save: str | os.PathLike | None = None, lib=None):
        self._lib = _load_lib(lib)
        if self._lib.gh_color_bits() != 32:
            raise CoreError("libmgba was built with 16-bit color; 32-bit is required")
        self._h = self._lib.gh_open(os.fsencode(rom), os.fsencode(save) if save else None)
        if not self._h:
            raise CoreError(f"could not load {rom}")
        self.keys = 0
        self._views: dict[str, tuple[np.ndarray, int]] = {}
        for name in (*_BLOCKS.values(), "cart0"):
            self._views[name] = self._map_block(name)
        w, h = C.c_uint(), C.c_uint()
        ptr = self._lib.gh_video(self._h, C.byref(w), C.byref(h))
        self._video = np.ctypeslib.as_array(C.cast(ptr, C.POINTER(C.c_uint8)), shape=(h.value, w.value, 4))

    def _map_block(self, name: str) -> tuple[np.ndarray, int]:
        size, start = C.c_size_t(), C.c_uint32()
        ptr = self._lib.gh_block(self._h, name.encode(), C.byref(size), C.byref(start))
        if not ptr:
            raise CoreError(f"memory block {name!r} not available")
        arr = np.ctypeslib.as_array(C.cast(ptr, C.POINTER(C.c_uint8)), shape=(size.value,))
        return arr, start.value

    def close(self) -> None:
        if self._h:
            self._lib.gh_close(self._h)
            self._h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- stepping -----------------------------------------------------------
    def set_keys(self, keys: int) -> None:
        self.keys = keys
        self._lib.gh_set_keys(self._h, keys)

    def run_frame(self) -> None:
        self._lib.gh_run_frame(self._h)

    def step(self, keys: int | None = None, frames: int = 1) -> None:
        if keys is not None:
            self.set_keys(keys)
        for _ in range(frames):
            self._lib.gh_run_frame(self._h)

    @property
    def frame(self) -> int:
        return self._lib.gh_frame(self._h)

    def reset(self) -> None:
        self._lib.gh_reset(self._h)

    # -- savestates ---------------------------------------------------------
    def save_state(self) -> bytes:
        buf = C.create_string_buffer(self._lib.gh_state_size(self._h))
        if not self._lib.gh_save_state(self._h, buf):
            raise CoreError("saveState failed")
        return buf.raw

    def load_state(self, data: bytes) -> None:
        if not self._lib.gh_load_state(self._h, data):
            raise CoreError("loadState failed")

    # -- memory -------------------------------------------------------------
    def region(self, address: int) -> tuple[np.ndarray, int]:
        """(buffer, offset into buffer) for a bus address. Mirrors are folded."""
        prefix = address >> 24
        if prefix in _ROM_PREFIXES:
            arr, _ = self._views["cart0"]
            return arr, (address - 0x08000000) % max(len(arr), 1)
        name = _BLOCKS.get(prefix)
        if name is None:
            raise CoreError(f"address {address:#010x} is not in a readable region")
        arr, start = self._views[name]
        return arr, (address - start) % len(arr)

    def read(self, address: int, length: int) -> bytes:
        arr, off = self.region(address)
        return arr[off:off + length].tobytes()

    def view(self, address: int, length: int) -> np.ndarray:
        """Zero-copy uint8 view (valid until the core is closed)."""
        arr, off = self.region(address)
        return arr[off:off + length]

    def u8(self, address: int) -> int:
        arr, off = self.region(address)
        return int(arr[off])

    def u16(self, address: int) -> int:
        arr, off = self.region(address)
        return int(arr[off]) | int(arr[off + 1]) << 8

    def u32(self, address: int) -> int:
        arr, off = self.region(address)
        return int(arr[off]) | int(arr[off + 1]) << 8 | int(arr[off + 2]) << 16 | int(arr[off + 3]) << 24

    def s16(self, address: int) -> int:
        v = self.u16(address)
        return v - 0x10000 if v & 0x8000 else v

    def io16(self, offset: int) -> int:
        """I/O register via mGBA's side-effect-free view (works for write-only regs)."""
        return self._lib.gh_raw_read16(self._h, 0x04000000 + offset)

    def write8(self, address: int, value: int) -> None:
        arr, off = self.region(address)
        arr[off] = value & 0xFF

    # -- display ------------------------------------------------------------
    def video_rgb(self) -> np.ndarray:
        """Copy of the last frame mGBA rendered, (160, 240, 3) uint8."""
        return self._video[:SCREEN_H, :SCREEN_W, :3].copy()

    def display_state(self) -> GBAState:
        """Snapshot of display memory in the shadow renderer's format."""
        st = GBAState()
        io16 = st.io.view("<u2")
        for i in range(0x60 // 2):
            io16[i] = self.io16(i * 2)
        st.palette[:] = self._views["palette"][0][:len(st.palette)]
        st.vram[:] = self._views["vram"][0][:len(st.vram)]
        st.oam[:] = self._views["oam"][0][:len(st.oam)]
        st.frame = self.frame
        return st
