"""log.py 文件轮转回归测试：超过 LOG_MAX_BYTES 后轮转为 <file>.1。"""

import log as log_mod


def test_rotate_when_limit_exceeded(tmp_path, monkeypatch):
    log_file = tmp_path / "log.txt"
    monkeypatch.setattr(log_mod, "_cached_log_file", str(log_file))
    monkeypatch.setattr(log_mod, "_cached_log_max_bytes", 100)
    # 阻止后台 writer 线程并发写同一文件，测试内手动控制句柄
    monkeypatch.setattr(log_mod, "_file_writing_disabled", True)
    try:
        assert log_mod._open_log_file("a")
        handle = log_mod._log_file_handle
        handle.write("x" * 90)
        handle.flush()
        assert log_file.stat().st_size == 90

        # 90 + 20 > 100：触发轮转，当前文件重命名为 .1
        log_mod._maybe_rotate_log_file(20)
        backup = tmp_path / "log.txt.1"
        assert backup.exists()
        assert backup.stat().st_size == 90
        assert log_mod._log_file_handle is not None
        assert log_mod._log_file_handle is not handle
    finally:
        log_mod._close_log_file()


def test_no_rotation_under_limit(tmp_path, monkeypatch):
    log_file = tmp_path / "log.txt"
    monkeypatch.setattr(log_mod, "_cached_log_file", str(log_file))
    monkeypatch.setattr(log_mod, "_cached_log_max_bytes", 1000)
    monkeypatch.setattr(log_mod, "_file_writing_disabled", True)
    try:
        assert log_mod._open_log_file("a")
        log_mod._log_file_handle.write("x" * 90)
        log_mod._log_file_handle.flush()
        log_mod._maybe_rotate_log_file(20)
        assert not (tmp_path / "log.txt.1").exists()
        assert log_file.stat().st_size == 90
    finally:
        log_mod._close_log_file()


def test_unlimited_when_zero(tmp_path, monkeypatch):
    log_file = tmp_path / "log.txt"
    monkeypatch.setattr(log_mod, "_cached_log_file", str(log_file))
    monkeypatch.setattr(log_mod, "_cached_log_max_bytes", 0)
    monkeypatch.setattr(log_mod, "_file_writing_disabled", True)
    try:
        assert log_mod._open_log_file("a")
        log_mod._log_file_handle.write("x" * 5000)
        log_mod._log_file_handle.flush()
        log_mod._maybe_rotate_log_file(5000)
        assert not (tmp_path / "log.txt.1").exists()
    finally:
        log_mod._close_log_file()
