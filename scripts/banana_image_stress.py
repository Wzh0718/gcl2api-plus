#!/usr/bin/env python3
"""Benchmark Banana/Gemini image generation and write a self-contained HTML report."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import html
import json
import math
import os
import struct
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import httpx
from dotenv import load_dotenv

Protocol = Literal["openai", "google"]
SUPPORTED_ASPECT_RATIOS = {
    "1:1",
    "1:4",
    "1:8",
    "2:3",
    "3:2",
    "3:4",
    "4:1",
    "4:3",
    "4:5",
    "5:4",
    "8:1",
    "9:16",
    "16:9",
    "21:9",
}
SUPPORTED_IMAGE_SIZES = {"512", "1K", "2K", "4K"}
MAX_CONCURRENCY = 256


@dataclass(frozen=True)
class BenchmarkConfig:
    api_key: str
    api_base: str
    protocol: Protocol
    model: str
    prompt: str
    concurrency_levels: tuple[int, ...]
    stage_seconds: float
    request_timeout_seconds: float
    size: str
    aspect_ratio: str
    image_size: str
    images_per_request: int
    target_rpm: float
    max_requests_per_stage: int
    minimum_success_rate: float
    max_consecutive_failures: int
    cooldown_seconds: float

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ValueError("请通过 BANANA_API_KEY 设置 API Key")
        parsed_url = urlsplit(self.api_base)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("api_base 必须是完整的 http:// 或 https:// 地址")
        if (
            parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError("api_base 不能包含用户名、密码、查询串或 fragment 等敏感信息")
        if self.protocol not in {"openai", "google"}:
            raise ValueError("protocol 只能是 openai 或 google")
        if not self.model.strip():
            raise ValueError("model 不能为空")
        if not self.prompt.strip():
            raise ValueError("prompt 不能为空")
        _validate_concurrency_levels(self.concurrency_levels)
        if not 0 < self.stage_seconds <= 86400:
            raise ValueError("stage_seconds 必须在 0 到 86400 秒之间")
        if not 0 < self.request_timeout_seconds <= 3600:
            raise ValueError("request_timeout_seconds 必须在 0 到 3600 秒之间")
        if not 1 <= self.images_per_request <= 4:
            raise ValueError("images_per_request 必须在 1 到 4 之间")
        if self.protocol == "google" and self.images_per_request != 1:
            raise ValueError("Google 协议当前只允许 images_per_request=1")
        if self.aspect_ratio not in SUPPORTED_ASPECT_RATIOS:
            raise ValueError("aspect_ratio 不是受支持的 Google 图片比例")
        normalized_image_size = self.image_size.upper()
        if normalized_image_size not in SUPPORTED_IMAGE_SIZES:
            raise ValueError("image_size 只能是 512、1K、2K 或 4K")
        object.__setattr__(self, "image_size", normalized_image_size)
        if self.target_rpm < 0:
            raise ValueError("target_rpm 不能小于 0")
        if self.max_requests_per_stage < 0:
            raise ValueError("max_requests_per_stage 不能小于 0")
        if not 0 < self.minimum_success_rate <= 1:
            raise ValueError("minimum_success_rate 必须大于 0 且不超过 1")
        if self.max_consecutive_failures < 0:
            raise ValueError("max_consecutive_failures 不能小于 0")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds 不能小于 0")


@dataclass(frozen=True)
class RequestResult:
    sequence: int
    started_offset_seconds: float
    duration_seconds: float
    status_code: int
    success: bool
    image_count: int
    image_bytes: int
    dimensions: tuple[tuple[int, int], ...]
    mime_types: tuple[str, ...]
    error: str | None = None


@dataclass(frozen=True)
class StageSummary:
    concurrency: int
    configured_seconds: float
    elapsed_seconds: float
    attempts: int
    successful_requests: int
    failed_requests: int
    image_count: int
    image_bytes: int
    request_rpm: float
    image_rpm: float
    success_rate: float
    average_seconds: float
    p50_seconds: float
    p95_seconds: float
    p99_seconds: float
    status_counts: dict[str, int]
    error_counts: dict[str, int]
    dimensions: dict[str, int]
    stable: bool


def _validate_concurrency_levels(levels: Sequence[int]) -> None:
    if not levels:
        raise ValueError("至少需要一个并发档位")
    if any(level <= 0 for level in levels):
        raise ValueError("并发档位必须是正整数")
    if any(level > MAX_CONCURRENCY for level in levels):
        raise ValueError(f"单个并发档位不能超过 {MAX_CONCURRENCY}")
    if len(set(levels)) != len(levels):
        raise ValueError("并发档位不能重复")


def parse_concurrency_levels(raw_value: str) -> tuple[int, ...]:
    try:
        levels = tuple(int(item.strip()) for item in raw_value.split(",") if item.strip())
    except ValueError as exc:
        raise ValueError("并发档位必须是逗号分隔的正整数") from exc
    _validate_concurrency_levels(levels)
    return levels


def build_http_request(
    benchmark_config: BenchmarkConfig,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    api_base = benchmark_config.api_base.rstrip("/")
    if benchmark_config.protocol == "openai":
        return (
            f"{api_base}/images/generations",
            {
                "Authorization": f"Bearer {benchmark_config.api_key}",
                "Content-Type": "application/json",
            },
            {
                "model": benchmark_config.model,
                "prompt": benchmark_config.prompt,
                "n": benchmark_config.images_per_request,
                "size": benchmark_config.size,
                "response_format": "b64_json",
                "output_format": "jpeg",
                "quality": "auto",
                "background": "auto",
                "moderation": "auto",
            },
        )

    encoded_model = quote(benchmark_config.model, safe="-._~")
    return (
        f"{api_base}/models/{encoded_model}:generateContent",
        {
            "x-goog-api-key": benchmark_config.api_key,
            "Content-Type": "application/json",
        },
        {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": benchmark_config.prompt}],
                }
            ],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "responseFormat": {
                    "image": {
                        "aspectRatio": benchmark_config.aspect_ratio,
                        "imageSize": benchmark_config.image_size,
                    }
                },
            },
        },
    )


def _decode_image(encoded: str) -> bytes:
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("响应包含无效的 Base64 图片") from exc


def _detect_mime_type(image: bytes) -> str:
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if image.startswith(b"RIFF") and image[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def extract_response_images(
    payload: dict[str, Any], *, protocol: Protocol
) -> list[tuple[str, bytes]]:
    images: list[tuple[str, bytes]] = []
    if protocol == "openai":
        for item in payload.get("data", []):
            encoded = item.get("b64_json") if isinstance(item, dict) else None
            if not encoded:
                continue
            image = _decode_image(str(encoded))
            images.append((_detect_mime_type(image), image))
        return images

    response = payload.get("response", payload)
    if not isinstance(response, dict):
        return images
    for candidate in response.get("candidates", []):
        content = candidate.get("content", {}) if isinstance(candidate, dict) else {}
        for part in content.get("parts", []):
            if not isinstance(part, dict):
                continue
            inline_data = part.get("inlineData") or part.get("inline_data")
            if not isinstance(inline_data, dict) or not inline_data.get("data"):
                continue
            image = _decode_image(str(inline_data["data"]))
            mime_type = (
                inline_data.get("mimeType")
                or inline_data.get("mime_type")
                or _detect_mime_type(image)
            )
            images.append((str(mime_type), image))
    return images


def image_dimensions(image: bytes, mime_type: str) -> tuple[int, int] | None:
    normalized_mime = mime_type.lower()
    if normalized_mime == "image/png" or image.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(image) >= 24 and image[12:16] == b"IHDR":
            return struct.unpack(">II", image[16:24])
        return None
    if normalized_mime in {"image/jpeg", "image/jpg"} or image.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 <= len(image):
            if image[index] != 0xFF:
                index += 1
                continue
            marker = image[index + 1]
            index += 2
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(image):
                break
            segment_length = int.from_bytes(image[index : index + 2], "big")
            if segment_length < 2 or index + segment_length > len(image):
                break
            if (
                marker
                in {
                    0xC0,
                    0xC1,
                    0xC2,
                    0xC3,
                    0xC5,
                    0xC6,
                    0xC7,
                    0xC9,
                    0xCA,
                    0xCB,
                    0xCD,
                    0xCE,
                    0xCF,
                }
                and segment_length >= 7
            ):
                height = int.from_bytes(image[index + 3 : index + 5], "big")
                width = int.from_bytes(image[index + 5 : index + 7], "big")
                return width, height
            index += segment_length
    return None


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def summarize_stage(
    *,
    concurrency: int,
    configured_seconds: float,
    elapsed_seconds: float,
    results: Sequence[RequestResult],
    minimum_success_rate: float,
) -> StageSummary:
    attempts = len(results)
    successful_requests = sum(1 for item in results if item.success)
    failed_requests = attempts - successful_requests
    image_count = sum(item.image_count for item in results)
    image_bytes = sum(item.image_bytes for item in results)
    safe_elapsed = max(elapsed_seconds, 0.000001)
    durations = [item.duration_seconds for item in results]
    status_counts = Counter(str(item.status_code) for item in results)
    error_counts = Counter(item.error for item in results if item.error)
    dimension_counts = Counter(
        f"{width}x{height}" for item in results for width, height in item.dimensions
    )
    success_rate = successful_requests / attempts if attempts else 0.0
    stable = (
        attempts > 0 and success_rate >= minimum_success_rate and status_counts.get("429", 0) == 0
    )
    return StageSummary(
        concurrency=concurrency,
        configured_seconds=configured_seconds,
        elapsed_seconds=elapsed_seconds,
        attempts=attempts,
        successful_requests=successful_requests,
        failed_requests=failed_requests,
        image_count=image_count,
        image_bytes=image_bytes,
        request_rpm=attempts / safe_elapsed * 60,
        image_rpm=image_count / safe_elapsed * 60,
        success_rate=success_rate,
        average_seconds=sum(durations) / attempts if attempts else 0.0,
        p50_seconds=_percentile(durations, 0.50),
        p95_seconds=_percentile(durations, 0.95),
        p99_seconds=_percentile(durations, 0.99),
        status_counts=dict(sorted(status_counts.items())),
        error_counts=dict(error_counts.most_common(20)),
        dimensions=dict(dimension_counts.most_common()),
        stable=stable,
    )


def _redact_error(message: str, api_key: str) -> str:
    redacted = message.replace(api_key, "[REDACTED]") if api_key else message
    return " ".join(redacted.split())[:500]


def _response_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500] or f"HTTP {response.status_code}"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("detail") or error)
        if error:
            return str(error)
        if payload.get("detail"):
            return str(payload["detail"])
    return str(payload)[:500]


async def execute_image_request(
    *,
    client: httpx.AsyncClient,
    benchmark_config: BenchmarkConfig,
    sequence: int,
    benchmark_started_monotonic: float,
    monotonic: Callable[[], float] = time.monotonic,
) -> RequestResult:
    endpoint, headers, payload = build_http_request(benchmark_config)
    started = monotonic()
    started_offset = max(0.0, started - benchmark_started_monotonic)
    try:
        response = await client.post(endpoint, headers=headers, json=payload)
        duration = max(0.0, monotonic() - started)
        if response.status_code < 200 or response.status_code >= 300:
            return RequestResult(
                sequence=sequence,
                started_offset_seconds=started_offset,
                duration_seconds=duration,
                status_code=response.status_code,
                success=False,
                image_count=0,
                image_bytes=0,
                dimensions=(),
                mime_types=(),
                error=_redact_error(_response_error(response), benchmark_config.api_key),
            )
        try:
            response_payload = response.json()
            if not isinstance(response_payload, dict):
                raise TypeError("响应 JSON 根节点不是对象")
            images = extract_response_images(response_payload, protocol=benchmark_config.protocol)
        except (ValueError, TypeError) as exc:
            return RequestResult(
                sequence=sequence,
                started_offset_seconds=started_offset,
                duration_seconds=duration,
                status_code=response.status_code,
                success=False,
                image_count=0,
                image_bytes=0,
                dimensions=(),
                mime_types=(),
                error=_redact_error(str(exc), benchmark_config.api_key),
            )
        if not images:
            return RequestResult(
                sequence=sequence,
                started_offset_seconds=started_offset,
                duration_seconds=duration,
                status_code=response.status_code,
                success=False,
                image_count=0,
                image_bytes=0,
                dimensions=(),
                mime_types=(),
                error="HTTP 成功，但响应中没有可解码图片",
            )
        dimensions = tuple(
            dimension
            for mime_type, image in images
            if (dimension := image_dimensions(image, mime_type)) is not None
        )
        return RequestResult(
            sequence=sequence,
            started_offset_seconds=started_offset,
            duration_seconds=duration,
            status_code=response.status_code,
            success=True,
            image_count=len(images),
            image_bytes=sum(len(image) for _, image in images),
            dimensions=dimensions,
            mime_types=tuple(mime_type for mime_type, _ in images),
            error=None,
        )
    except httpx.TimeoutException:
        duration = max(0.0, monotonic() - started)
        return RequestResult(
            sequence=sequence,
            started_offset_seconds=started_offset,
            duration_seconds=duration,
            status_code=0,
            success=False,
            image_count=0,
            image_bytes=0,
            dimensions=(),
            mime_types=(),
            error=f"请求超时（{benchmark_config.request_timeout_seconds:g} 秒）",
        )
    except httpx.HTTPError as exc:
        duration = max(0.0, monotonic() - started)
        return RequestResult(
            sequence=sequence,
            started_offset_seconds=started_offset,
            duration_seconds=duration,
            status_code=0,
            success=False,
            image_count=0,
            image_bytes=0,
            dimensions=(),
            mime_types=(),
            error=_redact_error(f"{type(exc).__name__}: {exc}", benchmark_config.api_key),
        )


class _StartRateLimiter:
    def __init__(self, target_rpm: float) -> None:
        self._interval_seconds = 60.0 / target_rpm if target_rpm > 0 else 0.0
        self._next_start = 0.0
        self._lock = asyncio.Lock()

    async def wait_for_slot(self, *, deadline: float) -> bool:
        if self._interval_seconds <= 0:
            return time.monotonic() < deadline
        async with self._lock:
            now = time.monotonic()
            scheduled = max(now, self._next_start)
            if scheduled >= deadline:
                return False
            self._next_start = scheduled + self._interval_seconds
        delay = scheduled - now
        if delay > 0:
            await asyncio.sleep(delay)
        return time.monotonic() < deadline


async def run_stage(
    *,
    client: httpx.AsyncClient,
    benchmark_config: BenchmarkConfig,
    concurrency: int,
    benchmark_started_monotonic: float,
) -> StageSummary:
    stage_started = time.monotonic()
    deadline = stage_started + benchmark_config.stage_seconds
    limiter = _StartRateLimiter(benchmark_config.target_rpm)
    results: list[RequestResult] = []
    state_lock = asyncio.Lock()
    stop_event = asyncio.Event()
    next_sequence = 1
    consecutive_failures = 0

    async def reserve_sequence() -> int | None:
        nonlocal next_sequence
        async with state_lock:
            if stop_event.is_set():
                return None
            if (
                benchmark_config.max_requests_per_stage
                and next_sequence > benchmark_config.max_requests_per_stage
            ):
                return None
            sequence = next_sequence
            next_sequence += 1
            return sequence

    async def record_result(request_result: RequestResult) -> None:
        nonlocal consecutive_failures
        async with state_lock:
            results.append(request_result)
            if request_result.success:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if (
                    benchmark_config.max_consecutive_failures
                    and consecutive_failures >= benchmark_config.max_consecutive_failures
                ):
                    stop_event.set()

    async def worker() -> None:
        while not stop_event.is_set() and time.monotonic() < deadline:
            if not await limiter.wait_for_slot(deadline=deadline):
                return
            sequence = await reserve_sequence()
            if sequence is None:
                return
            request_result = await execute_image_request(
                client=client,
                benchmark_config=benchmark_config,
                sequence=sequence,
                benchmark_started_monotonic=benchmark_started_monotonic,
            )
            await record_result(request_result)

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    elapsed_seconds = max(0.0, time.monotonic() - stage_started)
    ordered_results = sorted(results, key=lambda item: item.sequence)
    return summarize_stage(
        concurrency=concurrency,
        configured_seconds=benchmark_config.stage_seconds,
        elapsed_seconds=elapsed_seconds,
        results=ordered_results,
        minimum_success_rate=benchmark_config.minimum_success_rate,
    )


def build_report_document(
    *,
    benchmark_config: BenchmarkConfig,
    stages: Sequence[StageSummary],
    started_at: datetime,
    finished_at: datetime,
) -> dict[str, Any]:
    endpoint, _, _ = build_http_request(benchmark_config)
    total_attempts = sum(stage.attempts for stage in stages)
    total_successful_requests = sum(stage.successful_requests for stage in stages)
    total_images = sum(stage.image_count for stage in stages)
    total_image_bytes = sum(stage.image_bytes for stage in stages)
    stable_stages = [stage for stage in stages if stage.stable]
    best_stage = max(stages, key=lambda stage: stage.image_rpm, default=None)
    recommended_stage = max(
        stable_stages,
        key=lambda stage: stage.image_rpm,
        default=None,
    )
    return {
        "schema_version": 1,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": max(0.0, (finished_at - started_at).total_seconds()),
        "config": {
            "api_base": benchmark_config.api_base,
            "endpoint": endpoint,
            "transport_security": urlsplit(benchmark_config.api_base).scheme,
            "protocol": benchmark_config.protocol,
            "model": benchmark_config.model,
            "prompt": benchmark_config.prompt,
            "concurrency_levels": list(benchmark_config.concurrency_levels),
            "stage_seconds": benchmark_config.stage_seconds,
            "request_timeout_seconds": benchmark_config.request_timeout_seconds,
            "size": benchmark_config.size,
            "aspect_ratio": benchmark_config.aspect_ratio,
            "image_size": benchmark_config.image_size,
            "images_per_request": benchmark_config.images_per_request,
            "target_rpm": benchmark_config.target_rpm,
            "max_requests_per_stage": benchmark_config.max_requests_per_stage,
            "minimum_success_rate": benchmark_config.minimum_success_rate,
            "max_consecutive_failures": benchmark_config.max_consecutive_failures,
            "cooldown_seconds": benchmark_config.cooldown_seconds,
        },
        "summary": {
            "total_attempts": total_attempts,
            "total_successful_requests": total_successful_requests,
            "total_failed_requests": total_attempts - total_successful_requests,
            "total_images": total_images,
            "total_image_bytes": total_image_bytes,
            "overall_success_rate": (
                total_successful_requests / total_attempts if total_attempts else 0.0
            ),
            "best_image_rpm": best_stage.image_rpm if best_stage else 0.0,
            "best_request_rpm": best_stage.request_rpm if best_stage else 0.0,
            "best_stage_concurrency": best_stage.concurrency if best_stage else 0,
            "highest_stable_concurrency": max(
                (stage.concurrency for stage in stable_stages), default=0
            ),
            "recommended_concurrency": (recommended_stage.concurrency if recommended_stage else 0),
            "recommended_image_rpm": (recommended_stage.image_rpm if recommended_stage else 0.0),
        },
        "stages": [asdict(stage) for stage in stages],
    }


def _format_bytes(value: float) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if amount < 1024 or unit == "GiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} GiB"


def _throughput_svg(stages: Sequence[dict[str, Any]]) -> str:
    width = 900
    height = 300
    margin_left = 56
    margin_bottom = 42
    plot_width = width - margin_left - 24
    plot_height = height - 24 - margin_bottom
    if not stages:
        return (
            f'<svg viewBox="0 0 {width} {height}" role="img" '
            'aria-label="没有压测阶段数据"></svg>'
        )
    maximum = max(float(stage.get("image_rpm", 0)) for stage in stages) or 1.0
    slot = plot_width / len(stages)
    bar_width = min(72.0, slot * 0.58)
    parts = [
        (
            f'<svg viewBox="0 0 {width} {height}" role="img" '
            'aria-label="各并发档位图片 RPM 柱状图">'
        ),
        (
            f'<line x1="{margin_left}" y1="24" x2="{margin_left}" '
            f'y2="{24 + plot_height}" class="axis"/>'
        ),
        (
            f'<line x1="{margin_left}" y1="{24 + plot_height}" '
            f'x2="{margin_left + plot_width}" y2="{24 + plot_height}" class="axis"/>'
        ),
    ]
    for index, stage in enumerate(stages):
        rpm = float(stage.get("image_rpm", 0))
        bar_height = rpm / maximum * (plot_height - 28)
        x = margin_left + index * slot + (slot - bar_width) / 2
        y = 24 + plot_height - bar_height
        concurrency = int(stage.get("concurrency", 0))
        stable_class = "bar stable" if stage.get("stable") else "bar"
        parts.extend(
            [
                (
                    f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_width:.2f}" '
                    f'height="{bar_height:.2f}" rx="8" class="{stable_class}"/>'
                ),
                (
                    f'<text x="{x + bar_width / 2:.2f}" '
                    f'y="{max(18, y - 7):.2f}" class="value" '
                    f'text-anchor="middle">{rpm:.2f}</text>'
                ),
                (
                    f'<text x="{x + bar_width / 2:.2f}" y="{height - 13}" '
                    f'class="label" text-anchor="middle">C={concurrency}</text>'
                ),
            ]
        )
    parts.append("</svg>")
    return "".join(parts)


def _table_rows(stages: Sequence[dict[str, Any]]) -> str:
    rows = []
    for stage in stages:
        rows.append(
            "<tr>"
            f'<td>{int(stage.get("concurrency", 0))}</td>'
            f'<td>{int(stage.get("attempts", 0))}</td>'
            f'<td>{int(stage.get("image_count", 0))}</td>'
            f'<td>{float(stage.get("success_rate", 0)) * 100:.2f}%</td>'
            f'<td>{float(stage.get("request_rpm", 0)):.2f}</td>'
            f'<td>{float(stage.get("image_rpm", 0)):.2f}</td>'
            f'<td>{float(stage.get("average_seconds", 0)):.2f}s</td>'
            f'<td>{float(stage.get("p50_seconds", 0)):.2f}s</td>'
            f'<td>{float(stage.get("p95_seconds", 0)):.2f}s</td>'
            f'<td>{float(stage.get("p99_seconds", 0)):.2f}s</td>'
            f'<td>{"是" if stage.get("stable") else "否"}</td>'
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="11">没有阶段数据</td></tr>'


def _error_rows(stages: Sequence[dict[str, Any]]) -> str:
    errors: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    for stage in stages:
        errors.update(stage.get("error_counts", {}))
        statuses.update(stage.get("status_counts", {}))
    rows = [
        "<tr>" f"<td>HTTP {html.escape(status)}</td>" f"<td>{count}</td>" "</tr>"
        for status, count in statuses.most_common()
        if status != "200"
    ]
    rows.extend(
        "<tr>" f"<td>{html.escape(message)}</td>" f"<td>{count}</td>" "</tr>"
        for message, count in errors.most_common(20)
    )
    return "".join(rows) or '<tr><td colspan="2">没有错误</td></tr>'


def render_html_report(document: dict[str, Any]) -> str:
    config = document.get("config", {})
    summary = document.get("summary", {})
    stages = document.get("stages", [])
    prompt = html.escape(str(config.get("prompt", "")))
    endpoint = html.escape(str(config.get("endpoint", "")))
    protocol = html.escape(str(config.get("protocol", "")))
    model = html.escape(str(config.get("model", "")))
    started_at = html.escape(str(document.get("started_at", "")))
    finished_at = html.escape(str(document.get("finished_at", "")))
    security_warning = ""
    if config.get("transport_security") == "http":
        security_warning = (
            '<div class="warning">当前测试使用 HTTP 明文传输。API Key 和提示词在网络中没有 TLS 保护，'
            "生产接入建议使用 HTTPS。</div>"
        )
    cards = [
        ("成功图片", f'{int(summary.get("total_images", 0)):,}'),
        ("最佳图片 RPM", f'{float(summary.get("best_image_rpm", 0)):.2f}'),
        ("最高稳定并发", str(int(summary.get("highest_stable_concurrency", 0)))),
        ("建议并发", str(int(summary.get("recommended_concurrency", 0)))),
        ("总请求", f'{int(summary.get("total_attempts", 0)):,}'),
        ("成功率", f'{float(summary.get("overall_success_rate", 0)) * 100:.2f}%'),
    ]
    card_html = "".join(
        '<div class="card"><span>'
        + html.escape(label)
        + "</span><strong>"
        + html.escape(value)
        + "</strong></div>"
        for label, value in cards
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <link rel="icon" href="data:,">
  <title>Banana 图片生成压力测试报告</title>
  <style>
    :root {{ color-scheme: dark; --bg:#09111f; --panel:#111c2f; --line:#29364c; --text:#e8eef8; --muted:#94a3b8; --cyan:#22d3ee; --green:#34d399; --red:#fb7185; }}
    * {{ box-sizing:border-box; }} body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }}
    main {{ width:min(1180px,calc(100% - 32px)); margin:32px auto 72px; }} h1 {{ margin:0 0 6px; font-size:30px; }} h2 {{ margin:0 0 16px; font-size:19px; }}
    .muted {{ color:var(--muted); }} .panel {{ margin-top:20px; padding:22px; border:1px solid var(--line); border-radius:16px; background:var(--panel); overflow:auto; }}
    .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-top:20px; }} .card {{ padding:18px; border:1px solid var(--line); border-radius:14px; background:var(--panel); }}
    .card span {{ display:block; color:var(--muted); font-size:12px; }} .card strong {{ display:block; margin-top:7px; color:var(--cyan); font-size:25px; }}
    .meta {{ display:grid; grid-template-columns:160px 1fr; gap:7px 16px; }} .meta dt {{ color:var(--muted); }} .meta dd {{ margin:0; overflow-wrap:anywhere; }}
    .warning {{ margin-top:18px; padding:12px 14px; border:1px solid #7f1d1d; border-radius:10px; background:#32151b; color:#fecdd3; }}
    table {{ width:100%; border-collapse:collapse; min-width:850px; }} th,td {{ padding:10px 12px; border-bottom:1px solid var(--line); text-align:right; white-space:nowrap; }} th:first-child,td:first-child {{ text-align:left; }} th {{ color:var(--muted); font-size:12px; }}
    svg {{ width:100%; min-width:680px; height:auto; }} .axis {{ stroke:#526076; stroke-width:1; }} .bar {{ fill:var(--cyan); opacity:.72; }} .bar.stable {{ fill:var(--green); opacity:.9; }} .label,.value {{ fill:var(--text); font-size:12px; }}
    code {{ color:#bae6fd; }} @media (max-width:640px) {{ main {{ width:min(100% - 20px,1180px); margin-top:18px; }} .panel {{ padding:16px; }} .meta {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
<main>
  <header><h1>Banana 图片生成压力测试</h1><div class="muted">{started_at} — {finished_at}</div></header>
  {security_warning}
  <section class="cards">{card_html}</section>
  <section class="panel"><h2>测试配置</h2><dl class="meta">
    <dt>协议</dt><dd>{protocol}</dd><dt>模型</dt><dd>{model}</dd><dt>接口</dt><dd><code>{endpoint}</code></dd>
    <dt>提示词</dt><dd>{prompt}</dd><dt>并发档位</dt><dd>{html.escape(str(config.get("concurrency_levels", [])))}</dd>
    <dt>每档时长</dt><dd>{float(config.get("stage_seconds", 0)):g} 秒</dd><dt>客户端 RPM 上限</dt><dd>{float(config.get("target_rpm", 0)):g}（0 表示不限速）</dd>
    <dt>图片参数</dt><dd>size={html.escape(str(config.get("size", "")))}；aspectRatio={html.escape(str(config.get("aspect_ratio", "")))}；imageSize={html.escape(str(config.get("image_size", "")))}</dd>
    <dt>返回图片数据量</dt><dd>{_format_bytes(summary.get("total_image_bytes", 0))}</dd>
  </dl></section>
  <section class="panel"><h2>各并发档位图片 RPM</h2>{_throughput_svg(stages)}<div class="muted">绿色表示成功率达到阈值且没有 HTTP 429；最高柱为本次实测吞吐，不代表服务端承诺配额。</div></section>
  <section class="panel"><h2>阶段结果</h2><table><thead><tr><th>并发</th><th>请求</th><th>图片</th><th>成功率</th><th>请求 RPM</th><th>图片 RPM</th><th>平均耗时</th><th>P50</th><th>P95</th><th>P99</th><th>稳定</th></tr></thead><tbody>{_table_rows(stages)}</tbody></table></section>
  <section class="panel"><h2>错误与限流</h2><table><thead><tr><th>状态或错误</th><th>次数</th></tr></thead><tbody>{_error_rows(stages)}</tbody></table></section>
  <section class="panel"><h2>指标解释</h2><p>图片 RPM = 本阶段成功解码的图片总数 ÷ 阶段实际耗时 × 60。请求 RPM 使用请求数计算。最高稳定并发要求成功率达到配置阈值，并且该阶段没有 HTTP 429。建议并发取所有稳定阶段中图片 RPM 最高的档位。</p><p class="muted">报告不保存 API Key，也不保存生成图片或 Base64，只保留数量、字节数、尺寸、延迟和错误摘要。</p></section>
</main>
</body>
</html>
"""


def write_report_files(
    document: dict[str, Any],
    *,
    html_path: Path,
    json_path: Path | None = None,
) -> None:
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_html_report(document), encoding="utf-8")
    if json_path is not None:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


async def run_benchmark(
    benchmark_config: BenchmarkConfig,
    *,
    client: httpx.AsyncClient | None = None,
    progress: Callable[[str], None] = print,
) -> tuple[list[StageSummary], datetime, datetime]:
    started_at = datetime.now().astimezone()
    benchmark_started_monotonic = time.monotonic()
    owns_client = client is None
    active_client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(benchmark_config.request_timeout_seconds),
        limits=httpx.Limits(
            max_connections=max(benchmark_config.concurrency_levels),
            max_keepalive_connections=max(benchmark_config.concurrency_levels),
        ),
        follow_redirects=False,
    )
    stages: list[StageSummary] = []
    try:
        for index, concurrency in enumerate(benchmark_config.concurrency_levels, start=1):
            progress(
                f"[{index}/{len(benchmark_config.concurrency_levels)}] "
                f"并发={concurrency}，计划 {benchmark_config.stage_seconds:g} 秒"
            )
            stage = await run_stage(
                client=active_client,
                benchmark_config=benchmark_config,
                concurrency=concurrency,
                benchmark_started_monotonic=benchmark_started_monotonic,
            )
            stages.append(stage)
            progress(
                f"  请求={stage.attempts}，图片={stage.image_count}，"
                f"成功率={stage.success_rate * 100:.2f}%，"
                f"请求 RPM={stage.request_rpm:.2f}，图片 RPM={stage.image_rpm:.2f}，"
                f"平均={stage.average_seconds:.2f}s，"
                f"P95={stage.p95_seconds:.2f}s"
            )
            if not stage.stable:
                progress(
                    "  当前档位未达到稳定标准或出现 HTTP 429，停止继续加压，"
                    "避免扩大失败与额度消耗。"
                )
                break
            if (
                index < len(benchmark_config.concurrency_levels)
                and benchmark_config.cooldown_seconds > 0
            ):
                progress(f"  冷却 {benchmark_config.cooldown_seconds:g} 秒后进入下一档。")
                await asyncio.sleep(benchmark_config.cooldown_seconds)
    finally:
        if owns_client:
            await active_client.aclose()
    return stages, started_at, datetime.now().astimezone()


def build_argument_parser(
    environ: dict[str, str] | None = None,
) -> argparse.ArgumentParser:
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(
        description=(
            "对 Banana/Gemini 图片接口逐级增加并发，统计请求 RPM、图片 RPM、"
            "成功率和延迟，并生成自包含 HTML 报告。"
        )
    )
    parser.add_argument(
        "--api-base",
        default=env.get(
            "BANANA_API_BASE",
            "http://127.0.0.1:7861/antigravity/v1",
        ),
        help="API 基础地址，默认读取 BANANA_API_BASE",
    )
    parser.add_argument(
        "--protocol",
        choices=("openai", "google"),
        default=env.get("BANANA_PROTOCOL", "openai"),
        help="请求协议，默认 openai",
    )
    parser.add_argument(
        "--model",
        default=env.get("BANANA_MODEL", "gemini-3.1-flash-image"),
        help="图片模型",
    )
    parser.add_argument(
        "--prompt",
        default=env.get(
            "BANANA_PROMPT",
            "Generate a clean benchmark image of a blue geometric cube on a white background",
        ),
        help="每次请求使用的图片提示词",
    )
    parser.add_argument(
        "--concurrency-levels",
        default=env.get("BANANA_CONCURRENCY_LEVELS", "1,2,4"),
        help="逗号分隔的并发档位，例如 1,2,4,8；最大 256",
    )
    parser.add_argument(
        "--stage-seconds",
        type=float,
        default=float(env.get("BANANA_STAGE_SECONDS", "60")),
        help="每个并发档位运行秒数",
    )
    parser.add_argument(
        "--request-timeout-seconds",
        type=float,
        default=float(env.get("BANANA_REQUEST_TIMEOUT_SECONDS", "300")),
        help="单个图片请求超时秒数",
    )
    parser.add_argument(
        "--size",
        default=env.get("BANANA_SIZE", "512x512"),
        help="OpenAI 协议 size，例如 512x512",
    )
    parser.add_argument(
        "--aspect-ratio",
        default=env.get("BANANA_ASPECT_RATIO", "1:1"),
        help="Google 协议 aspectRatio，例如 1:1、16:9",
    )
    parser.add_argument(
        "--image-size",
        default=env.get("BANANA_IMAGE_SIZE", "512"),
        help="Google 协议 imageSize：512、1K、2K、4K",
    )
    parser.add_argument(
        "--images-per-request",
        type=int,
        default=int(env.get("BANANA_IMAGES_PER_REQUEST", "1")),
        help="OpenAI 单请求图片数 1-4；Google 必须为 1",
    )
    parser.add_argument(
        "--target-rpm",
        type=float,
        default=float(env.get("BANANA_TARGET_RPM", "0")),
        help="客户端请求启动 RPM 上限；0 表示不主动限速",
    )
    parser.add_argument(
        "--max-requests-per-stage",
        type=int,
        default=int(env.get("BANANA_MAX_REQUESTS_PER_STAGE", "0")),
        help="每档最多请求数；0 表示只受时长限制",
    )
    parser.add_argument(
        "--minimum-success-rate",
        type=float,
        default=float(env.get("BANANA_MINIMUM_SUCCESS_RATE", "0.95")),
        help="稳定档位最低成功率，默认 0.95",
    )
    parser.add_argument(
        "--max-consecutive-failures",
        type=int,
        default=int(env.get("BANANA_MAX_CONSECUTIVE_FAILURES", "10")),
        help="连续失败达到该值时提前结束当前档；0 表示关闭",
    )
    parser.add_argument(
        "--cooldown-seconds",
        type=float,
        default=float(env.get("BANANA_COOLDOWN_SECONDS", "5")),
        help="稳定档位之间的冷却秒数",
    )
    parser.add_argument(
        "--output",
        default=env.get("BANANA_REPORT_PATH", ""),
        help="HTML 报告路径；省略时写入 reports/banana-stress-时间.html",
    )
    parser.add_argument(
        "--save-json",
        action="store_true",
        help="同时保存同名 JSON 原始指标文件",
    )
    parser.add_argument(
        "--open-report",
        action="store_true",
        help="完成后尝试使用默认浏览器打开 HTML",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="确认执行会真实生成图片并消耗额度的压力测试",
    )
    return parser


def _api_key_from_environment(protocol: Protocol, environ: dict[str, str]) -> str:
    if environ.get("BANANA_API_KEY"):
        return environ["BANANA_API_KEY"]
    if protocol == "google" and environ.get("GEMINI_IMAGE_API_KEY"):
        return environ["GEMINI_IMAGE_API_KEY"]
    if protocol == "openai" and environ.get("OPENAI_API_KEY"):
        return environ["OPENAI_API_KEY"]
    return ""


def config_from_args(
    args: argparse.Namespace,
    *,
    environ: dict[str, str] | None = None,
) -> BenchmarkConfig:
    env = dict(os.environ) if environ is None else environ
    protocol: Protocol = args.protocol
    return BenchmarkConfig(
        api_key=_api_key_from_environment(protocol, env),
        api_base=args.api_base,
        protocol=protocol,
        model=args.model,
        prompt=args.prompt,
        concurrency_levels=parse_concurrency_levels(args.concurrency_levels),
        stage_seconds=args.stage_seconds,
        request_timeout_seconds=args.request_timeout_seconds,
        size=args.size,
        aspect_ratio=args.aspect_ratio,
        image_size=args.image_size,
        images_per_request=args.images_per_request,
        target_rpm=args.target_rpm,
        max_requests_per_stage=args.max_requests_per_stage,
        minimum_success_rate=args.minimum_success_rate,
        max_consecutive_failures=args.max_consecutive_failures,
        cooldown_seconds=args.cooldown_seconds,
    )


def _default_report_path() -> Path:
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return Path("reports") / f"banana-stress-{timestamp}.html"


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    if not args.execute:
        parser.print_usage()
        print("未执行真实请求。确认目标和额度后，请追加 --execute。")
        return 2
    try:
        benchmark_config = config_from_args(args)
    except ValueError as exc:
        print(f"配置错误: {exc}")
        return 2

    output_path = Path(args.output) if args.output else _default_report_path()
    if output_path.suffix.lower() not in {".html", ".htm"}:
        print("配置错误: --output 必须使用 .html 或 .htm 扩展名")
        return 2
    estimated_seconds = (
        len(benchmark_config.concurrency_levels) * benchmark_config.stage_seconds
        + max(0, len(benchmark_config.concurrency_levels) - 1) * benchmark_config.cooldown_seconds
    )
    print("Banana 图片压力测试即将开始")
    print(f"接口: {build_http_request(benchmark_config)[0]}")
    print(f"协议/模型: {benchmark_config.protocol} / {benchmark_config.model}")
    print(f"并发档位: {benchmark_config.concurrency_levels}")
    print(f"预计最长运行: {estimated_seconds:.0f} 秒")
    if benchmark_config.api_base.lower().startswith("http://"):
        print("警告: 当前使用 HTTP 明文连接，正式环境建议改为 HTTPS。")
    try:
        stages, started_at, finished_at = asyncio.run(run_benchmark(benchmark_config))
    except KeyboardInterrupt:
        print("测试已由用户中断，未生成完整报告。")
        return 130

    document = build_report_document(
        benchmark_config=benchmark_config,
        stages=stages,
        started_at=started_at,
        finished_at=finished_at,
    )
    json_path = output_path.with_suffix(".json") if args.save_json else None
    write_report_files(document, html_path=output_path, json_path=json_path)
    resolved_output = output_path.resolve()
    print(f"HTML 报告: {resolved_output}")
    if json_path is not None:
        print(f"JSON 指标: {json_path.resolve()}")
    summary = document["summary"]
    print(
        f"完成: 图片={summary['total_images']}，"
        f"最佳图片 RPM={summary['best_image_rpm']:.2f}，"
        f"最高稳定并发={summary['highest_stable_concurrency']}，"
        f"建议并发={summary['recommended_concurrency']}"
    )
    if args.open_report:
        import webbrowser

        webbrowser.open(resolved_output.as_uri())
    return 0 if summary["total_images"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
