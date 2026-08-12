import base64
import json

import httpx
import pytest

from scripts.antigravity_image_ab_demo import (
    AccountResult,
    RequestObservation,
    build_report,
    build_variant_schedule,
    build_wrapped_image_request,
    execute_image_request,
    extract_error_reason,
    extract_image_quota,
    fetch_quota_snapshot,
    summarize_observations,
    validate_api_base,
)


def test_manager_variant_changes_only_image_fingerprint_fields():
    common = {
        "prompt": "Generate a small blue circle on a white background",
        "project_id": "project-test",
        "request_id": "agent/conversation/1/trajectory/1",
        "session_id": "-123456789",
        "trajectory_id": "trajectory",
        "aspect_ratio": "1:1",
        "image_size": "512",
    }

    legacy = build_wrapped_image_request(variant="legacy", **common)
    manager = build_wrapped_image_request(variant="manager", **common)

    assert legacy["requestType"] == "agent"
    assert legacy["enabledCreditTypes"] == ["GOOGLE_ONE_AI"]
    assert manager["requestType"] == "image_gen"
    assert "enabledCreditTypes" not in manager

    normalized_legacy = dict(legacy)
    normalized_manager = dict(manager)
    normalized_legacy.pop("requestType")
    normalized_legacy.pop("enabledCreditTypes")
    normalized_manager.pop("requestType")
    assert normalized_legacy == normalized_manager

    generation_config = manager["request"]["generationConfig"]
    assert generation_config == {
        "responseModalities": ["IMAGE"],
        "candidateCount": 1,
        "imageConfig": {"aspectRatio": "1:1", "imageSize": "512"},
    }


def test_variant_schedule_is_interleaved_to_reduce_time_bias():
    assert build_variant_schedule(rounds=4) == [
        "legacy",
        "manager",
        "manager",
        "legacy",
        "legacy",
        "manager",
        "manager",
        "legacy",
    ]


def test_api_base_is_restricted_to_known_google_antigravity_hosts():
    assert (
        validate_api_base("https://daily-cloudcode-pa.googleapis.com/")
        == "https://daily-cloudcode-pa.googleapis.com"
    )
    assert (
        validate_api_base("https://daily-cloudcode-pa.sandbox.googleapis.com")
        == "https://daily-cloudcode-pa.sandbox.googleapis.com"
    )
    assert (
        validate_api_base("https://cloudcode-pa.googleapis.com")
        == "https://cloudcode-pa.googleapis.com"
    )

    with pytest.raises(ValueError, match="known Antigravity Google hosts"):
        validate_api_base("https://attacker.example")
    with pytest.raises(ValueError, match="HTTPS"):
        validate_api_base("http://daily-cloudcode-pa.googleapis.com")


def test_extract_image_quota_accepts_base_and_variant_model_keys():
    quota_payload = {
        "models": {
            "gemini-2.5-flash": {"quotaInfo": {"remainingFraction": 0.5}},
            "gemini-3.1-flash-image-2k": {
                "quotaInfo": {
                    "remainingFraction": 0.92,
                    "resetTime": "2026-08-07T00:00:00Z",
                }
            },
        }
    }

    assert extract_image_quota(quota_payload) == {
        "model": "gemini-3.1-flash-image-2k",
        "remaining": 0.92,
        "reset_time": "2026-08-07T00:00:00Z",
    }


def test_error_reason_extracts_capacity_reason_without_retaining_full_body():
    body = {
        "error": {
            "code": 503,
            "message": "The model is overloaded",
            "status": "UNAVAILABLE",
            "details": [{"reason": "MODEL_CAPACITY_EXHAUSTED"}],
        }
    }

    assert extract_error_reason(body) == "MODEL_CAPACITY_EXHAUSTED: The model is overloaded"


def test_summary_calculates_success_rate_latency_and_error_distribution():
    observations = [
        RequestObservation("legacy", 200, True, 4.0, 1, None),
        RequestObservation("legacy", 503, False, 1.0, 0, "MODEL_CAPACITY_EXHAUSTED"),
        RequestObservation("manager", 200, True, 3.0, 1, None),
        RequestObservation("manager", 200, True, 5.0, 1, None),
    ]

    summary = summarize_observations(observations)

    assert summary["legacy"]["attempts"] == 2
    assert summary["legacy"]["success_rate"] == pytest.approx(0.5)
    assert summary["legacy"]["average_seconds"] == pytest.approx(2.5)
    assert summary["legacy"]["status_counts"] == {"200": 1, "503": 1}
    assert summary["manager"]["success_rate"] == pytest.approx(1.0)
    assert summary["manager"]["p50_seconds"] == pytest.approx(3.0)
    assert summary["manager"]["p95_seconds"] == pytest.approx(5.0)


def test_report_never_contains_tokens_or_full_credential_paths():
    account = AccountResult(
        label="pro",
        credential_file="ag-account.json",
        project_id="able-courier",
        quota_before={"remaining": 1.0},
        quota_after={"remaining": 0.9},
        observations=[RequestObservation("manager", 200, True, 3.2, 1, None)],
    )

    report = build_report(
        accounts=[account],
        api_base="https://daily-cloudcode-pa.googleapis.com",
        model="gemini-3.1-flash-image",
        prompt="Generate a smoke-test image",
        rounds=1,
        warmup_rounds=0,
        started_at="2026-08-06T09:00:00+08:00",
        finished_at="2026-08-06T09:01:00+08:00",
    )
    serialized = json.dumps(report)

    assert "access-token-secret" not in serialized
    assert "/home/libre" not in serialized
    assert report["accounts"][0]["credential_file"] == "ag-account.json"
    assert report["accounts"][0]["summary"]["manager"]["attempts"] == 1


@pytest.mark.asyncio
async def test_quota_snapshot_sends_project_and_falls_back_without_it_on_403():
    request_bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_bodies.append(json.loads(request.content))
        if len(request_bodies) == 1:
            return httpx.Response(403, json={"error": {"message": "project rejected"}})
        return httpx.Response(
            200,
            json={
                "models": {
                    "gemini-3.1-flash-image": {
                        "quotaInfo": {"remainingFraction": 1.0}
                    }
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await fetch_quota_snapshot(
            client=client,
            api_base="https://daily-cloudcode-pa.googleapis.com",
            headers={"Authorization": "Bearer redacted"},
            project_id="project-test",
        )

    assert request_bodies == [{"project": "project-test"}, {}]
    assert snapshot == {
        "success": True,
        "status_code": 200,
        "model": "gemini-3.1-flash-image",
        "remaining": 1.0,
        "reset_time": None,
    }


@pytest.mark.asyncio
async def test_execute_image_request_counts_image_without_returning_base64():
    image = b"\x89PNG\r\n\x1a\n" + b"image-data"
    encoded = base64.b64encode(image).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["requestType"] == "image_gen"
        assert "enabledCreditTypes" not in payload
        return httpx.Response(
            200,
            json={
                "response": {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "inlineData": {
                                            "mimeType": "image/png",
                                            "data": encoded,
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            },
        )

    payload = build_wrapped_image_request(
        prompt="Generate an image",
        project_id="project-test",
        variant="manager",
        request_id="request-1",
        session_id="session-1",
        trajectory_id="trajectory-1",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        observation = await execute_image_request(
            client=client,
            api_base="https://daily-cloudcode-pa.googleapis.com",
            headers={"Authorization": "Bearer redacted"},
            payload=payload,
            variant="manager",
        )

    assert observation.success is True
    assert observation.image_count == 1
    assert observation.status_code == 200
    assert encoded not in json.dumps(observation.__dict__)
