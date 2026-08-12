#!/usr/bin/env python3
"""Direct A/B smoke test for Antigravity image request fingerprints.

This script compares the legacy gcli2api wrapper fields with the current
Antigravity-Manager image fields while keeping the inner Gemini request equal.
It never writes credentials or returned image Base64 into its report.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
import math
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Sequence
from urllib.parse import urlsplit

import httpx


Variant = Literal["legacy", "manager"]
MODEL = "gemini-3.1-flash-image"
DEFAULT_API_BASE = "https://daily-cloudcode-pa.googleapis.com"
ALLOWED_API_HOSTS = {
    "daily-cloudcode-pa.sandbox.googleapis.com",
    "daily-cloudcode-pa.googleapis.com",
    "cloudcode-pa.googleapis.com",
}


@dataclass(frozen=True)
class RequestObservation:
    variant: Variant
    status_code: int
    success: bool
    duration_seconds: float
    image_count: int
    error: str | None


@dataclass
class AccountResult:
    label: str
    credential_file: str
    project_id: str
    quota_before: dict[str, Any] | None
    quota_after: dict[str, Any] | None
    observations: list[RequestObservation]
    setup_error: str | None = None


def validate_api_base(api_base: str) -> str:
    parsed = urlsplit(api_base.strip())
    if parsed.scheme != "https":
        raise ValueError("--api-base must use HTTPS")
    if parsed.hostname not in ALLOWED_API_HOSTS or parsed.port is not None:
        raise ValueError("--api-base must be one of the known Antigravity Google hosts")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("--api-base cannot contain credentials, query, or fragment")
    path = parsed.path.rstrip("/")
    if path:
        raise ValueError("--api-base cannot contain a path")
    return f"https://{parsed.hostname}"


def build_wrapped_image_request(
    *,
    prompt: str,
    project_id: str,
    variant: Variant,
    request_id: str,
    session_id: str,
    trajectory_id: str,
    aspect_ratio: str = "1:1",
    image_size: str = "512",
    model: str = MODEL,
) -> dict[str, Any]:
    """Build equal image requests whose only variant is the wrapper fingerprint."""
    if variant not in ("legacy", "manager"):
        raise ValueError("variant must be legacy or manager")
    if not prompt.strip():
        raise ValueError("prompt cannot be empty")
    if not project_id.strip():
        raise ValueError("project_id cannot be empty")

    inner_request = {
        "model": model,
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt.strip()}],
            }
        ],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "candidateCount": 1,
            "imageConfig": {
                "aspectRatio": aspect_ratio,
                "imageSize": image_size.upper(),
            },
        },
        "sessionId": session_id,
        "labels": {
            "last_step_index": "1",
            "model_enum": model,
            "trajectory_id": trajectory_id,
            "used_claude": "false",
            "used_claude_conservative": "false",
        },
        "toolConfig": {
            "functionCallingConfig": {"mode": "VALIDATED"},
        },
    }
    payload: dict[str, Any] = {
        "project": project_id,
        "requestId": request_id,
        "request": inner_request,
        "model": model,
        "userAgent": "antigravity",
        "requestType": "agent" if variant == "legacy" else "image_gen",
    }
    if variant == "legacy":
        payload["enabledCreditTypes"] = ["GOOGLE_ONE_AI"]
    return payload


def build_variant_schedule(rounds: int) -> list[Variant]:
    """Alternate which variant runs first to reduce time-window bias."""
    if rounds < 0:
        raise ValueError("rounds cannot be negative")
    schedule: list[Variant] = []
    for round_index in range(rounds):
        if round_index % 2 == 0:
            schedule.extend(("legacy", "manager"))
        else:
            schedule.extend(("manager", "legacy"))
    return schedule


def extract_image_quota(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), dict):
        return None
    models: dict[str, Any] = payload["models"]
    ordered_keys = sorted(
        models,
        key=lambda key: (
            key != MODEL,
            MODEL not in key.lower(),
            "image" not in key.lower() and "imagen" not in key.lower(),
            key,
        ),
    )
    for model_name in ordered_keys:
        if MODEL not in model_name.lower() and not any(
            marker in model_name.lower() for marker in ("image", "imagen")
        ):
            continue
        model_data = models.get(model_name)
        quota = model_data.get("quotaInfo") if isinstance(model_data, dict) else None
        if not isinstance(quota, dict):
            continue
        return {
            "model": model_name,
            "remaining": quota.get("remainingFraction"),
            "reset_time": quota.get("resetTime"),
        }
    return None


def _find_reason(value: Any) -> str | None:
    if isinstance(value, dict):
        reason = value.get("reason")
        if isinstance(reason, str) and reason.strip():
            return reason.strip()
        for nested in value.values():
            found = _find_reason(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_reason(nested)
            if found:
                return found
    return None


def extract_error_reason(payload: Any) -> str:
    if not isinstance(payload, dict):
        return str(payload).replace("\n", " ")[:300]
    error = payload.get("error", payload)
    reason = _find_reason(error)
    message = error.get("message") if isinstance(error, dict) else None
    parts = [part for part in (reason, message) if isinstance(part, str) and part.strip()]
    if parts:
        return ": ".join(parts).replace("\n", " ")[:300]
    return json.dumps(error, ensure_ascii=False, separators=(",", ":"))[:300]


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def summarize_observations(
    observations: Sequence[RequestObservation],
) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for variant in ("legacy", "manager"):
        items = [item for item in observations if item.variant == variant]
        durations = [item.duration_seconds for item in items]
        successes = sum(item.success for item in items)
        attempts = len(items)
        summary[variant] = {
            "attempts": attempts,
            "successful_requests": successes,
            "failed_requests": attempts - successes,
            "success_rate": successes / attempts if attempts else 0.0,
            "average_seconds": sum(durations) / attempts if attempts else 0.0,
            "p50_seconds": _percentile(durations, 0.50),
            "p95_seconds": _percentile(durations, 0.95),
            "status_counts": dict(
                sorted(Counter(str(item.status_code) for item in items).items())
            ),
            "error_counts": dict(
                Counter(item.error for item in items if item.error).most_common(20)
            ),
            "images": sum(item.image_count for item in items),
        }
    return summary


def build_report(
    *,
    accounts: Sequence[AccountResult],
    api_base: str,
    model: str,
    prompt: str,
    rounds: int,
    warmup_rounds: int,
    started_at: str,
    finished_at: str,
) -> dict[str, Any]:
    all_observations = [item for account in accounts for item in account.observations]
    return {
        "schema_version": 1,
        "started_at": started_at,
        "finished_at": finished_at,
        "config": {
            "api_base": api_base,
            "model": model,
            "prompt": prompt,
            "rounds_per_variant": rounds,
            "warmup_rounds_per_variant": warmup_rounds,
            "variants": {
                "legacy": {
                    "requestType": "agent",
                    "enabledCreditTypes": ["GOOGLE_ONE_AI"],
                },
                "manager": {
                    "requestType": "image_gen",
                    "enabledCreditTypes": None,
                },
            },
        },
        "aggregate_summary": summarize_observations(all_observations),
        "accounts": [
            {
                "label": account.label,
                "credential_file": Path(account.credential_file).name,
                "project_id": account.project_id,
                "quota_before": account.quota_before,
                "quota_after": account.quota_after,
                "setup_error": account.setup_error,
                "summary": summarize_observations(account.observations),
                "observations": [asdict(item) for item in account.observations],
            }
            for account in accounts
        ],
    }


def _redact_text(message: str, secrets: Sequence[str] = ()) -> str:
    redacted = message
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return " ".join(redacted.split())[:500]


def _extract_image_count(payload: Any) -> int:
    response = payload.get("response", payload) if isinstance(payload, dict) else {}
    if not isinstance(response, dict):
        return 0
    count = 0
    for candidate in response.get("candidates", []):
        content = candidate.get("content", {}) if isinstance(candidate, dict) else {}
        for part in content.get("parts", []):
            if not isinstance(part, dict):
                continue
            inline_data = part.get("inlineData") or part.get("inline_data")
            encoded = inline_data.get("data") if isinstance(inline_data, dict) else None
            if not encoded:
                continue
            try:
                base64.b64decode(str(encoded), validate=True)
            except (ValueError, binascii.Error):
                continue
            count += 1
    return count


async def fetch_quota_snapshot(
    *,
    client: httpx.AsyncClient,
    api_base: str,
    headers: dict[str, str],
    project_id: str,
    secrets: Sequence[str] = (),
) -> dict[str, Any]:
    endpoint = f"{api_base.rstrip('/')}/v1internal:fetchAvailableModels"
    try:
        response = await client.post(endpoint, headers=headers, json={"project": project_id})
        if response.status_code == 403:
            response = await client.post(endpoint, headers=headers, json={})
        if response.status_code < 200 or response.status_code >= 300:
            try:
                error = extract_error_reason(response.json())
            except ValueError:
                error = response.text or f"HTTP {response.status_code}"
            return {
                "success": False,
                "status_code": response.status_code,
                "error": _redact_text(error, secrets),
            }
        payload = response.json()
        quota = extract_image_quota(payload)
        return {
            "success": True,
            "status_code": response.status_code,
            **(quota or {"model": None, "remaining": None, "reset_time": None}),
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "success": False,
            "status_code": 0,
            "error": _redact_text(f"{type(exc).__name__}: {exc}", secrets),
        }


async def execute_image_request(
    *,
    client: httpx.AsyncClient,
    api_base: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    variant: Variant,
    secrets: Sequence[str] = (),
) -> RequestObservation:
    endpoint = f"{api_base.rstrip('/')}/v1internal:generateContent"
    started = time.monotonic()
    try:
        response = await client.post(endpoint, headers=headers, json=payload)
        duration = max(0.0, time.monotonic() - started)
        try:
            response_payload = response.json()
        except ValueError:
            response_payload = None
        if response.status_code < 200 or response.status_code >= 300:
            error = (
                extract_error_reason(response_payload)
                if response_payload is not None
                else response.text or f"HTTP {response.status_code}"
            )
            return RequestObservation(
                variant=variant,
                status_code=response.status_code,
                success=False,
                duration_seconds=duration,
                image_count=0,
                error=_redact_text(error, secrets),
            )
        image_count = _extract_image_count(response_payload)
        if image_count == 0:
            return RequestObservation(
                variant=variant,
                status_code=response.status_code,
                success=False,
                duration_seconds=duration,
                image_count=0,
                error="HTTP success but no decodable image was returned",
            )
        return RequestObservation(
            variant=variant,
            status_code=response.status_code,
            success=True,
            duration_seconds=duration,
            image_count=image_count,
            error=None,
        )
    except httpx.TimeoutException:
        return RequestObservation(
            variant=variant,
            status_code=0,
            success=False,
            duration_seconds=max(0.0, time.monotonic() - started),
            image_count=0,
            error="request timeout",
        )
    except httpx.HTTPError as exc:
        return RequestObservation(
            variant=variant,
            status_code=0,
            success=False,
            duration_seconds=max(0.0, time.monotonic() - started),
            image_count=0,
            error=_redact_text(f"{type(exc).__name__}: {exc}", secrets),
        )


@dataclass(frozen=True)
class AccountSpec:
    label: str
    credential_path: Path


@dataclass
class _PreparedAccount:
    spec: AccountSpec
    project_id: str
    headers: dict[str, str]
    secrets: tuple[str, ...]
    client: httpx.AsyncClient
    result: AccountResult


def _parse_account(raw: str) -> AccountSpec:
    label, separator, path_text = raw.partition("=")
    if not separator or not label.strip() or not path_text.strip():
        raise argparse.ArgumentTypeError("--account must use LABEL=/path/to/credential.json")
    return AccountSpec(label=label.strip(), credential_path=Path(path_text).expanduser())


def _new_wrapped_request(
    *,
    prompt: str,
    project_id: str,
    variant: Variant,
    aspect_ratio: str,
    image_size: str,
    model: str,
) -> dict[str, Any]:
    conversation_id = str(uuid.uuid4())
    trajectory_id = str(uuid.uuid4())
    unix_ms = int(time.time() * 1000)
    request_id = f"agent/{conversation_id}/{unix_ms}/{trajectory_id}/1"
    session_id = f"-{uuid.uuid4().int % 9_000_000_000_000_000_000}"
    return build_wrapped_image_request(
        prompt=prompt,
        project_id=project_id,
        variant=variant,
        request_id=request_id,
        session_id=session_id,
        trajectory_id=trajectory_id,
        aspect_ratio=aspect_ratio,
        image_size=image_size,
        model=model,
    )


async def _prepare_account(
    spec: AccountSpec,
    *,
    timeout_seconds: float,
    proxy: str | None,
) -> _PreparedAccount:
    from src.google_oauth_api import Credentials
    from src.utils import ANTIGRAVITY_USER_AGENT

    credential_path = spec.credential_path.resolve()
    data = json.loads(credential_path.read_text(encoding="utf-8"))
    credentials = Credentials.from_dict(data)
    if proxy:
        credentials.proxy_url = proxy
    await credentials.refresh_if_needed()
    if not credentials.access_token:
        raise ValueError("credential has no access token")
    if not credentials.project_id:
        raise ValueError("credential has no project_id")

    secrets = tuple(
        str(value)
        for key in ("token", "access_token", "refresh_token", "client_secret")
        if (value := data.get(key))
    ) + (credentials.access_token,)
    headers = {
        "User-Agent": ANTIGRAVITY_USER_AGENT,
        "Authorization": f"Bearer {credentials.access_token}",
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip",
    }
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds),
        proxy=proxy,
        trust_env=False,
    )
    result = AccountResult(
        label=spec.label,
        credential_file=credential_path.name,
        project_id=credentials.project_id,
        quota_before=None,
        quota_after=None,
        observations=[],
    )
    return _PreparedAccount(
        spec=spec,
        project_id=credentials.project_id,
        headers=headers,
        secrets=secrets,
        client=client,
        result=result,
    )


def _print_observation(
    account: _PreparedAccount,
    observation: RequestObservation,
    *,
    phase: str,
    index: int,
    total: int,
) -> None:
    outcome = "OK" if observation.success else "FAIL"
    error = f" | {observation.error}" if observation.error else ""
    print(
        f"[{account.spec.label}] {phase} {index}/{total} "
        f"{observation.variant}: {outcome} HTTP {observation.status_code} "
        f"{observation.duration_seconds:.3f}s images={observation.image_count}{error}",
        flush=True,
    )


async def run_demo(
    *,
    account_specs: Sequence[AccountSpec],
    api_base: str,
    model: str,
    prompt: str,
    rounds: int,
    warmup_rounds: int,
    delay_seconds: float,
    timeout_seconds: float,
    aspect_ratio: str,
    image_size: str,
    proxy: str | None,
) -> list[AccountResult]:
    prepared: list[_PreparedAccount] = []
    results: list[AccountResult] = []
    for spec in account_specs:
        try:
            account = await _prepare_account(
                spec,
                timeout_seconds=timeout_seconds,
                proxy=proxy,
            )
            prepared.append(account)
            results.append(account.result)
        except Exception as exc:
            results.append(
                AccountResult(
                    label=spec.label,
                    credential_file=spec.credential_path.name,
                    project_id="",
                    quota_before=None,
                    quota_after=None,
                    observations=[],
                    setup_error=_redact_text(f"{type(exc).__name__}: {exc}"),
                )
            )

    try:
        for account in prepared:
            account.result.quota_before = await fetch_quota_snapshot(
                client=account.client,
                api_base=api_base,
                headers=account.headers,
                project_id=account.project_id,
                secrets=account.secrets,
            )
            print(
                f"[{account.spec.label}] quota before: "
                f"{json.dumps(account.result.quota_before, ensure_ascii=False)}",
                flush=True,
            )

        async def run_phase(phase_rounds: int, *, measured: bool) -> None:
            if phase_rounds <= 0:
                return
            phase = "measure" if measured else "warmup"
            total = phase_rounds * 2
            per_account_index = {account.spec.label: 0 for account in prepared}
            for round_index in range(phase_rounds):
                account_order = prepared if round_index % 2 == 0 else list(reversed(prepared))
                variant_order: tuple[Variant, Variant] = (
                    ("legacy", "manager")
                    if round_index % 2 == 0
                    else ("manager", "legacy")
                )
                for account in account_order:
                    for variant in variant_order:
                        payload = _new_wrapped_request(
                            prompt=prompt,
                            project_id=account.project_id,
                            variant=variant,
                            aspect_ratio=aspect_ratio,
                            image_size=image_size,
                            model=model,
                        )
                        observation = await execute_image_request(
                            client=account.client,
                            api_base=api_base,
                            headers=account.headers,
                            payload=payload,
                            variant=variant,
                            secrets=account.secrets,
                        )
                        per_account_index[account.spec.label] += 1
                        _print_observation(
                            account,
                            observation,
                            phase=phase,
                            index=per_account_index[account.spec.label],
                            total=total,
                        )
                        if measured:
                            account.result.observations.append(observation)
                        if delay_seconds > 0:
                            await asyncio.sleep(delay_seconds)

        await run_phase(warmup_rounds, measured=False)
        await run_phase(rounds, measured=True)

        for account in prepared:
            account.result.quota_after = await fetch_quota_snapshot(
                client=account.client,
                api_base=api_base,
                headers=account.headers,
                project_id=account.project_id,
                secrets=account.secrets,
            )
            print(
                f"[{account.spec.label}] quota after: "
                f"{json.dumps(account.result.quota_after, ensure_ascii=False)}",
                flush=True,
            )
    finally:
        await asyncio.gather(*(account.client.aclose() for account in prepared))
    return results


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="A/B test legacy and Antigravity-Manager image request fingerprints"
    )
    parser.add_argument(
        "--account",
        action="append",
        type=_parse_account,
        required=True,
        help="Account label and credential path: LABEL=/path/to/account.json",
    )
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument(
        "--prompt",
        default="Generate a simple blue circle centered on a plain white background.",
    )
    parser.add_argument("--rounds", type=int, default=5, help="Measured requests per variant")
    parser.add_argument("--warmup-rounds", type=int, default=1)
    parser.add_argument("--delay-seconds", type=float, default=2.0)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--aspect-ratio", default="1:1")
    parser.add_argument("--image-size", default="512")
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--output", type=Path, default=None)
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    if args.rounds <= 0:
        raise ValueError("--rounds must be positive")
    if args.warmup_rounds < 0:
        raise ValueError("--warmup-rounds cannot be negative")
    if args.delay_seconds < 0:
        raise ValueError("--delay-seconds cannot be negative")
    if args.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be positive")
    api_base = validate_api_base(args.api_base)

    started = datetime.now().astimezone()
    accounts = await run_demo(
        account_specs=args.account,
        api_base=api_base,
        model=args.model,
        prompt=args.prompt,
        rounds=args.rounds,
        warmup_rounds=args.warmup_rounds,
        delay_seconds=args.delay_seconds,
        timeout_seconds=args.timeout_seconds,
        aspect_ratio=args.aspect_ratio,
        image_size=args.image_size,
        proxy=args.proxy,
    )
    finished = datetime.now().astimezone()
    report = build_report(
        accounts=accounts,
        api_base=api_base,
        model=args.model,
        prompt=args.prompt,
        rounds=args.rounds,
        warmup_rounds=args.warmup_rounds,
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
    )
    print(json.dumps(report["aggregate_summary"], ensure_ascii=False, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"report: {args.output.resolve()}")
    return 0 if any(account.observations for account in accounts) else 2


def main() -> int:
    parser = build_argument_parser()
    try:
        return asyncio.run(_async_main(parser.parse_args()))
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
