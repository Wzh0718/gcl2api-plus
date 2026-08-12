import base64
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from scripts.banana_image_stress import (
    BenchmarkConfig,
    RequestResult,
    build_argument_parser,
    build_http_request,
    build_report_document,
    config_from_args,
    execute_image_request,
    extract_response_images,
    image_dimensions,
    parse_concurrency_levels,
    render_html_report,
    run_benchmark,
    run_stage,
    summarize_stage,
    write_report_files,
)


def png_stub(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
    )


def config(**overrides) -> BenchmarkConfig:
    values = {
        "api_key": "stress-secret-value",
        "api_base": "https://images.example.test/antigravity/v1",
        "protocol": "openai",
        "model": "gemini-3.1-flash-image",
        "prompt": "Generate a small benchmark image",
        "concurrency_levels": (1, 2),
        "stage_seconds": 30.0,
        "request_timeout_seconds": 120.0,
        "size": "512x512",
        "aspect_ratio": "1:1",
        "image_size": "512",
        "images_per_request": 1,
        "target_rpm": 0.0,
        "max_requests_per_stage": 0,
        "minimum_success_rate": 0.95,
        "max_consecutive_failures": 10,
        "cooldown_seconds": 0.0,
    }
    values.update(overrides)
    return BenchmarkConfig(**values)


def result(
    sequence: int,
    *,
    duration: float,
    status: int = 200,
    images: int = 1,
    error: str | None = None,
) -> RequestResult:
    return RequestResult(
        sequence=sequence,
        started_offset_seconds=float(sequence),
        duration_seconds=duration,
        status_code=status,
        success=status == 200 and images > 0,
        image_count=images,
        image_bytes=images * 100,
        dimensions=((512, 512),) if images else (),
        mime_types=("image/png",) if images else (),
        error=error,
    )


def test_parse_concurrency_levels_preserves_order_and_rejects_unsafe_values():
    assert parse_concurrency_levels("1,2,4,8") == (1, 2, 4, 8)

    with pytest.raises(ValueError, match="正整数"):
        parse_concurrency_levels("1,0,4")
    with pytest.raises(ValueError, match="重复"):
        parse_concurrency_levels("1,2,2")
    with pytest.raises(ValueError, match="256"):
        parse_concurrency_levels("257")


def test_build_openai_request_uses_images_generation_shape():
    endpoint, headers, payload = build_http_request(config(images_per_request=2))

    assert endpoint == "https://images.example.test/antigravity/v1/images/generations"
    assert headers["Authorization"] == "Bearer stress-secret-value"
    assert "stress-secret-value" not in json.dumps(payload)
    assert payload == {
        "model": "gemini-3.1-flash-image",
        "prompt": "Generate a small benchmark image",
        "n": 2,
        "size": "512x512",
        "response_format": "b64_json",
        "output_format": "jpeg",
        "quality": "auto",
        "background": "auto",
        "moderation": "auto",
    }


def test_build_google_request_uses_official_image_parameters():
    endpoint, headers, payload = build_http_request(
        config(protocol="google", aspect_ratio="16:9", image_size="2K")
    )

    assert endpoint.endswith("/models/gemini-3.1-flash-image:generateContent")
    assert headers["x-goog-api-key"] == "stress-secret-value"
    assert payload["generationConfig"] == {
        "responseModalities": ["IMAGE"],
        "responseFormat": {"image": {"aspectRatio": "16:9", "imageSize": "2K"}},
    }


def test_google_protocol_rejects_multiple_images_per_request():
    with pytest.raises(ValueError, match="images_per_request=1"):
        config(protocol="google", images_per_request=2)


@pytest.mark.parametrize(
    "api_base",
    [
        "https://user:password@images.example.test/antigravity/v1",
        "https://images.example.test/antigravity/v1?key=secret",
        "https://images.example.test/antigravity/v1#secret",
    ],
)
def test_config_rejects_credentials_query_and_fragment_in_api_base(api_base: str):
    with pytest.raises(ValueError, match="敏感信息"):
        config(api_base=api_base)


def test_extract_images_and_dimensions_from_both_protocols():
    image = png_stub(640, 480)
    encoded = base64.b64encode(image).decode("ascii")

    openai_images = extract_response_images({"data": [{"b64_json": encoded}]}, protocol="openai")
    google_images = extract_response_images(
        {
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
        protocol="google",
    )

    assert openai_images == [("image/png", image)]
    assert google_images == [("image/png", image)]
    assert image_dimensions(image, "image/png") == (640, 480)


def test_summarize_stage_calculates_image_rpm_and_latency_percentiles():
    summary = summarize_stage(
        concurrency=4,
        configured_seconds=60.0,
        elapsed_seconds=30.0,
        results=[
            result(1, duration=1.0),
            result(2, duration=2.0),
            result(3, duration=3.0, images=2),
            result(4, duration=4.0, status=429, images=0, error="rate limited"),
        ],
        minimum_success_rate=0.70,
    )

    assert summary.attempts == 4
    assert summary.successful_requests == 3
    assert summary.image_count == 4
    assert summary.request_rpm == pytest.approx(8.0)
    assert summary.image_rpm == pytest.approx(8.0)
    assert summary.success_rate == pytest.approx(0.75)
    assert summary.average_seconds == pytest.approx(2.5)
    assert summary.p50_seconds == pytest.approx(2.0)
    assert summary.p95_seconds == pytest.approx(4.0)
    assert summary.status_counts == {"200": 3, "429": 1}
    assert summary.stable is False


def test_stage_summary_limits_unique_error_messages_to_twenty():
    summary = summarize_stage(
        concurrency=1,
        configured_seconds=60.0,
        elapsed_seconds=60.0,
        results=[
            result(
                index,
                duration=1.0,
                status=500,
                images=0,
                error=f"unique error {index}",
            )
            for index in range(30)
        ],
        minimum_success_rate=0.95,
    )

    assert len(summary.error_counts) == 20


@pytest.mark.asyncio
async def test_execute_request_counts_images_without_retaining_base64():
    image = png_stub(512, 512)
    encoded = base64.b64encode(image).decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer stress-secret-value"
        return httpx.Response(200, json={"data": [{"b64_json": encoded}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        request_result = await execute_image_request(
            client=client,
            benchmark_config=config(),
            sequence=7,
            benchmark_started_monotonic=10.0,
            monotonic=lambda: 12.0,
        )

    assert request_result.success is True
    assert request_result.image_count == 1
    assert request_result.image_bytes == len(image)
    assert request_result.dimensions == ((512, 512),)
    assert request_result.duration_seconds == pytest.approx(0.0)
    assert not hasattr(request_result, "image_data")


@pytest.mark.asyncio
async def test_execute_request_redacts_key_from_error_details():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"message": "blocked stress-secret-value"}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        request_result = await execute_image_request(
            client=client,
            benchmark_config=config(),
            sequence=1,
            benchmark_started_monotonic=10.0,
            monotonic=lambda: 11.0,
        )

    assert request_result.success is False
    assert request_result.status_code == 429
    assert "stress-secret-value" not in (request_result.error or "")
    assert "[REDACTED]" in (request_result.error or "")


def test_html_report_is_self_contained_and_never_contains_api_key_or_raw_html():
    benchmark_config = config(prompt='<script>alert("x")</script>')
    stage = summarize_stage(
        concurrency=1,
        configured_seconds=30.0,
        elapsed_seconds=30.0,
        results=[result(1, duration=2.0)],
        minimum_success_rate=0.95,
    )
    document = build_report_document(
        benchmark_config=benchmark_config,
        stages=[stage],
        started_at=datetime(2026, 7, 29, 8, 0, tzinfo=UTC),
        finished_at=datetime(2026, 7, 29, 8, 1, tzinfo=UTC),
    )

    html = render_html_report(document)

    assert "stress-secret-value" not in json.dumps(document)
    assert "results" not in document["stages"][0]
    assert "stress-secret-value" not in html
    assert '<script>alert("x")</script>' not in html
    assert "&lt;script&gt;alert" in html
    assert "<svg" in html
    assert "图片 RPM" in html
    assert "平均耗时" in html
    assert "最高稳定并发" in html


@pytest.mark.asyncio
async def test_run_stage_honors_concurrency_and_request_cap():
    image = png_stub(512, 512)
    encoded = base64.b64encode(image).decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"b64_json": encoded}]})

    benchmark_config = config(
        concurrency_levels=(2,),
        stage_seconds=10.0,
        max_requests_per_stage=4,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        stage = await run_stage(
            client=client,
            benchmark_config=benchmark_config,
            concurrency=2,
            benchmark_started_monotonic=time.monotonic(),
        )

    assert stage.attempts == 4
    assert stage.successful_requests == 4
    assert stage.image_count == 4
    assert stage.stable is True


@pytest.mark.asyncio
async def test_run_stage_stops_after_consecutive_failures():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "upstream unavailable"}})

    benchmark_config = config(
        concurrency_levels=(1,),
        stage_seconds=10.0,
        max_requests_per_stage=20,
        max_consecutive_failures=2,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        stage = await run_stage(
            client=client,
            benchmark_config=benchmark_config,
            concurrency=1,
            benchmark_started_monotonic=time.monotonic(),
        )

    assert stage.attempts == 2
    assert stage.failed_requests == 2
    assert stage.error_counts == {"upstream unavailable": 2}


def test_write_report_files_creates_html_and_optional_json(tmp_path: Path):
    benchmark_config = config()
    stage = summarize_stage(
        concurrency=1,
        configured_seconds=30.0,
        elapsed_seconds=30.0,
        results=[result(1, duration=2.0)],
        minimum_success_rate=0.95,
    )
    document = build_report_document(
        benchmark_config=benchmark_config,
        stages=[stage],
        started_at=datetime(2026, 7, 29, 8, 0, tzinfo=UTC),
        finished_at=datetime(2026, 7, 29, 8, 1, tzinfo=UTC),
    )
    html_path = tmp_path / "nested" / "report.html"
    json_path = tmp_path / "nested" / "report.json"

    write_report_files(document, html_path=html_path, json_path=json_path)

    assert html_path.read_text(encoding="utf-8").startswith("<!doctype html>")
    saved_json = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved_json["summary"]["total_images"] == 1
    assert "stress-secret-value" not in json_path.read_text(encoding="utf-8")


def test_cli_config_reads_key_from_environment_without_api_key_argument():
    parser = build_argument_parser({})
    args = parser.parse_args(
        [
            "--execute",
            "--api-base",
            "https://images.example.test/antigravity/v1",
            "--concurrency-levels",
            "1,3,6",
            "--stage-seconds",
            "45",
        ]
    )

    benchmark_config = config_from_args(
        args,
        environ={"BANANA_API_KEY": "environment-secret"},
    )

    assert benchmark_config.api_key == "environment-secret"
    assert benchmark_config.concurrency_levels == (1, 3, 6)
    assert benchmark_config.stage_seconds == 45.0
    assert "--api-key" not in parser.format_help().lower()


@pytest.mark.asyncio
async def test_run_benchmark_stops_ramp_after_first_unstable_stage():
    image = png_stub(512, 512)
    encoded = base64.b64encode(image).decode("ascii")
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, json={"data": [{"b64_json": encoded}]})
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    benchmark_config = config(
        concurrency_levels=(1, 2, 4),
        stage_seconds=10.0,
        max_requests_per_stage=1,
        cooldown_seconds=0.0,
    )
    messages: list[str] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        stages, _, _ = await run_benchmark(
            benchmark_config,
            client=client,
            progress=messages.append,
        )

    assert [stage.concurrency for stage in stages] == [1, 2]
    assert stages[0].stable is True
    assert stages[1].stable is False
    assert any("停止继续加压" in message for message in messages)
