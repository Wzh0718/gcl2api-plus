"""One schema for environment, panel validation and effective capture settings."""
from __future__ import annotations

import os
from pathlib import Path

from src.storage.request_capture_store import POLICIES


def _field(default, label, minimum=None, maximum=None):
    return {"default": default, "label": label, "min": minimum, "max": maximum,
            "type": "boolean" if isinstance(default, bool) else "integer"}


CAPTURE_FIELDS = {
    "request_capture_enabled": _field(False, "启用请求证据采集"),
    "request_capture_min_free_mb": _field(64, "全部采集停止写入的剩余空间（MiB）", 0, 1048576),
    "request_capture_max_entry_kb": _field(256, "单条证据上限（KiB）", 1, 4096),
    "request_capture_max_body_kb": _field(4096, "入站正文观察上限（KiB）", 1, 16384),
    "request_capture_error_body_kb": _field(16, "错误响应正文上限（KiB）", 1, 256),
    "request_capture_context_count": _field(5, "异常前成功样本数", 0, 50),
    "request_capture_cleanup_seconds": _field(60, "空闲清理间隔（秒）", 5, 3600),
    "request_capture_export_max_mb": _field(16, "单次导出上限（MiB）", 1, 128),
}
for _name, _label, _mb, _hours in (
    ("attempts", "上游尝试与最终结果", 256, 336),
    ("features", "请求特征", 256, 336),
    ("normal", "普通请求正文", 2048, 24),
    ("errors", "异常证据与成功对照", 1024, 168),
):
    CAPTURE_FIELDS[f"request_capture_{_name}_enabled"] = _field(True, f"采集{_label}")
    CAPTURE_FIELDS[f"request_capture_{_name}_max_mb"] = _field(_mb, f"{_label}容量上限（MiB）", 1, 1048576)
    CAPTURE_FIELDS[f"request_capture_{_name}_retention_hours"] = _field(_hours, f"{_label}保留时长（小时）", 1, 8760)
    CAPTURE_FIELDS[f"request_capture_{_name}_min_free_mb"] = _field(
        {"attempts": 64, "features": 64, "normal": 1024, "errors": 256}[_name],
        f"{_label}暂停写入的剩余空间（MiB）", 0, 1048576)


def validate_capture_value(key, value):
    field = CAPTURE_FIELDS[key]
    if field["type"] == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false", "1", "0", "on", "off", "yes", "no"}:
            return value.lower() in {"true", "1", "on", "yes"}
        raise ValueError(f"{key} 必须是布尔值")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{key} 必须是整数")
    try:
        number = int(value)
    except (ValueError, TypeError):
        raise ValueError(f"{key} 必须是整数") from None
    if not field["min"] <= number <= field["max"]:
        raise ValueError(f"{key} 范围为 {field['min']}–{field['max']}")
    return number


def capture_settings():
    import config
    result = {}
    for key, field in CAPTURE_FIELDS.items():
        value = os.getenv(key.upper())
        if value is None or value == "":
            value = config._get_cached_config(key, field["default"])
        try:
            result[key] = validate_capture_value(key, value)
        except ValueError:
            # Invalid environment values fail closed for the master switch;
            # numeric settings retain finite, documented defaults.
            result[key] = field["default"]
    return result


def capture_policies(settings=None):
    settings = capture_settings() if settings is None else settings
    return {name: {"enabled": settings[f"request_capture_{name}_enabled"],
                   "max_bytes": settings[f"request_capture_{name}_max_mb"] * 1024**2,
                   "retention_hours": settings[f"request_capture_{name}_retention_hours"],
                   "min_free_bytes": max(settings["request_capture_min_free_mb"],
                                         settings[f"request_capture_{name}_min_free_mb"]) * 1024**2}
            for name in POLICIES}


def capture_directory():
    # Environment-only path: API callers cannot write outside the configured
    # credentials volume. All other controls support panel hot reload.
    return Path(os.getenv("REQUEST_CAPTURE_DIR") or
                Path(os.getenv("CREDENTIALS_DIR", "./creds")) / "request-capture").resolve()
