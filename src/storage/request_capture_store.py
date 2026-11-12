"""Bounded, restart-safe evidence segments, independent of the billing database.

Each frame is independently compressed/checksummed. Eviction happens BEFORE an
append, counting allocated blocks (including segment rounding), not just JSON
length. One advisory lock serializes maintenance, reads and writes across workers.
No WAL, temporary database or unbounded index is created.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import threading
import time
import zlib


POLICIES = ("attempts", "features", "normal", "errors")
HEADER = struct.Struct("!4sIId16s16s")
MAGIC = b"GCP1"
MAX_FRAME = 8 * 1024**2
RESERVE = 16 * 1024


def _hash(value):
    return hashlib.sha256(str(value or "").encode()).digest()[:16]


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


class CaptureStore:
    def __init__(self, root):
        self.root = Path(root)
        self._lock = threading.RLock()
        self._dropped = Counter()
        self._pending = Counter()
        self._last_error = None
        self._tails = {}  # At most one verified append position per policy.

    def _free_bytes(self):
        return shutil.disk_usage(self.root).free

    def _directory(self, name):
        if name not in POLICIES:
            raise ValueError("Unknown capture policy")
        return self.root / name

    @contextmanager
    def _guard(self):
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            locked = False
            before_state = None
            try:
                # A stalled reader/writer must not indefinitely stall inference.
                deadline = time.monotonic() + 0.5
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Capture store busy")
                        time.sleep(0.005)
                state_path = self.root / "state.json"
                try:
                    with state_path.open("rb") as state_file:
                        state = json.loads(state_file.read(16384))
                    self._dropped = Counter(state.get("dropped", {}))
                    self._last_error = self._last_error or state.get("last_error")
                except (FileNotFoundError, ValueError, OSError):
                    pass
                before_state = _json({"dropped": dict(self._dropped), "last_error": self._last_error})
                self._dropped.update(self._pending)
                self._pending.clear()
                yield
            finally:
                # Fixed-size control metadata, outside policy directories. No
                # append-only failure log can grow while content capture drops.
                try:
                    if locked:
                        self._dropped.update(self._pending)
                        self._pending.clear()
                    payload = _json({"dropped": dict(self._dropped), "last_error": self._last_error})
                    if locked and len(payload) <= 16384 and (payload != before_state or not (self.root / "state.json").exists()):
                        state_fd = os.open(self.root / "state.json", os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                        try:
                            os.write(state_fd, payload)
                        finally:
                            os.close(state_fd)
                except OSError:
                    pass
                if locked:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _segments(self, name):
        folder = self._directory(name)
        if not folder.exists():
            return []
        return sorted(p for p in folder.glob("*.seg")
                      if p.stem.isdigit() and stat.S_ISREG(p.lstat().st_mode))

    @staticmethod
    def _allocated(path):
        info = path.stat()
        return max(info.st_size, info.st_blocks * 512)

    def usage(self, name):
        folder = self._directory(name)
        return (self._allocated(folder) if folder.exists() else 0) + sum(
            self._allocated(p) for p in self._segments(name))

    @staticmethod
    def _scan(path, *, payload=False):
        """Yield valid frames only. A torn/corrupt tail ends this segment."""
        with path.open("rb") as inp:
            while True:
                raw = inp.read(HEADER.size)
                if not raw:
                    return
                if len(raw) != HEADER.size:
                    return
                magic, length, checksum, timestamp, request_hash, account_hash = HEADER.unpack(raw)
                if magic != MAGIC or not 0 < length <= MAX_FRAME:
                    return
                packed = inp.read(length)
                if len(packed) != length or zlib.crc32(packed) != checksum:
                    return
                yield inp.tell(), timestamp, request_hash, account_hash, packed if payload else None

    def _maintain(self, name, policy, now):
        cutoff = now - policy["retention_hours"] * 3600
        segments = self._segments(name)
        for path in list(segments):
            # Segment mtime is its latest append. Queries ALSO filter individual
            # timestamps so an unexpired neighbour cannot expose expired rows.
            if path.stat().st_mtime < cutoff:
                path.unlink()
                self._dropped[f"{name}:expired_segments"] += 1
                segments.remove(path)
        while segments and self.usage(name) > policy["max_bytes"]:
            segments.pop(0).unlink()
            self._dropped[f"{name}:evicted_segments"] += 1

    def maintain(self, name, policy, *, now=None):
        with self._guard():
            self._maintain(name, policy, time.time() if now is None else now)

    def _drop(self, name, reason):
        with self._lock:
            self._pending[f"{name}:{reason}"] += 1
            self._last_error = reason
        return False

    def write(self, name, event, policy, *, now=None, min_free_bytes=0,
              max_entry_bytes=256 * 1024):
        if not policy["enabled"]:
            return False
        now = time.time() if now is None else now
        try:
            raw = _json(event)
            if len(raw) > max_entry_bytes:
                event = {k: event[k] for k in (
                    "request_id", "attempt_id", "credential_name", "created_at",
                    "status_code", "kind", "model", "phase",
                ) if k in event}
                event.update(capture_state="entry_too_large", original_bytes=len(raw),
                             sha256=hashlib.sha256(raw).hexdigest())
                raw = _json(event)
            packed = zlib.compress(raw, level=3)
            if len(packed) > MAX_FRAME:
                return self._drop(name, "entry_too_large")
            timestamp = float(event.get("created_at", now))
            frame = HEADER.pack(MAGIC, len(packed), zlib.crc32(packed), timestamp,
                                _hash(event.get("request_id")),
                                _hash(event.get("credential_name"))) + packed
            with self._guard():
                folder = self._directory(name)
                folder.mkdir(mode=0o700, exist_ok=True)
                self._maintain(name, policy, now)
                if self._free_bytes() < min_free_bytes + len(frame) + RESERVE:
                    return self._drop(name, "low_disk")
                block = os.statvfs(folder).f_frsize or 4096
                budget = policy["max_bytes"]
                segment_limit = min(4 * 1024**2, max(block, budget // 8))
                segments = self._segments(name)
                current = segments[-1] if segments else None
                if current:
                    info = current.stat()
                    signature = (str(current), info.st_size, info.st_mtime_ns)
                    if self._tails.get(name) == signature:
                        valid_end = info.st_size
                    else:
                        valid_end = 0
                        for valid_end, *_ in self._scan(current):
                            pass
                    if current.stat().st_size != valid_end:
                        with current.open("r+b") as out:
                            out.truncate(valid_end)
                        self._dropped[f"{name}:recovered_tail"] += 1
                    if valid_end + len(frame) > segment_limit:
                        current = None
                old_size = current.stat().st_size if current else 0
                old_blocks = self._allocated(current) if current else 0
                new_blocks = ((old_size + len(frame) + block - 1) // block) * block
                extra = max(new_blocks - old_blocks, 0)
                # Reserve directory expansion; only app-owned segment files are
                # included, with no index or journal outside the policy budget.
                while segments and self.usage(name) + extra + RESERVE > budget:
                    oldest = segments.pop(0)
                    oldest.unlink()
                    self._dropped[f"{name}:evicted_segments"] += 1
                    if oldest == current:
                        current = None
                        old_size = 0
                        extra = ((len(frame) + block - 1) // block) * block
                if self.usage(name) + extra + RESERVE > budget:
                    return self._drop(name, "capacity")
                if current is None:
                    sequence = max(time.time_ns(), int(segments[-1].stem) + 1 if segments else 0)
                    current = folder / f"{sequence:020d}.seg"
                fd = os.open(current, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                try:
                    # Reserve precisely the planned extent first. In particular,
                    # avoid filesystem speculative append preallocation making
                    # st_blocks exceed the preflight budget on ext4/XFS.
                    os.posix_fallocate(fd, old_size, len(frame))
                    os.lseek(fd, old_size, os.SEEK_SET)
                    view = memoryview(frame)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise OSError("Incomplete capture append")
                        view = view[written:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.utime(current, (now, now))
                info = current.stat()
                self._tails[name] = (str(current), info.st_size, info.st_mtime_ns)
                return True
        except (OSError, ValueError, TimeoutError, TypeError, OverflowError) as exc:
            return self._drop(name, type(exc).__name__)

    def query(self, name, policy, *, now=None, request_id=None, credential_name=None,
              since=0, before=None, limit=100, status_code=None, max_result_bytes=8 * 1024**2,
              with_status=False, complete_only=False):
        now = time.time() if now is None else now
        cutoff = max(float(since), now - policy["retention_hours"] * 3600)
        result = []
        request_hash = _hash(request_id) if request_id else None
        account_hash = _hash(credential_name) if credential_name else None
        limit = min(max(int(limit), 1), 1000)
        used = 0
        def page(limited=False):
            return {"records": result, "limited": limited} if with_status else result
        with self._guard():
            segments = list(reversed(self._segments(name)))
        for path in segments:
            # Release the lock between segments so historical searches do not
            # pause new evidence writes for the duration of a whole-store scan.
            with self._guard():
                if not path.exists():
                    continue
                frames = []
                start = 0
                for end, timestamp, rh, ah, _ in self._scan(path):
                    frames.append((start, end, timestamp, rh, ah))
                    start = end
                with path.open("rb") as inp:
                    for start, end, timestamp, rh, ah in reversed(frames):
                        if timestamp < cutoff or (before is not None and timestamp >= before):
                            continue
                        if request_hash and rh != request_hash or account_hash and ah != account_hash:
                            continue
                        inp.seek(start + HEADER.size)
                        try:
                            decoder = zlib.decompressobj()
                            raw = decoder.decompress(inp.read(end - start - HEADER.size), MAX_FRAME + 1)
                            if len(raw) > MAX_FRAME or not decoder.eof:
                                continue
                            row = json.loads(raw)
                        except (zlib.error, ValueError):
                            continue
                        if complete_only and row.get("incomplete"):
                            continue
                        if status_code is None or row.get("status_code") == status_code:
                            if used + len(raw) > max_result_bytes:
                                return page(True)
                            used += len(raw)
                            result.append(row)
                            if len(result) >= limit:
                                return page(True)
        return page()

    def status(self):
        with self._guard():
            return {"dropped": dict(self._dropped), "last_error": self._last_error,
                    "free_disk_bytes": self._free_bytes(),
                    "policies": {name: {"disk_bytes": self.usage(name),
                                        "segments": len(self._segments(name))} for name in POLICIES}}
