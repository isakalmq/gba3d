"""Wire format shared with lua/shadow_bridge.lua, a TCP receiver, and capture loading.

Message (little endian):
    header  "GBSH" u8 version u8 flags u16 record_count u32 frame u32 payload_len
    record  u8 region u32 offset u32 length, then `length` bytes
flags bit0 = full snapshot.
"""
from __future__ import annotations

import gzip
import socket
import struct
import threading
from pathlib import Path
from typing import Iterator

import numpy as np

from .state import REGION_SIZES, GBAState

MAGIC = b"GBSH"
VERSION = 1
FLAG_FULL = 1
HEADER = struct.Struct("<4sBBHII")
RECORD = struct.Struct("<BII")
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
    for region, offset, data in iter_records(payload, count):
        state.region(region)[offset:offset + len(data)] = np.frombuffer(data, np.uint8)
    state.frame = frame
    return bool(flags & FLAG_FULL)


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


class FrameReceiver:
    """Listens for the mGBA bridge and keeps the latest complete state.

    Runs in a background thread. `latest()` returns a copy of the newest state
    (or None if no full snapshot has arrived yet) plus a sequence number that
    increments with every received frame.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT):
        self.host, self.port = host, port
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
                    self._have_full = False
                try:
                    while not self._stop.is_set():
                        header = _recv_exact(conn, HEADER.size)
                        _, _, _, payload_len = parse_header(header)
                        payload = _recv_exact(conn, payload_len)
                        with self._lock:
                            full = apply_message(self._state, header, payload)
                            self._have_full = self._have_full or full
                            self._seq += 1
                        self.frames_received += 1
                except (ConnectionError, OSError, ProtocolError) as e:
                    print(f"[shadow] bridge connection closed: {e}")
                finally:
                    self.connected = False
