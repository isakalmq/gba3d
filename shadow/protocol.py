"""Wire format shared with lua/shadow_bridge.lua, a TCP receiver, and capture loading.

Bridge -> renderer (little endian):
    header  "GBSH" u8 version u8 flags u16 record_count u32 frame u32 payload_len
    record  u8 region u32 offset u32 length, then `length` bytes
flags bit0 = full snapshot; bit1 = hello (payload: s1 game code, s1 title; no records).
Regions: 0 IO, 1 palette, 2 VRAM, 3 OAM, 4 EWRAM, 5 IWRAM (offsets relative to the region).

Renderer -> bridge (watch request, replaces the previous one):
    "GBSW" u8 version u8 flags(bit0 = delay RAM by one frame) u16 count, count x (u32 address, u32 length)
"""
from __future__ import annotations

import gzip
import socket
import struct
import threading
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

from .state import REGION_SIZES, GBAState

MAGIC = b"GBSH"
VERSION = 1
FLAG_FULL = 1
FLAG_HELLO = 2
HEADER = struct.Struct("<4sBBHII")
RECORD = struct.Struct("<BII")
WATCH_MAGIC = b"GBSW"
WATCH_HEADER = struct.Struct("<4sBBH")
WATCH_RANGE = struct.Struct("<II")
DEFAULT_PORT = 61337


class ProtocolError(Exception):
    pass


def parse_header(data: bytes) -> tuple[int, int, int, int]:
    magic, version, flags, count, frame, payload_len = HEADER.unpack(data)
    if magic != MAGIC:
        raise ProtocolError(f"bad magic {magic!r}")
    if version != VERSION:
        raise ProtocolError(f"unsupported protocol version {version}")
    return flags, count, frame, payload_len


def iter_records(payload: bytes, count: int) -> Iterator[tuple[int, int, memoryview]]:
    view = memoryview(payload)
    pos = 0
    for _ in range(count):
        region, offset, length = RECORD.unpack_from(view, pos)
        pos += RECORD.size
        if region not in REGION_SIZES or offset + length > REGION_SIZES[region]:
            raise ProtocolError(f"record out of range: region={region} offset={offset:#x} len={length:#x}")
        yield region, offset, view[pos:pos + length]
        pos += length
    if pos != len(payload):
        raise ProtocolError("payload length mismatch")


def apply_message(state: GBAState, header: bytes, payload: bytes) -> bool:
    """Apply one message to `state` in place. Returns True if it was a full snapshot."""
    flags, count, frame, _ = parse_header(header)
    if flags & FLAG_HELLO:
        return False
    for region, offset, data in iter_records(payload, count):
        state.write(region, offset, np.frombuffer(data, np.uint8))
    state.frame = frame
    return bool(flags & FLAG_FULL)


def parse_hello(payload: bytes) -> tuple[str, str]:
    """(game code, title) from a hello payload."""
    n = payload[0]
    code = payload[1:1 + n].decode("ascii", "replace")
    m = payload[1 + n]
    title = payload[2 + n:2 + n + m].decode("ascii", "replace")
    return code, title


def encode_watch(ranges: list[tuple[int, int]], delay_frames: int = 0) -> bytes:
    """Watch request asking the bridge to stream these (bus address, length) RAM ranges."""
    if delay_frames not in (0, 1):
        raise ValueError("the bridge supports a RAM delay of 0 or 1 frames")
    out = WATCH_HEADER.pack(WATCH_MAGIC, VERSION, delay_frames, len(ranges))
    return out + b"".join(WATCH_RANGE.pack(a, n) for a, n in ranges)


def read_message(conn: socket.socket) -> tuple[bytes, bytes]:
    """Block until one complete bridge message arrives; returns (header, payload)."""
    header = _recv_exact(conn, HEADER.size)
    payload = _recv_exact(conn, parse_header(header)[3])
    return header, payload


def load_capture(path: str | Path) -> GBAState:
    """Load a .gbsh snapshot written by the bridge's capture mode (optionally gzipped)."""
    data = Path(path).read_bytes()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    header, payload = data[:HEADER.size], data[HEADER.size:]
    _, _, _, payload_len = parse_header(header)
    if len(payload) != payload_len:
        raise ProtocolError(f"{path}: truncated capture")
    state = GBAState()
    apply_message(state, header, payload)
    return state


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("bridge disconnected")
        buf += chunk
    return bytes(buf)


# Called with (game code, title) when the bridge connects; returns a watch
# request (ranges, delay_frames) to send back, or None.
HelloHandler = Callable[[str, str], "tuple[list[tuple[int, int]], int] | None"]


class FrameReceiver:
    """Listens for the mGBA bridge and keeps the latest complete state.

    Runs in a background thread. `latest()` returns a copy of the newest state
    (or None if no full snapshot has arrived yet) plus a sequence number that
    increments with every received frame. When the bridge says hello,
    `on_hello` decides which work-RAM ranges to request.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT, on_hello: HelloHandler | None = None):
        self.host, self.port = host, port
        self.on_hello = on_hello
        self.game_code: str | None = None
        self.game_title: str | None = None
        self._lock = threading.Lock()
        self._state = GBAState()
        self._have_full = False
        self._seq = 0
        self._snapshot: GBAState | None = None
        self._snapshot_seq = -1
        self.connected = False
        self.frames_received = 0
        self._stop = threading.Event()
        self._server = socket.create_server((host, port), reuse_port=False)
        self._server.settimeout(0.5)
        self.port = self._server.getsockname()[1]   # actual port when 0 was requested
        self._thread = threading.Thread(target=self._run, name="shadow-receiver", daemon=True)

    def start(self) -> "FrameReceiver":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._server.close()

    def latest(self) -> tuple[GBAState | None, int]:
        with self._lock:
            if not self._have_full:
                return None, self._seq
            if self._snapshot_seq != self._seq:
                self._snapshot = self._state.copy()
                self._snapshot_seq = self._seq
            return self._snapshot, self._seq

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            with conn:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn.settimeout(None)
                self.connected = True
                with self._lock:
                    # A new connection starts from scratch (RAM may now belong to another game).
                    self._state = GBAState()
                    self._have_full = False
                try:
                    while not self._stop.is_set():
                        header, payload = read_message(conn)
                        if parse_header(header)[0] & FLAG_HELLO:
                            self._handle_hello(conn, payload)
                            continue
                        with self._lock:
                            full = apply_message(self._state, header, payload)
                            self._have_full = self._have_full or full
                            self._seq += 1
                        self.frames_received += 1
                except (ConnectionError, OSError, ProtocolError) as e:
                    print(f"[shadow] bridge connection closed: {e}")
                finally:
                    self.connected = False

    def _handle_hello(self, conn: socket.socket, payload: bytes) -> None:
        self.game_code, self.game_title = parse_hello(payload)
        print(f"[shadow] bridge connected: game code {self.game_code!r}, title {self.game_title!r}")
        request = self.on_hello(self.game_code, self.game_title) if self.on_hello else None
        if request:
            ranges, delay = request
            conn.sendall(encode_watch(ranges, delay))
