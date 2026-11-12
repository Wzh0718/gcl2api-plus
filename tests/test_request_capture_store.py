import concurrent.futures
import os

from src.storage.request_capture_store import CaptureStore


def _process_writer(root, prefix):
    store = CaptureStore(root)
    for i in range(30):
        assert store.write("normal", event(f"{prefix}-{i}"), policy(), now=1000)


def policy(capacity=192 * 1024, hours=1):
    return {"enabled": True, "max_bytes": capacity, "retention_hours": hours}


def event(i, at=1000):
    return {"request_id": f"request-{i}", "credential_name": "account-a",
            "created_at": at, "status_code": 200, "text": os.urandom(5000).hex()}


def test_budget_is_physical_isolated_and_survives_restart(tmp_path):
    store = CaptureStore(tmp_path)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda i: store.write("normal", event(i), policy(), now=1000), range(80)))
    assert store.usage("normal") <= policy()["max_bytes"]
    assert store.write("errors", event("error"), policy(), now=1000)
    store = CaptureStore(tmp_path)
    rows = store.query("normal", policy(), now=1000, limit=100)
    assert rows and len(rows) < 80
    assert len({r["request_id"] for r in rows}) == len(rows)
    assert store.query("errors", policy(), now=1000)[0]["request_id"] == "request-error"
    store.maintain("normal", policy(96 * 1024), now=1000)
    assert store.usage("normal") <= 96 * 1024
    assert store.query("errors", policy(), now=1000)


def test_retention_is_enforced_even_within_live_segment(tmp_path):
    store = CaptureStore(tmp_path)
    store.write("normal", event(1, 1000), policy(), now=1000)
    store.write("normal", event(2, 4500), policy(), now=4500)
    assert [r["request_id"] for r in store.query("normal", policy(), now=4700)] == ["request-2"]
    store.maintain("normal", policy(), now=9000)
    assert store.query("normal", policy(), now=9000) == []


def test_partial_tail_recovers_without_losing_next_record(tmp_path):
    store = CaptureStore(tmp_path)
    assert store.write("normal", event(1), policy(), now=1000)
    segment = next((tmp_path / "normal").glob("*.seg"))
    with segment.open("ab") as out:
        out.write(b"broken-tail")
    assert store.write("normal", event(2), policy(), now=1000)
    assert {r["request_id"] for r in store.query("normal", policy(), now=1000)} == {"request-1", "request-2"}


def test_record_bound_low_disk_and_disabled_policy(tmp_path, monkeypatch):
    store = CaptureStore(tmp_path)
    assert not store.write("normal", event(1), {**policy(), "enabled": False}, now=1000)
    assert not list(tmp_path.rglob("*.seg"))
    monkeypatch.setattr(store, "_free_bytes", lambda: 0)
    assert not store.write("normal", event(2), policy(), min_free_bytes=100, now=1000)
    assert store.status()["dropped"]["normal:low_disk"] == 1
    monkeypatch.setattr(store, "_free_bytes", lambda: 10**12)
    assert store.write("normal", event(3), policy(), max_entry_bytes=2048, now=1000)
    row = store.query("normal", policy(), now=1000)[0]
    assert row["capture_state"] == "entry_too_large"
    assert "text" not in row and row["original_bytes"] > 2048


def test_multiple_processes_share_the_same_capacity_and_persist_drop_counts(tmp_path):
    import multiprocessing
    context = multiprocessing.get_context("spawn")
    children = [context.Process(target=_process_writer, args=(tmp_path, i)) for i in range(3)]
    for child in children:
        child.start()
    for child in children:
        child.join(10)
        assert child.exitcode == 0
    store = CaptureStore(tmp_path)
    assert store.usage("normal") <= policy()["max_bytes"]
    assert store.query("normal", policy(), now=1000)
    status = store.status()
    assert status["dropped"]["normal:evicted_segments"] > 0
    assert CaptureStore(tmp_path).status()["dropped"] == status["dropped"]


def test_reads_have_an_independent_decompressed_size_limit(tmp_path):
    store = CaptureStore(tmp_path)
    store.write("normal", {"request_id": "huge", "created_at": 1000, "text": "a" * 50000},
                policy(), now=1000)
    page = store.query("normal", policy(), now=1000, max_result_bytes=1000, with_status=True)
    assert page == {"records": [], "limited": True}


def test_allocated_disk_usage_never_exceeds_budget_during_append(tmp_path, monkeypatch):
    store = CaptureStore(tmp_path)
    original_write = os.write
    peak = 0
    def checked_write(fd, data):
        nonlocal peak
        size = original_write(fd, data)
        peak = max(peak, store.usage("normal"))
        assert peak <= policy()["max_bytes"]
        return size
    monkeypatch.setattr(os, "write", checked_write)
    for i in range(100):
        assert store.write("normal", event(i), policy(), now=1000)
    assert peak > 0
