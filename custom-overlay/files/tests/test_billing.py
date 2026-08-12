import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from src.billing import UsageMetrics, calculate_usage_metrics, calculate_costs


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_project_dotenv_loads_redis_url_without_overriding_runtime_env(tmp_path, monkeypatch):
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text('REDIS_URL="redis://dotenv.example:6379/0"\n', encoding="utf-8")
    copied_config = tmp_path / "config.py"
    copied_config.write_bytes((REPO_ROOT / "config.py").read_bytes())
    child_env = os.environ.copy()
    child_env.pop("REDIS_URL", None)

    completed = subprocess.run(
        [sys.executable, "-c", "import os, config; print(os.getenv('REDIS_URL', ''))"],
        cwd=tmp_path,
        env=child_env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert completed.stdout.strip() == "redis://dotenv.example:6379/0"

    from config import load_project_dotenv

    monkeypatch.delenv("REDIS_URL", raising=False)

    load_project_dotenv(dotenv_path)

    assert os.getenv("REDIS_URL") == "redis://dotenv.example:6379/0"

    dotenv_path.write_text('REDIS_URL="redis://changed.example:6379/0"\n', encoding="utf-8")
    monkeypatch.setenv("REDIS_URL", "redis://runtime.example:6379/0")

    load_project_dotenv(dotenv_path)

    assert os.getenv("REDIS_URL") == "redis://runtime.example:6379/0"


def test_usage_metrics_excludes_cached_tokens_and_keeps_thought_tokens():
    metrics = calculate_usage_metrics(
        {
            "promptTokenCount": 100,
            "cachedContentTokenCount": 25,
            "candidatesTokenCount": 40,
            "thoughtsTokenCount": 10,
            "totalTokenCount": 150,
        }
    )

    assert metrics == UsageMetrics(
        input_tokens=75,
        output_tokens=40,
        cache_tokens=25,
        thought_tokens=10,
        total_tokens=150,
        unknown_usage=False,
    )


def test_calculate_costs_uses_output_price_for_thoughts_and_rounds_to_eight_places():
    costs = calculate_costs(
        UsageMetrics(100, 200, 50, 25, 375, False),
        {"input_price": "1.23456789", "output_price": "2", "cache_price": "0.5"},
    )

    assert costs == {
        "input_cost": Decimal("0.00012346"),
        "output_cost": Decimal("0.00045000"),
        "cache_cost": Decimal("0.00002500"),
        "total_cost": Decimal("0.00059846"),
    }


def test_billing_prices_round_half_up_to_two_decimal_places():
    from src.panel.billing import _price_payload

    rounded = _price_payload("default", {
        "input_price": "1.235",
        "output_price": "2.234",
        "cache_price": "0.005",
        "currency": "CNY",
    })

    assert rounded == {
        "model": "default",
        "input_price": "1.24",
        "output_price": "2.23",
        "cache_price": "0.01",
        "currency": "CNY",
    }


@pytest.mark.asyncio
async def test_billing_recorder_deduplicates_request_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage.sqlite_manager import SQLiteManager
    from src.billing import BillingRecorder

    storage = SQLiteManager()
    await storage.initialize()
    recorder = BillingRecorder(storage)
    await recorder.record(
        request_id="req-1",
        credential_name="a.json",
        model="gemini-2.5-flash",
        usage_metadata={"promptTokenCount": 10, "candidatesTokenCount": 5},
        success=True,
    )
    await recorder.record(
        request_id="req-1",
        credential_name="a.json",
        model="gemini-2.5-flash",
        usage_metadata={"promptTokenCount": 10, "candidatesTokenCount": 5},
        success=True,
    )

    summary = await storage.get_billing_summary("today")
    assert summary["total_tokens"] == 15
    assert summary["success_count"] == 1
    await storage.close()


@pytest.mark.asyncio
async def test_billing_recorder_concurrent_duplicate_is_counted_once(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage.sqlite_manager import SQLiteManager
    from src.billing import BillingRecorder

    storage = SQLiteManager()
    await storage.initialize()
    recorder = BillingRecorder(storage)
    recorded = await asyncio.gather(*(
        recorder.record(
            request_id="same-request",
            credential_name="a.json",
            model="m",
            usage_metadata={"promptTokenCount": 10},
            success=True,
        )
        for _ in range(12)
    ))

    assert recorded.count(True) == 1
    summary = await storage.get_billing_summary("today")
    assert summary["input_tokens"] == 10
    assert summary["success_count"] == 1
    await storage.close()


@pytest.mark.asyncio
async def test_price_changes_only_affect_future_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage.sqlite_manager import SQLiteManager
    from src.billing import BillingRecorder

    storage = SQLiteManager()
    await storage.initialize()
    await storage.upsert_billing_price("default", "1", "0", "0", "CNY")
    recorder = BillingRecorder(storage)
    await recorder.record(
        request_id="priced-1", credential_name="a.json", model="m",
        usage_metadata={"promptTokenCount": 1_000_000}, success=True,
    )
    await storage.upsert_billing_price("default", "2", "0", "0", "CNY")
    await recorder.invalidate_price_cache()
    await recorder.record(
        request_id="priced-2", credential_name="a.json", model="m",
        usage_metadata={"promptTokenCount": 1_000_000}, success=True,
    )

    summary = await storage.get_billing_summary("today")
    assert summary["input_cost"] == "3.00000000"
    assert summary["total_cost"] == "3.00000000"
    await storage.close()


@pytest.mark.asyncio
async def test_recalculate_costs_updates_history_with_new_model_price(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage.sqlite_manager import SQLiteManager
    from src.billing import BillingRecorder

    storage = SQLiteManager()
    await storage.initialize()
    await storage.upsert_billing_price("default", "1", "0", "0", "CNY")
    recorder = BillingRecorder(storage)
    await recorder.record(
        request_id="reprice-1", credential_name="a.json", model="m",
        usage_metadata={"promptTokenCount": 2_000_000}, success=True,
    )

    await storage.upsert_billing_price("m", "3", "0", "0", "CNY")
    await recorder.invalidate_price_cache()
    updated = await recorder.recalculate_costs("m")

    assert updated == 1
    summary = await storage.get_billing_summary("today")
    assert summary["input_cost"] == "6.00000000"
    assert summary["total_cost"] == "6.00000000"
    await storage.close()


@pytest.mark.asyncio
async def test_recalculate_costs_default_only_touches_models_without_own_price(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage.sqlite_manager import SQLiteManager
    from src.billing import BillingRecorder

    storage = SQLiteManager()
    await storage.initialize()
    await storage.upsert_billing_price("default", "1", "0", "0", "CNY")
    await storage.upsert_billing_price("special", "10", "0", "0", "CNY")
    recorder = BillingRecorder(storage)
    await recorder.record(
        request_id="reprice-m", credential_name="a.json", model="m",
        usage_metadata={"promptTokenCount": 1_000_000}, success=True,
    )
    await recorder.record(
        request_id="reprice-special", credential_name="a.json", model="special",
        usage_metadata={"promptTokenCount": 1_000_000}, success=True,
    )

    await storage.upsert_billing_price("default", "2", "0", "0", "CNY")
    await recorder.invalidate_price_cache()
    updated = await recorder.recalculate_costs(None)

    assert updated == 1
    summary = await storage.get_billing_summary("today")
    # m: 1M tokens * 2 = 2；special 有独立价格 10，不受 default 变更影响
    assert summary["input_cost"] == "12.00000000"
    assert summary["total_cost"] == "12.00000000"
    await storage.close()


@pytest.mark.asyncio
async def test_recalculate_costs_rebuilds_api_key_quota_used(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage.sqlite_manager import SQLiteManager
    from src.billing import BillingRecorder

    storage = SQLiteManager()
    await storage.initialize()
    await storage.upsert_billing_price("default", "1", "0", "0", "CNY")
    await storage.create_api_key_record(
        id="key-1", name="test", key_prefix="pref",
        secret_hash="hash", secret_ciphertext="cipher",
        quota_amount=None, currency="CNY", reset_mode="monthly",
        quota_period_start="2000-01-01", now=0,
    )
    recorder = BillingRecorder(storage)
    await recorder.record(
        request_id="reprice-quota", credential_name="a.json", model="m",
        usage_metadata={"promptTokenCount": 1_000_000}, success=True,
        api_key_id="key-1",
    )
    record = await storage.get_api_key_record("key-1")
    assert record["quota_used"] == "1.00000000"

    await storage.upsert_billing_price("default", "5", "0", "0", "CNY")
    await recorder.invalidate_price_cache()
    await recorder.recalculate_costs(None)

    record = await storage.get_api_key_record("key-1")
    assert record["quota_used"] == "5.00000000"
    await storage.close()


@pytest.mark.asyncio
async def test_expired_request_id_can_be_recorded_again(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage.sqlite_manager import SQLiteManager
    from src.billing import BillingRecorder

    storage = SQLiteManager()
    await storage.initialize()
    recorder = BillingRecorder(storage)
    assert await recorder.record(
        request_id="expiring", credential_name="a.json", model="m",
        usage_metadata={"promptTokenCount": 1}, success=True,
    )
    import aiosqlite
    async with aiosqlite.connect(storage._db_path) as db:
        await db.execute("UPDATE billing_request_dedupe SET expires_at = 0 WHERE request_id = 'expiring'")
        await db.commit()
    assert await recorder.record(
        request_id="expiring", credential_name="a.json", model="m",
        usage_metadata={"promptTokenCount": 2}, success=True,
    )
    summary = await storage.get_billing_summary("today")
    assert summary["input_tokens"] == 3
    assert summary["success_count"] == 2
    await storage.close()


@pytest.mark.asyncio
async def test_redis_mirror_uses_integer_counters_and_never_blocks_sqlite_on_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    monkeypatch.setenv("BILLING_REDIS_PREFIX", "test:billing")
    from src.storage.sqlite_manager import SQLiteManager
    from src.billing import BillingRecorder

    class FakeRedis:
        def __init__(self):
            self.values = {}

        async def hincrby(self, key, field, value):
            self.values[(key, field)] = self.values.get((key, field), 0) + value

        async def expire(self, key, ttl):
            self.values[(key, "ttl")] = ttl

    storage = SQLiteManager()
    await storage.initialize()
    fake = FakeRedis()
    recorder = BillingRecorder(storage, redis_client=fake)
    assert await recorder.record(
        request_id="redis-ok", credential_name="a.json", model="m",
        usage_metadata={"promptTokenCount": 10, "candidatesTokenCount": 5}, success=True,
    )
    assert any(key.startswith("test:billing:v2:") for key, _ in fake.values)
    assert any(":env:" in key for key, _ in fake.values)
    assert any(field == "total_tokens" and value == 15 for (_, field), value in fake.values.items())
    assert any(field == "success_count" and value == 1 for (_, field), value in fake.values.items())

    async def fail(*args, **kwargs):
        raise ConnectionError("redis://user:secret@example.invalid")

    fake.hincrby = fail
    assert await recorder.record(
        request_id="redis-fail", credential_name="a.json", model="m",
        usage_metadata={"promptTokenCount": 1}, success=True,
    )
    summary = await storage.get_billing_summary("today")
    assert summary["total_tokens"] == 16
    await storage.close()


@pytest.mark.asyncio
async def test_without_redis_url_no_redis_client_is_created(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    from src.billing import _RedisBillingMirror

    mirror = _RedisBillingMirror()
    assert await mirror._get_client() is None
    assert mirror.client is None


@pytest.mark.asyncio
async def test_redis_rebuild_reads_sqlite_in_bounded_pages():
    from src.billing import BillingRecorder

    row = {
        "billing_date": "2026-07-27", "api_key_id": "key-1",
        "credential_name": "a.json", "model": "m", "currency": "CNY",
        "input_tokens": 1, "output_tokens": 2, "cache_tokens": 3, "thought_tokens": 4, "total_tokens": 10,
        "input_cost": "0.00000001", "output_cost": "0.00000002", "cache_cost": "0.00000003", "total_cost": "0.00000006",
        "success_count": 1, "failed_count": 0, "unknown_usage_count": 0,
    }

    class FakeStorage:
        def __init__(self):
            self.pages = []

        async def list_billing_daily(self, days, page, page_size):
            self.pages.append((days, page, page_size))
            if page == 1:
                return [row] * 500
            if page == 2:
                return [row]
            return []

    class FakeRedis:
        def __init__(self):
            self.writes = 0

        async def hset(self, key, mapping):
            self.writes += 1

        async def expire(self, key, ttl):
            return None

    storage = FakeStorage()
    redis = FakeRedis()
    recorder = BillingRecorder(storage, redis_client=redis)

    assert await recorder.rebuild_redis(45) == 501
    assert storage.pages == [(45, 1, 500), (45, 2, 500)]
    assert redis.writes == 501


@pytest.mark.asyncio
async def test_redis_ready_marker_failure_does_not_break_sqlite_rebuild():
    from src.billing import BillingRecorder

    class FakeStorage:
        async def list_billing_daily(self, days, page, page_size):
            return []

    class FakeRedis:
        async def set(self, key, value):
            raise ConnectionError("redis unavailable")

    recorder = BillingRecorder(FakeStorage(), redis_client=FakeRedis())

    assert await recorder.rebuild_redis() == 0


@pytest.mark.asyncio
async def test_redis_dashboard_aggregates_usage_in_one_read_path(monkeypatch):
    from src.billing import BillingRecorder, SHANGHAI

    billing_date = datetime.now(SHANGHAI).date().isoformat()
    monkeypatch.setenv("BILLING_REDIS_PREFIX", "test:billing")

    class FakeRedis:
        def __init__(self):
            self.values = {
                f"test:billing:v2:{billing_date}:CNY:key-a:a.json:gemini-flash": {
                    "billing_date": billing_date,
                    "api_key_id": "key-a",
                    "credential_name": "a.json",
                    "model": "gemini-flash",
                    "currency": "CNY",
                    "input_tokens": "400000",
                    "output_tokens": "100000",
                    "cache_tokens": "0",
                    "thought_tokens": "0",
                    "total_tokens": "500000",
                    "input_cost_e8": "40000000",
                    "output_cost_e8": "20000000",
                    "cache_cost_e8": "0",
                    "total_cost_e8": "60000000",
                    "success_count": "2",
                    "failed_count": "0",
                    "unknown_usage_count": "0",
                },
                f"test:billing:v2:{billing_date}:CNY:key-b:b.json:gemini-pro": {
                    "billing_date": billing_date,
                    "api_key_id": "key-b",
                    "credential_name": "b.json",
                    "model": "gemini-pro",
                    "currency": "CNY",
                    "input_tokens": "700000",
                    "output_tokens": "300000",
                    "cache_tokens": "250000",
                    "thought_tokens": "50000",
                    "total_tokens": "1300000",
                    "input_cost_e8": "70000000",
                    "output_cost_e8": "70000000",
                    "cache_cost_e8": "10000000",
                    "total_cost_e8": "150000000",
                    "success_count": "3",
                    "failed_count": "1",
                    "unknown_usage_count": "1",
                },
            }
            self.scan_calls = 0

        async def scan_iter(self, match, count):
            self.scan_calls += 1
            prefix = match.removesuffix("*")
            for key in self.values:
                if key.startswith(prefix):
                    yield key

        async def hgetall(self, key):
            return self.values[key]

    redis = FakeRedis()
    recorder = BillingRecorder(object(), redis_client=redis)

    dashboard = await recorder.get_redis_dashboard("today", page_size=10)

    assert redis.scan_calls == 1
    assert dashboard["source"] == "redis"
    assert dashboard["summary"]["total_tokens"] == 1_800_000
    assert dashboard["summary"]["total_cost"] == "2.10000000"
    assert dashboard["summary"]["daily_trend"] == [
        {"billing_date": billing_date, "total_tokens": 1_800_000, "total_cost": "2.10000000"}
    ]
    assert [item["credential_name"] for item in dashboard["accounts"]["items"]] == ["b.json", "a.json"]
    assert [item["model"] for item in dashboard["models"]["items"]] == ["gemini-pro", "gemini-flash"]
    assert [item["api_key_id"] for item in dashboard["keys"]["items"]] == ["key-b", "key-a"]

    filtered = await recorder.get_redis_dashboard("today", page_size=10, api_key_id="key-a")

    assert filtered["summary"]["total_tokens"] == 500_000
    assert filtered["accounts"]["items"][0]["credential_name"] == "a.json"
    assert filtered["models"]["items"][0]["model"] == "gemini-flash"
    assert [item["api_key_id"] for item in filtered["keys"]["items"]] == ["key-b", "key-a"]


@pytest.mark.asyncio
async def test_billing_dashboard_falls_back_to_sqlite_when_redis_is_unavailable(monkeypatch):
    from src.billing import BillingRedisUnavailable
    from src.panel import billing as billing_panel
    import src.billing as billing_module

    class FakeStorage:
        async def get_billing_summary(self, range_name, api_key_id=None):
            assert api_key_id == "key-a"
            return {"range": range_name, "total_tokens": 123}

        async def get_billing_accounts(self, range_name, page, page_size, api_key_id=None):
            assert api_key_id == "key-a"
            return {
                "items": [{"credential_name": "account.json"}],
                "range": range_name,
                "page": page,
                "page_size": page_size,
            }

        async def get_billing_models(self, range_name, page, page_size, api_key_id=None):
            assert api_key_id == "key-a"
            return {
                "items": [{"model": "gemini-test"}],
                "range": range_name,
                "page": page,
                "page_size": page_size,
            }

        async def get_billing_keys(self, range_name, page, page_size):
            return {
                "items": [{"api_key_id": "key-a"}],
                "range": range_name,
                "page": page,
                "page_size": page_size,
            }

    class FakeRecorder:
        async def get_redis_dashboard(self, range_name, page_size, api_key_id=None):
            assert api_key_id == "key-a"
            raise BillingRedisUnavailable("Redis 计费镜像不可用")

    storage = FakeStorage()

    async def fake_get_storage_adapter():
        return storage

    async def fake_get_billing_recorder(received_storage):
        assert received_storage is storage
        return FakeRecorder()

    monkeypatch.setattr(billing_panel, "get_storage_adapter", fake_get_storage_adapter)
    monkeypatch.setattr(billing_module, "get_billing_recorder", fake_get_billing_recorder)

    dashboard = await billing_panel.billing_dashboard(
        range="7d", page_size=999, api_key_id="key-a", _token="test-token"
    )

    assert dashboard == {
        "source": "sqlite",
        "degraded": True,
        "scanned_keys": 0,
        "summary": {"range": "7d", "total_tokens": 123},
        "accounts": {
            "items": [{"credential_name": "account.json"}],
            "range": "7d",
            "page": 1,
            "page_size": 100,
        },
        "models": {
            "items": [{"model": "gemini-test"}],
            "range": "7d",
            "page": 1,
            "page_size": 100,
        },
        "keys": {
            "items": [{"api_key_id": "key-a"}],
            "range": "7d",
            "page": 1,
            "page_size": 100,
        },
    }


def _run_common_js_slice(start_marker: str, end_marker: str, setup: str, expression: str):
    source = (REPO_ROOT / "front" / "common.js").read_text(encoding="utf-8")
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    script = f"{setup}\n{source[start:end]}\n{expression}"
    result = subprocess.run(
        ["node", "-e", script], cwd=REPO_ROOT, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    assert result.returncode == 0, result.stdout
    return json.loads(result.stdout)


def test_billing_token_display_always_uses_millions():
    values = _run_common_js_slice(
        "function formatBillingNumber",
        "function formatBillingCost",
        "",
        "console.log(JSON.stringify([formatBillingNumber(0), formatBillingNumber(520000), formatBillingNumber(1000000), formatBillingNumber(1250000)]));",
    )

    assert values == ["0.000M", "0.520M", "1.000M", "1.250M"]


def test_billing_cost_display_rounds_to_two_decimal_places():
    values = _run_common_js_slice(
        "function formatBillingCost",
        "function billingEmpty",
        "let billingCurrency = 'CNY';",
        "console.log(JSON.stringify([formatBillingCost('1.235'), formatBillingCost('1.234', 'USD')]));",
    )

    assert values == ["1.24 CNY", "1.23 USD"]

    api_key_values = _run_common_js_slice(
        "function formatApiKeyAmount",
        "function formatApiKeyDate",
        "let billingCurrency = 'CNY';",
        "console.log(JSON.stringify([formatApiKeyAmount('1.235'), formatApiKeyAmount('1.234', 'USD')]));",
    )
    assert api_key_values == ["1.24 CNY", "1.23 USD"]


def test_antigravity_quota_percentage_preserves_fractional_precision():
    values = _run_common_js_slice(
        "function quotaPercentageValue",
        "async function toggleAntigravityQuotaDetails",
        "",
        "console.log(JSON.stringify([quotaPercentageValue(0.998642), formatQuotaPercentage(0.998642), formatQuotaPercentage(0.9893502), formatQuotaPercentage(0.999999999), formatQuotaPercentage(1)]));",
    )

    assert values == [99.8642, "99.86%", "98.93%", "99.99%", "100%"]


def test_key_quota_usage_displays_tokens_and_spend_together():
    source = (REPO_ROOT / "front" / "common.js").read_text(encoding="utf-8")

    assert "formatBillingNumber(item.quota_tokens_used)" in source
    assert ">已用额度</span>" in source
    assert "Tokens</strong>" in source
    assert "花费 ${escapeHtml(formatApiKeyAmount(item.quota_used, item.currency))}" in source


def test_billing_key_selection_switches_to_exact_quota_period():
    values = _run_common_js_slice(
        "function billingRangeAfterApiKeySelection",
        "function syncBillingQuotaPeriodControl",
        "",
        "console.log(JSON.stringify([billingRangeAfterApiKeySelection('today', 'key-a'), billingRangeAfterApiKeySelection('quota', '')]));",
    )

    assert values == ["quota", "today"]


def test_quota_period_dashboard_uses_key_management_counters():
    dashboard = _run_common_js_slice(
        "function buildBillingQuotaPeriodDashboard",
        "async function loadBillingDashboard",
        "",
        "console.log(JSON.stringify(buildBillingQuotaPeriodDashboard({id: 'key-a', currency: 'CNY', quota_tokens_used: 1520000, quota_used: '1.52000000', quota_remaining: '8.48000000', quota_period_start: '2026-07-01', reset_mode: 'monthly', kind: 'managed'})));",
    )

    assert dashboard["summary"]["range"] == "quota"
    assert dashboard["summary"]["total_tokens"] == 1_520_000
    assert dashboard["summary"]["total_cost"] == "1.52000000"
    assert dashboard["quota_item"]["quota_remaining"] == "8.48000000"
    assert dashboard["keys"]["items"] == [{
        "api_key_id": "key-a",
        "currency": "CNY",
        "total_tokens": 1_520_000,
        "total_cost": "1.52000000",
    }]


def test_billing_auto_refresh_keeps_one_visible_ten_second_timer():
    state = _run_common_js_slice(
        "function stopBillingAutoRefresh",
        "function formatBillingNumber",
        """
const AppState = { billingRefreshInterval: null };
let nextTimerId = 0;
let cleared = [];
let scheduled = [];
let refreshes = 0;
let billingRange = 'today';
const document = {
  visibilityState: 'visible',
  getElementById: (id) => id === 'billingRange'
    ? { value: billingRange }
    : { classList: { contains: () => true } }
};
function clearInterval(id) { cleared.push(id); }
function setInterval(callback, delay) { scheduled.push({ callback, delay }); return ++nextTimerId; }
async function loadBillingDashboard(options) { refreshes += options && options.silent ? 1 : 100; }
""",
        """
startBillingAutoRefresh();
startBillingAutoRefresh();
scheduled.at(-1).callback();
billingRange = 'quota';
scheduled.at(-1).callback();
setTimeout(() => console.log(JSON.stringify({delay: scheduled.at(-1).delay, timers: scheduled.length, cleared, refreshes, active: AppState.billingRefreshInterval})), 0);
""",
    )

    assert state == {"delay": 10000, "timers": 2, "cleared": [1], "refreshes": 1, "active": 2}


@pytest.mark.parametrize("filename", ["control_panel.html", "control_panel_mobile.html"])
def test_panel_defaults_to_key_distribution_and_hides_gcli_navigation(filename):
    html = (REPO_ROOT / "front" / filename).read_text(encoding="utf-8")

    if filename == "control_panel.html":
        assert (
            '<button type="button" class="tab active" aria-current="page" '
            'onclick="switchTab(\'api-keys\', this)">密钥分发</button>'
        ) in html
    else:
        assert '<button class="tab active" onclick="switchTab(\'api-keys\')">密钥分发管理</button>' in html
    assert "switchTab('oauth')" not in html
    assert "switchTab('manage')" not in html
    assert 'id="api-keysTab" class="tab-content active"' in html
    assert 'id="apiKeyCreateForm"' in html
    assert 'id="apiKeyList"' in html
    assert 'id="billingApiKeyFilter"' in html
    assert 'id="billingKeys"' in html
    assert 'onchange="handleBillingApiKeyChange()"' in html
    assert '<option value="quota">额度周期</option>' in html
    if filename == "control_panel.html":
        assert 'id="billingQuotaRangeButton"' in html
    assert "密钥只展示这一次" not in html
    assert 'id="apiKeySecretPanel"' not in html


def test_key_distribution_list_supports_copying_keys_after_creation():
    source = (REPO_ROOT / "front" / "common.js").read_text(encoding="utf-8")

    assert "async function copyApiKey(apiKeyId)" in source
    assert "复制密钥" in source
    assert "./api-keys/${encodeURIComponent(apiKeyId)}/secret" in source
    assert "copyCreatedApiKey" not in source
    assert "请立即复制保存" not in source
