"""Broadcast fan-out: independent readers, drop-to-live, no head-of-line stalls."""

import os
import time

from lofi.daemon import BUFFER_CHUNKS, BroadcastWriter

CHUNK = 64


def chunk(i: int) -> bytes:
    return bytes([i % 256]) * CHUNK


def open_reader(path: str, timeout_s: float = 5.0) -> int:
    """Non-blocking FIFO reader; retries until the writer side exists."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.01)


def read_all(fd: int, min_bytes: int, timeout_s: float = 5.0) -> bytes:
    out = b""
    deadline = time.monotonic() + timeout_s
    while len(out) < min_bytes and time.monotonic() < deadline:
        try:
            data = os.read(fd, 65536)
            if data:
                out += data
            else:
                time.sleep(0.005)
        except BlockingIOError:
            time.sleep(0.005)
    return out


def test_connected_reader_receives_everything(tmp_path):
    path = str(tmp_path / "a.pcm")
    bw = BroadcastWriter([path])
    try:
        fd = open_reader(path)
        for i in range(10):
            bw.write(chunk(i))
        data = read_all(fd, 10 * CHUNK)
        assert data == b"".join(chunk(i) for i in range(10))
        os.close(fd)
    finally:
        bw.close()


def test_absent_reader_never_stalls_the_other(tmp_path):
    a, b = str(tmp_path / "a.pcm"), str(tmp_path / "b.pcm")
    bw = BroadcastWriter([a, b])  # nobody ever reads b
    try:
        fd = open_reader(a)
        bw.write(chunk(255))
        assert read_all(fd, CHUNK)  # a's pump is connected and flowing
        start = time.monotonic()
        for i in range(BUFFER_CHUNKS * 3):  # far beyond b's buffer
            bw.write(chunk(i))
            time.sleep(0.002)  # gentle pacing, like the real 100ms station clock
        assert time.monotonic() - start < 2.0  # write() never blocked on b
        data = read_all(fd, BUFFER_CHUNKS * 3 * CHUNK)
        assert data == b"".join(chunk(i) for i in range(BUFFER_CHUNKS * 3))
        os.close(fd)
    finally:
        bw.close()


def test_late_reader_joins_live_not_backlog(tmp_path):
    path = str(tmp_path / "late.pcm")
    bw = BroadcastWriter([path])
    try:
        total = BUFFER_CHUNKS * 2
        for i in range(total):  # written with no reader attached
            bw.write(chunk(i))
        fd = open_reader(path)
        data = read_all(fd, CHUNK)  # whatever survived the drop-oldest buffer
        # first byte identifies the first chunk we got: must be recent, not chunk 0
        assert data[0] >= total - BUFFER_CHUNKS
        os.close(fd)
    finally:
        bw.close()
