"""Lossless monitor-detail storage; legacy JSON TEXT remains readable.

Historical conversion is explicit maintenance, never a startup migration.
Stop writers and take a consistent backup before invoking this file as a CLI.
"""

import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import zlib


MAGIC = b"GCLI-DETAILS-ZLIB\x01\x00"


def compress_details(raw: bytes) -> bytes:
    packed = MAGIC + zlib.compress(raw, level=6)
    if zlib.decompress(packed[len(MAGIC):]) != raw:
        raise ValueError("Monitor content compression verification failed")
    return packed


def encode_details(details: dict) -> bytes:
    return compress_details(json.dumps(details, ensure_ascii=False).encode("utf-8"))


def decode_details(stored):
    if isinstance(stored, bytes) and stored.startswith(MAGIC):
        stored = zlib.decompress(stored[len(MAGIC):])
    return json.loads(stored or "{}")


def compress_history(db_path, batch_size=1, progress=None):
    """Convert only legacy detail TEXT, verifying exact bytes before each commit.

    Each batch is atomic and reruns skip compressed rows. No rows are deleted.
    The caller must deploy the compatible reader before running this function.
    """
    if not 1 <= batch_size <= 256:
        raise ValueError("batch_size must be between 1 and 256")
    result = {"records": 0, "before_bytes": 0, "after_bytes": 0}
    last_id = 0
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=rw", uri=True, timeout=30)) as db:
        db.execute("PRAGMA cache_size=-8192")
        while True:
            with db:
                db.execute("BEGIN IMMEDIATE")
                rows = db.execute(
                    "SELECT id, request_details FROM call_records "
                    "WHERE id > ? AND typeof(request_details) = 'text' ORDER BY id LIMIT ?",
                    (last_id, batch_size),
                ).fetchall()
                if not rows:
                    break
                for row_id, text in rows:
                    raw = text.encode("utf-8")
                    packed = compress_details(raw)
                    db.execute("UPDATE call_records SET request_details = ? WHERE id = ?", (packed, row_id))
                    saved = db.execute("SELECT request_details FROM call_records WHERE id = ?", (row_id,)).fetchone()[0]
                    if zlib.decompress(saved[len(MAGIC):]) != raw:
                        raise ValueError(f"Monitor content verification failed at row {row_id}")
                    result["records"] += 1
                    result["before_bytes"] += len(raw)
                    result["after_bytes"] += len(packed)
                last_id = rows[-1][0]
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            if progress:
                progress(dict(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--vacuum", action="store_true", help="Reclaim free pages; requires temporary disk space")
    args = parser.parse_args()
    def report(result):
        print(json.dumps(result), flush=True)
    report(compress_history(args.database, args.batch_size, progress=report))
    with closing(sqlite3.connect(args.database.resolve().as_uri() + "?mode=rw", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Database integrity check failed")
        if args.vacuum:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            pages = db.execute("PRAGMA page_count").fetchone()[0]
            free_pages = db.execute("PRAGMA freelist_count").fetchone()[0]
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            # VACUUM in WAL mode may need both a temporary database and WAL.
            required = 2 * (pages - free_pages) * page_size + 256 * 1024**2
            if shutil.disk_usage(args.database.parent).free < required:
                raise RuntimeError(f"VACUUM requires at least {required} free bytes")
            db.execute("VACUUM")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Database integrity check failed after VACUUM")
        print("Database integrity: ok", flush=True)


if __name__ == "__main__":
    main()
