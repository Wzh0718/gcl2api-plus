"""上游 modelVersion（真实执行模型）透传与逐请求记录对比的测试。

背景：上游把不同请求模型归一到同一物理后端（如 3.6/3.7/3.8 各档位实际
执行 gemini-3.8-flash-n），响应里的 modelVersion 是唯一可信来源。
本文件覆盖：
- extract_model_version 提取（顶层 / response 包装层 / 缺失）；
- OpenAI 非流式与流式响应、Anthropic 响应附加 modelVersion；
- call_records.upstream_model 列写入、读取与旧库迁移；
- BillingRecorder.record 将 upstream_model 透传到调用监控记录。
"""

import time

import aiosqlite
import pytest

from src.billing import extract_model_version


# ==================== extract_model_version ====================

def test_extract_model_version_top_level():
    payload = {"modelVersion": "gemini-3.8-flash-n", "candidates": []}
    assert extract_model_version(payload) == "gemini-3.8-flash-n"


def test_extract_model_version_response_wrapper():
    payload = {"response": {"modelVersion": "gemini-3.8-flash-tiered"}}
    assert extract_model_version(payload) == "gemini-3.8-flash-tiered"


def test_extract_model_version_missing_or_invalid():
    assert extract_model_version({}) is None
    assert extract_model_version({"modelVersion": ""}) is None
    assert extract_model_version({"modelVersion": 123}) is None
    assert extract_model_version("not-a-dict") is None
    assert extract_model_version(None) is None


# ==================== OpenAI 响应透传 ====================

def test_openai_non_stream_response_includes_model_version():
    from src.converter.openai2gemini import convert_gemini_to_openai_response

    gemini_response = {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": "hi"}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 2,
            "candidatesTokenCount": 9,
            "totalTokenCount": 11,
        },
        "modelVersion": "gemini-3.8-flash-n",
    }
    result = convert_gemini_to_openai_response(gemini_response, "gemini-3.7-flash-high", 200)
    # model 按协议回显请求名，modelVersion 透传真实后端
    assert result["model"] == "gemini-3.7-flash-high"
    assert result["modelVersion"] == "gemini-3.8-flash-n"


def test_openai_non_stream_response_without_model_version_omits_field():
    from src.converter.openai2gemini import convert_gemini_to_openai_response

    gemini_response = {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": "hi"}]}, "finishReason": "STOP"}
        ],
    }
    result = convert_gemini_to_openai_response(gemini_response, "gemini-3.7-flash-high", 200)
    assert "modelVersion" not in result


def test_openai_stream_chunk_includes_model_version():
    import json

    from src.converter.openai2gemini import convert_gemini_to_openai_stream

    chunk = {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": "Hello"}]}}
        ],
        "modelVersion": "gemini-3.8-flash-n",
    }
    out = convert_gemini_to_openai_stream(f"data: {json.dumps(chunk)}", "gemini-3.7-flash-high", "resp-1")
    assert out is not None
    payload = json.loads(out[len("data: "):])
    assert payload["model"] == "gemini-3.7-flash-high"
    assert payload["modelVersion"] == "gemini-3.8-flash-n"


def test_openai_stream_chunk_response_wrapper_model_version():
    import json

    from src.converter.openai2gemini import convert_gemini_to_openai_stream

    chunk = {
        "response": {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "Hello"}]}}
            ],
            "modelVersion": "gemini-3.8-flash-tiered",
        }
    }
    out = convert_gemini_to_openai_stream(f"data: {json.dumps(chunk)}", "m", "resp-1")
    payload = json.loads(out[len("data: "):])
    assert payload["modelVersion"] == "gemini-3.8-flash-tiered"


# ==================== Anthropic 响应透传 ====================

def test_anthropic_response_includes_model_version():
    from src.converter.anthropic2gemini import gemini_to_anthropic_response

    gemini_response = {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": "hi"}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 9},
        "modelVersion": "gemini-3.8-flash-n",
    }
    result = gemini_to_anthropic_response(gemini_response, "gemini-3.7-flash-high", 200)
    assert result["model"] == "gemini-3.7-flash-high"
    assert result["modelVersion"] == "gemini-3.8-flash-n"


def test_anthropic_response_wrapper_model_version():
    from src.converter.anthropic2gemini import gemini_to_anthropic_response

    gemini_response = {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": "hi"}]}, "finishReason": "STOP"}
        ],
        "response": {"modelVersion": "gemini-3.8-flash-n"},
    }
    result = gemini_to_anthropic_response(gemini_response, "m", 200)
    assert result["modelVersion"] == "gemini-3.8-flash-n"


def test_anthropic_response_without_model_version_omits_field():
    from src.converter.anthropic2gemini import gemini_to_anthropic_response

    gemini_response = {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": "hi"}]}, "finishReason": "STOP"}
        ],
    }
    result = gemini_to_anthropic_response(gemini_response, "m", 200)
    assert "modelVersion" not in result


# ==================== call_records.upstream_model ====================

async def _make_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage_adapter import StorageAdapter

    adapter = StorageAdapter()
    await adapter.initialize()
    return adapter


@pytest.mark.asyncio
async def test_call_record_upstream_model_roundtrip(tmp_path, monkeypatch):
    adapter = await _make_adapter(tmp_path, monkeypatch)
    backend = adapter._backend
    now = time.time()

    await backend.insert_call_record(
        request_id="u1", created_at=now,
        model_name="gemini-3.7-flash-high",
        upstream_model="gemini-3.8-flash-n",
        success=True,
    )
    await backend.insert_call_record(
        request_id="u2", created_at=now + 1,
        model_name="gemini-3.8-flash-tiered",
        success=True,
    )

    rows = await backend.list_call_records(since_ts=0)
    by_id = {row["request_id"]: row for row in rows}
    assert by_id["u1"]["upstream_model"] == "gemini-3.8-flash-n"
    # 未提供时为空字符串而非 NULL/缺失
    assert by_id["u2"]["upstream_model"] == ""
    await adapter.close()


@pytest.mark.asyncio
async def test_call_records_upstream_model_migration(tmp_path, monkeypatch):
    """旧库无 upstream_model 列时，初始化后自动补列且历史行默认为 ''。"""
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    import os

    os.makedirs(tmp_path, exist_ok=True)
    db_path = os.path.join(str(tmp_path), "credentials.db")

    # 手工建一份「旧版」call_records（无 upstream_model 列）并插入历史行
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE call_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                api_key_id TEXT NOT NULL DEFAULT 'env',
                credential_name TEXT NOT NULL DEFAULT '',
                channel TEXT NOT NULL DEFAULT 'antigravity',
                model_name TEXT NOT NULL DEFAULT '',
                task_type TEXT NOT NULL DEFAULT 'chat',
                success INTEGER NOT NULL DEFAULT 0,
                status_code INTEGER,
                gateway_seconds REAL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_tokens INTEGER NOT NULL DEFAULT 0,
                thought_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                total_cost TEXT NOT NULL DEFAULT '0.00000000',
                currency TEXT NOT NULL DEFAULT 'CNY'
            )
        """)
        await db.execute(
            "INSERT INTO call_records(request_id, created_at, model_name, success) VALUES ('legacy', 1.0, 'gemini-3-pro', 1)"
        )
        await db.commit()

    from src.storage_adapter import StorageAdapter

    adapter = StorageAdapter()
    await adapter.initialize()
    backend = adapter._backend

    rows = await backend.list_call_records(since_ts=0)
    assert rows[0]["request_id"] == "legacy"
    assert rows[0]["upstream_model"] == ""

    # 迁移后新行可以带 upstream_model
    await backend.insert_call_record(
        request_id="new", created_at=time.time(),
        model_name="gemini-3.7-flash-high",
        upstream_model="gemini-3.8-flash-n",
        success=True,
    )
    rows = await backend.list_call_records(since_ts=0)
    by_id = {row["request_id"]: row for row in rows}
    assert by_id["new"]["upstream_model"] == "gemini-3.8-flash-n"
    await adapter.close()


@pytest.mark.asyncio
async def test_billing_record_passes_upstream_model_to_call_monitor(tmp_path, monkeypatch):
    adapter = await _make_adapter(tmp_path, monkeypatch)
    from src.billing import get_billing_recorder
    from src.call_monitor import get_call_monitor

    recorder = await get_billing_recorder(adapter)
    monitor = await get_call_monitor(adapter)

    ok = await recorder.record(
        request_id="req-upstream",
        credential_name="acc-a.json",
        model="gemini-3.7-flash-high",
        usage_metadata={"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15},
        success=True,
        api_key_id="key-1",
        status_code=200,
        upstream_model="gemini-3.8-flash-n",
    )
    assert ok is True

    rows = await adapter._backend.list_call_records(since_ts=0)
    assert len(rows) == 1
    assert rows[0]["model_name"] == "gemini-3.7-flash-high"
    assert rows[0]["upstream_model"] == "gemini-3.8-flash-n"

    buffered = monitor.realtime()
    assert len(buffered) == 1
    assert buffered[0]["upstream_model"] == "gemini-3.8-flash-n"
    await adapter.close()
