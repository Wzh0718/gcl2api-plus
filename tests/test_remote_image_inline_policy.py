"""远程图片内联策略：来源 IP 白名单的解析与网关行为。"""

import base64
import ipaddress
import json
import socket

import httpx
import pytest
from fastapi.responses import JSONResponse

import config
from src.client_ip import normalize_client_ip, pick_client_ip
from src.image_inline_policy import (
    InlinePolicyError,
    matches,
    parse_entries,
    validate_settings,
)


_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5y8AAAAASUVORK5CYII="
)
_BASE64_PNG = base64.b64encode(_PNG).decode()
_PUBLIC_IP = "93.184.216.34"
_IMAGE_URL = "https://images.example/photo.png"
_FINAL = {
    "candidates": [{
        "content": {"role": "model", "parts": [{"text": "ok"}]},
        "finishReason": "STOP",
    }],
}


# ---------------------------------------------------------------- 单元


@pytest.mark.parametrize(("raw", "expected"), [
    ("47.121.25.9", ["47.121.25.9/32"]),
    ("47.121.25.9\n10.20.30.0/24", ["47.121.25.9/32", "10.20.30.0/24"]),
    (["2001:db8::/32", " 192.168.1.0/24 "], ["2001:db8::/32", "192.168.1.0/24"]),
    ("47.121.25.9\n# 注释\n47.121.25.9", ["47.121.25.9/32"]),
    ("", []),
    (None, []),
])
def test_parse_entries(raw, expected):
    assert [str(network) for network in parse_entries(raw)] == expected


@pytest.mark.parametrize("raw", ["not-an-ip", "1.2.3.4/33", ["ok", 5], {"a": 1}])
def test_parse_entries_rejects_invalid(raw):
    with pytest.raises(InlinePolicyError):
        parse_entries(raw)


def test_matches_respects_address_family():
    entries = parse_entries(["10.0.0.0/8", "2001:db8::/32"])
    assert matches(entries, "10.1.2.3")
    assert not matches(entries, "11.1.2.3")
    assert matches(entries, "2001:db8::1")
    assert not matches(entries, None)
    assert not matches(entries, "not-an-ip")


@pytest.mark.parametrize(("raw", "expected"), [
    ("1.2.3.4", "1.2.3.4"),
    ("1.2.3.4:5678", "1.2.3.4"),
    ("[2001:db8::1]:443", "2001:db8::1"),
    ("2001:db8::1", "2001:db8::1"),
    ("", None),
    ("unknown", None),
])
def test_normalize_client_ip(raw, expected):
    assert normalize_client_ip(raw) == expected


def test_pick_client_ip_prefers_peer_unless_forwarded_trusted():
    scope = {"client": ("10.0.0.5", 1234), "headers": [(b"x-forwarded-for", b"47.121.25.9, 10.0.0.1")]}
    assert pick_client_ip(scope) == "10.0.0.5"
    assert pick_client_ip(scope, trust_forwarded=True) == "47.121.25.9"


def test_validate_settings_normalizes_and_rejects():
    payload = {"remote_image_inline_mode": "ip_list", "remote_image_inline_ips": "47.121.25.9\n10.0.0.0/8"}
    validate_settings(payload)
    assert payload["remote_image_inline_ips"] == ["47.121.25.9/32", "10.0.0.0/8"]

    for bad in (
        {"remote_image_inline_mode": "sometimes"},
        {"remote_image_inline_ips": "1.2.3.4/99"},
        {"remote_image_inline_trust_forwarded": "yes"},
    ):
        with pytest.raises(InlinePolicyError):
            validate_settings(dict(bad))


# ---------------------------------------------------------------- 网关


@pytest.fixture
def upstream(monkeypatch):
    monkeypatch.setattr(config, "_config_initialized", True)
    monkeypatch.setattr(config, "_config_cache", {})
    seen = []

    async def non_stream_request(*, body, **kwargs):
        seen.append(body)
        return JSONResponse(_FINAL)

    from src.api import antigravity as backend
    from src.api import vertex

    for channel in (backend, vertex):
        monkeypatch.setattr(channel, "non_stream_request", non_stream_request)
    return seen


@pytest.fixture
def image_transport(monkeypatch):
    requests = []

    def resolve(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (_PUBLIC_IP, port))]

    async def send(transport, request):
        requests.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(_PNG))

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    return requests


@pytest.fixture
async def gateway(monkeypatch):
    from web import app

    monkeypatch.setattr(config, "_config_initialized", True)
    monkeypatch.setattr(config, "_config_cache", {})
    monkeypatch.setenv("API_PASSWORD", "test")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer test"},
    ) as client:
        yield client


def _gemini_payload():
    return {"contents": [{"parts": [
        {"text": "Describe"},
        {"fileData": {"mimeType": "image/png", "fileUri": _IMAGE_URL}},
    ]}]}


def _sent_parts(upstream):
    return [
        part
        for content in upstream[0]["request"]["contents"]
        for part in content["parts"]
    ]


@pytest.mark.parametrize(("mode", "ips", "expected_inline"), [
    (None, None, True),                                    # 默认 all：保持历史行为
    ("all", [], True),
    ("ip_list", ["127.0.0.1"], True),                      # 命中来源 IP
    ("ip_list", ["127.0.0.0/8"], True),
    ("ip_list", ["10.0.0.0/8"], False),                    # 未命中：透传
    ("ip_list", [], False),
    ("off", ["127.0.0.1"], False),
])
async def test_inline_policy_gateway(gateway, upstream, image_transport, monkeypatch,
                                    mode, ips, expected_inline):
    cache = {}
    if mode is not None:
        cache["remote_image_inline_mode"] = mode
    if ips is not None:
        cache["remote_image_inline_ips"] = ips
    monkeypatch.setattr(config, "_config_cache", cache)

    response = await gateway.post(
        "/antigravity/v1/models/gemini-3.1-flash-lite:generateContent",
        json=_gemini_payload(),
    )
    assert response.status_code == 200, response.text
    parts = _sent_parts(upstream)
    if expected_inline:
        assert len(image_transport) == 1
        assert {"inlineData": {"mimeType": "image/png", "data": _BASE64_PNG}} in parts
        assert all("fileData" not in part for part in parts)
    else:
        assert image_transport == []
        assert {"fileData": {"mimeType": "image/png", "fileUri": _IMAGE_URL}} in parts


async def test_forwarded_header_selects_allowlisted_origin(gateway, upstream, image_transport, monkeypatch):
    monkeypatch.setattr(config, "_config_cache", {
        "remote_image_inline_mode": "ip_list",
        "remote_image_inline_ips": ["47.121.25.9"],
        "remote_image_inline_trust_forwarded": True,
    })
    response = await gateway.post(
        "/antigravity/v1/models/gemini-3.1-flash-lite:generateContent",
        json=_gemini_payload(),
        headers={"X-Forwarded-For": "47.121.25.9, 10.0.0.1"},
    )
    assert response.status_code == 200, response.text
    assert len(image_transport) == 1
    assert {"inlineData": {"mimeType": "image/png", "data": _BASE64_PNG}} in _sent_parts(upstream)


async def test_forwarded_header_ignored_when_untrusted(gateway, upstream, image_transport, monkeypatch):
    monkeypatch.setattr(config, "_config_cache", {
        "remote_image_inline_mode": "ip_list",
        "remote_image_inline_ips": ["47.121.25.9"],
    })
    response = await gateway.post(
        "/antigravity/v1/models/gemini-3.1-flash-lite:generateContent",
        json=_gemini_payload(),
        headers={"X-Forwarded-For": "47.121.25.9"},
    )
    assert response.status_code == 200, response.text
    assert image_transport == []
    assert all(
        part.get("fileData", {}).get("fileUri") == _IMAGE_URL for part in _sent_parts(upstream)
        if "fileData" in part
    )


def test_allowlist_cidr_matches_ipv6():
    entries = parse_entries(["2001:db8::/32"])
    assert matches(entries, str(ipaddress.ip_address("2001:db8::5")))


# ---------------------------------------------------------------- 面板配置


@pytest.fixture
def config_app(monkeypatch):
    from unittest.mock import AsyncMock

    from fastapi import FastAPI

    from src.panel import config_routes

    monkeypatch.setattr(config, "_config_initialized", True)
    monkeypatch.setattr(config, "_config_cache", {})
    values = {}

    class Storage:
        async def set_config(self, key, value):
            values[key] = value
            return True

        async def get_all_config(self):
            return values

    async def reload_config():
        config._config_cache.update(values)

    monkeypatch.setattr(config_routes, "get_storage_adapter", AsyncMock(return_value=Storage()))
    monkeypatch.setattr(config, "reload_config", reload_config)
    app = FastAPI()
    app.include_router(config_routes.router)
    app.dependency_overrides[config_routes.verify_panel_token] = lambda: "ok"
    return app, values


async def test_panel_saves_and_reads_inline_policy(config_app):
    app, values = config_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/config/save", json={"config": {
            "remote_image_inline_mode": "ip_list",
            "remote_image_inline_ips": ["47.121.25.9", "10.20.30.0/24"],
            "remote_image_inline_trust_forwarded": False,
        }})
        assert response.status_code == 200, response.text
        assert values["remote_image_inline_mode"] == "ip_list"
        assert values["remote_image_inline_ips"] == ["47.121.25.9/32", "10.20.30.0/24"]

        fetched = await client.get("/config/get")
        assert fetched.status_code == 200, fetched.text
        body = fetched.json()["config"] if "config" in fetched.json() else fetched.json()
        assert body["remote_image_inline_mode"] == "ip_list"
        assert body["remote_image_inline_ips"] == ["47.121.25.9/32", "10.20.30.0/24"]
        assert body["remote_image_inline_trust_forwarded"] is False

        for bad in (
            {"remote_image_inline_mode": "sometimes"},
            {"remote_image_inline_ips": ["1.2.3.4/99"]},
            {"remote_image_inline_trust_forwarded": "yes"},
        ):
            assert (await client.post("/config/save", json={"config": bad})).status_code == 400
