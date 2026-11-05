import json
import sqlite3
import zlib

import pytest


@pytest.mark.asyncio
async def test_new_details_are_compressed_and_legacy_details_remain_readable(tmp_path, monkeypatch):
    from src.storage.sqlite_manager import SQLiteManager

    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    manager = SQLiteManager()
    await manager.initialize()
    details = {"client": {"messages": [{"content": "中文正文🙂" * 10000}]}}
    try:
        await manager.insert_call_record(
            request_id="compressed", created_at=1, success=True, request_details=details,
        )
        with sqlite3.connect(manager._db_path) as db:
            stored = db.execute("SELECT request_details FROM call_records").fetchone()[0]
            assert isinstance(stored, bytes)
            assert len(stored) < len(json.dumps(details).encode()) // 10
            db.execute(
                "INSERT INTO call_records(request_id, created_at, success, request_details) "
                "SELECT 'legacy', created_at, success, ? FROM call_records LIMIT 1",
                (json.dumps(details, ensure_ascii=False),),
            )
        for request_id in ("compressed", "legacy"):
            assert (await manager.get_call_record_content(request_id))["request_details"] == details
        assert len(await manager.list_call_records()) == 2
    finally:
        await manager.close()


def test_historical_compression_preserves_exact_bytes_and_other_tables(tmp_path):
    from src.storage.call_record_content import MAGIC, compress_history, decode_details

    path = tmp_path / "history.db"
    original = ' {"client": {"text": "正文🙂"}, "n": 1.00} '
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE call_records(id INTEGER PRIMARY KEY, request_details TEXT)")
        db.executemany("INSERT INTO call_records(request_details) VALUES (?)", [(original,), ("{}",)])
        db.execute("CREATE TABLE credentials(secret TEXT)")
        db.execute("INSERT INTO credentials VALUES ('untouched')")
    assert compress_history(path, batch_size=1)["records"] == 2
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT request_details FROM call_records ORDER BY id").fetchall()
        assert zlib.decompress(rows[0][0][len(MAGIC):]) == original.encode("utf-8")
        assert decode_details(rows[0][0]) == json.loads(original)
        assert decode_details(rows[1][0]) == {}
        assert db.execute("SELECT secret FROM credentials").fetchone()[0] == "untouched"
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert compress_history(path)["records"] == 0


def test_conversion_rolls_back_batch_on_verification_failure(tmp_path):
    from src.storage import call_record_content as content

    path = tmp_path / "history.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE call_records(id INTEGER PRIMARY KEY, request_details TEXT)")
        db.executemany("INSERT INTO call_records VALUES (?, ?)", [(1, '{}'), (2, '{}')])
        db.execute("CREATE TRIGGER corrupt AFTER UPDATE ON call_records WHEN NEW.id=2 "
                   "BEGIN UPDATE call_records SET request_details=x'00' WHERE id=2; END")
    with pytest.raises(zlib.error):
        content.compress_history(path, batch_size=2)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT request_details FROM call_records").fetchall() == [('{}',), ('{}',)]
