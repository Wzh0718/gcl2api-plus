import base64
import json
import socket
from types import SimpleNamespace

import httpx
import pytest
from fastapi.responses import JSONResponse

import config
from src.api import antigravity as backend
from src.api import vertex
from src.converter import remote_images
from src.models import OpenAIChatCompletionRequest
from src.router.antigravity.openai import chat_completions


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


def _chat_request(*urls, stream=False, model="gemini-3.1-flash-lite"):
    return OpenAIChatCompletionRequest.model_validate({
        "model": model,
        "stream": stream,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe the image"},
                *({"type": "image_url", "image_url": {"url": url}} for url in urls),
            ],
        }],
    })


@pytest.fixture
def requests_to_upstream(monkeypatch):
    monkeypatch.setattr(config, "_config_initialized", True)
    monkeypatch.setattr(config, "_config_cache", {})
    seen = []

    async def non_stream_request(*, body, **kwargs):
        seen.append(body)
        return JSONResponse(_FINAL)

    async def stream_request(*, body, **kwargs):
        seen.append(body)
        yield f"data: {json.dumps({'response': _FINAL})}\n\n".encode()

    for channel in (backend, vertex):
        monkeypatch.setattr(channel, "non_stream_request", non_stream_request)
        monkeypatch.setattr(channel, "stream_request", stream_request)
    return seen


@pytest.fixture
def image_transport(monkeypatch):
    def install(responses):
        requests = []

        def resolve(host, port, *args, **kwargs):
            address = "127.0.0.1" if host == "private.example" else _PUBLIC_IP
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]

        async def send(transport, request):
            requests.append(request)
            return responses[len(requests) - 1]

        monkeypatch.setattr(socket, "getaddrinfo", resolve)
        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
        return requests

    return install


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


def _image_request(protocol, image_url, stream):
    model = "gemini-3.1-flash-lite"
    if protocol == "chat":
        return _chat_request(image_url, stream=stream).model_dump(exclude_none=True)
    if protocol == "responses":
        return {"model": model, "stream": stream, "input": [{"role": "user", "content": [
            {"type": "input_text", "text": "Describe the image"},
            {"type": "input_image", "image_url": image_url},
        ]}]}
    if protocol == "anthropic":
        source = {"type": "url", "url": image_url}
        if image_url.startswith("data:"):
            source = {"type": "base64", "media_type": "image/png", "data": _BASE64_PNG}
        return {"model": model, "stream": stream, "max_tokens": 256, "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "Describe the image"},
                {"type": "image", "source": source},
            ]},
        ]}
    image = {"file_data": {"mime_type": "image/png", "file_uri": image_url}}
    if image_url.startswith("data:"):
        image = {"inlineData": {"mimeType": "image/png", "data": _BASE64_PNG}}
    return {"contents": [{"parts": [{"text": "Hi"}, image]}]}


_INFERENCE_ROUTES = [
    ("/v1/chat/completions", "chat"),
    ("/antigravity/v1/chat/completions", "chat"),
    ("/vertex/v1/chat/completions", "chat"),
    ("/v1/responses", "responses"),
    ("/antigravity/v1/responses", "responses"),
    ("/v1/messages", "anthropic"),
    ("/antigravity/v1/messages", "anthropic"),
    *(
        (f"{prefix}/models/gemini-3.1-flash-lite:{{method}}{key_suffix}", "gemini")
        for prefix in (
            "/v1", "/v1beta", "/antigravity/v1", "/antigravity/v1beta", "/vertex/v1", "/vertex/v1beta",
        )
        for key_suffix in (("",) if prefix.startswith("/vertex") else ("", "/key=test"))
    ),
]
_EDIT_ROUTES = ["/v1/images/edits", "/antigravity/v1/images/edits"]


def test_route_matrix_covers_mounted_inference_post_endpoints():
    from web import app

    mounted = {
        path for path, methods in app.openapi()["paths"].items()
        if "post" in methods and path.startswith(("/v1", "/antigravity/v1", "/vertex/v1"))
        and not path.endswith(("/images/generations", "/count_tokens", ":countTokens"))
    }
    covered = set(_EDIT_ROUTES)
    for path, protocol in _INFERENCE_ROUTES:
        if protocol == "gemini":
            for method in ("generateContent", "streamGenerateContent"):
                covered.add(
                    path.format(method=method).replace("gemini-3.1-flash-lite:", "{model}:")
                    .replace("/key=test", "/key={path_key}")
                )
        else:
            covered.add(path)
    assert covered == mounted


@pytest.mark.parametrize(("path", "protocol"), _INFERENCE_ROUTES)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("image_format", ["remote", "inline", "blocked"])
async def test_mounted_image_input_routes(
    path, protocol, stream, image_format, gateway, requests_to_upstream, image_transport
):
    path = path.format(method="streamGenerateContent" if stream else "generateContent")
    image_url = {
        "remote": _IMAGE_URL,
        "inline": f"data:image/png;base64,{_BASE64_PNG}",
        "blocked": "https://private.example/photo.png",
    }[image_format]
    downloads = image_transport(
        [httpx.Response(200, stream=httpx.ByteStream(_PNG))] if image_format == "remote" else []
    )
    response = await gateway.post(path, json=_image_request(protocol, image_url, stream))
    if image_format == "blocked":
        assert response.status_code == 400, response.text
        error = response.json()["error"]
        if protocol == "gemini":
            assert error["status"] == "INVALID_ARGUMENT"
        elif protocol == "anthropic":
            assert error["type"] == "invalid_request_error"
        else:
            assert error["code"] == "invalid_image_url"
        assert not downloads
        assert not requests_to_upstream
        return

    assert response.status_code == 200, response.text
    if stream:
        assert response.headers["content-type"].startswith("text/event-stream")
    assert len(downloads) == (1 if image_format == "remote" else 0)
    assert len(requests_to_upstream) == 1
    sent_parts = [
        part
        for message in requests_to_upstream[0]["request"]["contents"]
        for part in message["parts"]
    ]
    assert {"inlineData": {"mimeType": "image/png", "data": _BASE64_PNG}} in sent_parts
    assert all("fileData" not in part and "file_data" not in part for part in sent_parts)


async def test_gemini_preserves_existing_file_references(gateway, requests_to_upstream, image_transport):
    downloads = image_transport([])
    parts = [
        {"fileData": {"mimeType": "image/png", "fileUri": "gs://bucket/reference.png"}},
        {"fileData": {
            "mimeType": "image/png",
            "fileUri": "https://generativelanguage.googleapis.com/v1beta/files/reference123",
        }},
        {"fileData": {"mimeType": "application/pdf", "fileUri": "https://files.example/input.pdf"}},
    ]
    response = await gateway.post(
        "/antigravity/v1/models/gemini-3.1-flash-lite:generateContent",
        json={"contents": [{"parts": parts}]},
    )
    assert response.status_code == 200, response.text
    assert requests_to_upstream[0]["request"]["contents"][0]["parts"] == parts
    assert not downloads


@pytest.mark.parametrize(("prefix", "protocol"), [
    ("/antigravity/v1", "anthropic"),
    ("/v1", "gemini"),
    ("/antigravity/v1beta", "gemini"),
    ("/vertex/v1", "gemini"),
])
async def test_token_counting_estimates_inline_and_remote_images_equally(
    prefix, protocol, gateway, image_transport
):
    downloads = image_transport([])
    payload = _image_request(protocol, _IMAGE_URL, False)
    if protocol == "anthropic":
        path = prefix + "/messages/count_tokens"
        token_key = "input_tokens"
        parts = payload["messages"][0]["content"]
        inline_image = {"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": _BASE64_PNG,
        }}
    else:
        path = prefix + "/models/gemini-3.1-flash-lite:countTokens"
        token_key = "totalTokens"
        parts = payload["contents"][0]["parts"]
        inline_image = {"inlineData": {"mimeType": "image/png", "data": _BASE64_PNG}}
    remote = await gateway.post(path, json=payload)
    parts[-1] = inline_image
    inline = await gateway.post(path, json=payload)
    parts.pop()
    text_only = await gateway.post(path, json=payload)
    assert remote.status_code == inline.status_code == text_only.status_code == 200
    assert remote.json()[token_key] == inline.json()[token_key] > text_only.json()[token_key]
    assert not downloads


@pytest.mark.parametrize(
    ("model", "stream"),
    [
        ("gemini-3.1-flash-lite", False),
        ("gemini-3.1-flash-lite", True),
        ("gemini-3.1-flash-image", False),
    ],
)
async def test_chat_inlines_remote_image_and_keeps_data_url(
    model, stream, requests_to_upstream, image_transport
):
    downloads = image_transport([
        httpx.Response(302, headers={
            "location": "https://cdn.example/photo?signature=test",
            "set-cookie": "source=private",
        }),
        httpx.Response(
            200, headers={"content-type": "application/octet-stream"}, stream=httpx.ByteStream(_PNG)
        ),
    ])
    request = _chat_request(
        "https://images.example/photo",
        "data:image/jpeg;base64,YWJj",
        stream=stream,
        model=model,
    )
    response = await chat_completions(request, principal=SimpleNamespace(api_key_id="test"))
    assert response.status_code == 200
    if stream:
        chunks = [chunk async for chunk in response.body_iterator]
        assert b'"content": "ok"' in b"".join(chunks)
    assert [download.url.host for download in downloads] == [_PUBLIC_IP, _PUBLIC_IP]
    assert [download.headers["host"] for download in downloads] == ["images.example", "cdn.example"]
    assert [download.extensions["sni_hostname"] for download in downloads] == [
        "images.example", "cdn.example",
    ]
    assert downloads[1].url.query == b"signature=test"
    assert all("cookie" not in download.headers for download in downloads)
    assert requests_to_upstream[0]["request"]["contents"][0]["parts"] == [
        {"text": "Describe the image"},
        {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(_PNG).decode("ascii")}},
        {"inlineData": {"mimeType": "image/jpeg", "data": "YWJj"}},
    ]


@pytest.mark.parametrize(
    ("url", "responses", "download_count"),
    [
        ("http://private.example/photo", [], 0),
        ("https://images.example/photo", [
            httpx.Response(302, headers={"location": "http://private.example/photo"}),
        ], 1),
        ("https://images.example/photo", [httpx.Response(404)], 1),
        ("https://images.example/photo", [
            httpx.Response(
                200, headers={"content-type": "image/png"}, stream=httpx.ByteStream(b"<html>")
            ),
        ], 1),
    ],
)
async def test_chat_rejects_unusable_image_before_upstream(
    url, responses, download_count, gateway, requests_to_upstream, image_transport
):
    downloads = image_transport(responses)
    response = await gateway.post(
        "/v1/chat/completions", json=_image_request("chat", url, True),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_image_url"
    assert len(downloads) == download_count
    assert not requests_to_upstream


async def test_download_limits_apply_to_combined_response_bodies(
    monkeypatch, gateway, requests_to_upstream, image_transport
):
    monkeypatch.setattr(remote_images, "_MAX_REMOTE_BYTES", len(_PNG) * 2 - 1)
    downloads = image_transport([
        httpx.Response(200, stream=httpx.ByteStream(_PNG)),
        httpx.Response(200, stream=httpx.ByteStream(_PNG)),
    ])
    response = await gateway.post(
        "/v1/chat/completions",
        json=_chat_request(
            "https://images.example/first.png", "https://images.example/second.png",
        ).model_dump(exclude_none=True),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_image_url"
    assert len(downloads) == 2
    assert not requests_to_upstream


@pytest.mark.parametrize("path", _EDIT_ROUTES)
@pytest.mark.parametrize("multipart", [False, True])
@pytest.mark.parametrize("accepted", [True, False])
async def test_image_edits_accepts_url_and_existing_inline_image(
    path, multipart, accepted, gateway, image_transport, monkeypatch,
):
    downloads = image_transport(
        [httpx.Response(200, stream=httpx.ByteStream(_PNG))] if accepted else []
    )
    calls = []

    async def non_stream_request(*, body, **kwargs):
        calls.append(body)
        return JSONResponse({"candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "image/jpeg", "data": "ZWRpdGVk"}},
        ]}}]})

    monkeypatch.setattr(backend, "non_stream_request", non_stream_request)
    remote = _IMAGE_URL if accepted else "https://private.example/photo.png"
    inline = "data:image/jpeg;base64,YWJj"
    params = {"model": "gemini-3.1-flash-image", "prompt": "Add a green triangle"}
    if multipart:
        request_args = {"data": params, "files": [
            ("image[]", (None, remote)), ("image[]", (None, inline)),
        ]}
    else:
        request_args = {"json": {**params, "images": [{"image_url": remote}, {"image_url": inline}]}}
    response = await gateway.post(path, **request_args)

    if not accepted:
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_image_url"
        assert not downloads
        assert not calls
        return
    assert response.status_code == 200
    assert response.json()["data"] == [{"b64_json": "ZWRpdGVk"}]
    assert len(downloads) == 1
    assert calls[0]["request"]["contents"][0]["parts"] == [
        {"text": params["prompt"]},
        {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(_PNG).decode("ascii")}},
        {"inlineData": {"mimeType": "image/jpeg", "data": "YWJj"}},
    ]
