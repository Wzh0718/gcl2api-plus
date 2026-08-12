import base64
import json

import pytest
from fastapi import Response
from starlette.requests import Request

from src.api_keys import ApiKeyPrincipal
from src.router.antigravity.openai import image_edits


BASE_MODEL = "gemini-3.1-flash-image"
BOUNDARY = "----gcli2api-test-boundary"


def principal():
    return ApiKeyPrincipal(
        api_key_id="managed-key",
        name="Test key",
        kind="managed",
        key_prefix="test",
    )


def multipart_request(fields=None, files=None):
    """Build a starlette Request carrying a multipart/form-data body."""
    body = b""
    for name, value in (fields or {}).items():
        body += (
            f"--{BOUNDARY}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()
    for name, (filename, content, content_type) in (files or {}).items():
        body += (
            f"--{BOUNDARY}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode() + content + b"\r\n"
    body += f"--{BOUNDARY}--\r\n".encode()

    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "headers": [
            (
                b"content-type",
                f"multipart/form-data; boundary={BOUNDARY}".encode(),
            )
        ],
    }
    return Request(scope, receive)


def gemini_image_response(image_bytes=b"edited-jpeg"):
    return Response(
        content=json.dumps(
            {
                "response": {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "inlineData": {
                                            "mimeType": "image/jpeg",
                                            "data": base64.b64encode(image_bytes).decode(),
                                        }
                                    }
                                ]
                            }
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 9,
                        "candidatesTokenCount": 12,
                        "totalTokenCount": 21,
                    },
                }
            }
        ),
        status_code=200,
        media_type="application/json",
    )


async def collect_sse_events(response):
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    return [
        json.loads(line[6:])
        for line in b"".join(chunks).decode().splitlines()
        if line.startswith("data: ")
    ]


@pytest.mark.asyncio
async def test_images_edits_sends_prompt_and_uploaded_image(monkeypatch):
    from src.api import antigravity

    calls = []

    async def fake_non_stream_request(body, api_key_id):
        calls.append((body, api_key_id))
        return gemini_image_response()

    monkeypatch.setattr(antigravity, "non_stream_request", fake_non_stream_request)

    request = multipart_request(
        fields={"model": BASE_MODEL, "prompt": "把背景改成海边", "size": "1024x1024"},
        files={"image": ("input.png", b"png-bytes", "image/png")},
    )
    response = await image_edits(request, principal())

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["data"] == [
        {"b64_json": base64.b64encode(b"edited-jpeg").decode()}
    ]
    assert payload["usage"] == {
        "input_tokens": 9,
        "output_tokens": 12,
        "total_tokens": 21,
    }
    assert payload["size"] == "1024x1024"
    assert payload["output_format"] == "jpeg"

    body, api_key_id = calls[0]
    assert api_key_id == "managed-key"
    assert body["model"] == BASE_MODEL
    parts = body["request"]["contents"][0]["parts"]
    assert parts[0] == {"text": "把背景改成海边"}
    assert parts[1] == {
        "inlineData": {
            "mimeType": "image/png",
            "data": base64.b64encode(b"png-bytes").decode(),
        }
    }
    assert body["request"]["generationConfig"]["imageConfig"] == {
        "aspectRatio": "1:1",
        "imageSize": "1K",
    }


@pytest.mark.asyncio
async def test_images_edits_accepts_multiple_images_and_data_url(monkeypatch):
    from src.api import antigravity

    calls = []

    async def fake_non_stream_request(body, api_key_id):
        calls.append(body)
        return gemini_image_response()

    monkeypatch.setattr(antigravity, "non_stream_request", fake_non_stream_request)

    data_url = "data:image/jpeg;base64," + base64.b64encode(b"jpeg-bytes").decode()
    request = multipart_request(
        fields={"model": BASE_MODEL, "prompt": "合并两张图", "image[]": data_url},
        files={"image": ("a.png", b"a-bytes", "image/png")},
    )
    response = await image_edits(request, principal())

    assert response.status_code == 200
    parts = calls[0]["request"]["contents"][0]["parts"]
    assert len(parts) == 3
    assert parts[1]["inlineData"]["mimeType"] == "image/png"
    assert parts[2]["inlineData"] == {
        "mimeType": "image/jpeg",
        "data": base64.b64encode(b"jpeg-bytes").decode(),
    }


@pytest.mark.asyncio
async def test_images_edits_supports_n_by_making_single_candidate_calls(monkeypatch):
    from src.api import antigravity

    calls = []

    async def fake_non_stream_request(body, api_key_id):
        calls.append(body)
        return gemini_image_response(f"edited-{len(calls)}".encode())

    monkeypatch.setattr(antigravity, "non_stream_request", fake_non_stream_request)

    request = multipart_request(
        fields={"model": BASE_MODEL, "prompt": "生成两个变体", "n": "2"},
        files={"image": ("input.png", b"png-bytes", "image/png")},
    )
    response = await image_edits(request, principal())

    payload = json.loads(response.body)
    assert len(calls) == 2
    assert [item["b64_json"] for item in payload["data"]] == [
        base64.b64encode(b"edited-1").decode(),
        base64.b64encode(b"edited-2").decode(),
    ]
    assert all(
        call["request"]["generationConfig"]["candidateCount"] == 1
        for call in calls
    )


@pytest.mark.asyncio
async def test_images_edits_accepts_any_protocol_model_name(monkeypatch):
    from src.api import antigravity

    calls = []

    async def fake_non_stream_request(body, api_key_id):
        calls.append(body)
        return gemini_image_response()

    monkeypatch.setattr(antigravity, "non_stream_request", fake_non_stream_request)

    request = multipart_request(
        fields={"model": "gpt-image-1", "prompt": "改图"},
        files={"image": ("input.png", b"png-bytes", "image/png")},
    )
    response = await image_edits(request, principal())

    assert response.status_code == 200
    # 协议层 model 不影响上游；规范化统一使用默认图片模型
    assert calls[0]["model"] == "gemini-3.1-flash-image"


@pytest.mark.asyncio
async def test_images_edits_rejects_webp_output_and_compression_with_openai_error_shape():
    request = multipart_request(
        fields={
            "model": BASE_MODEL,
            "prompt": "改图",
            "output_format": "webp",
        },
        files={"image": ("input.png", b"png-bytes", "image/png")},
    )
    response = await image_edits(request, principal())
    assert response.status_code == 400
    payload = json.loads(response.body)
    assert payload["error"]["type"] == "invalid_request_error"
    assert "output_format='webp'" in payload["error"]["message"]

    request = multipart_request(
        fields={
            "model": BASE_MODEL,
            "prompt": "改图",
            "output_compression": "80",
        },
        files={"image": ("input.png", b"png-bytes", "image/png")},
    )
    response = await image_edits(request, principal())
    assert response.status_code == 400
    payload = json.loads(response.body)
    assert "output_compression" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_images_edits_stream_emits_completed_events(monkeypatch):
    from src.api import antigravity

    async def fake_non_stream_request(body, api_key_id):
        return gemini_image_response()

    monkeypatch.setattr(antigravity, "non_stream_request", fake_non_stream_request)

    request = multipart_request(
        fields={"model": BASE_MODEL, "prompt": "改图", "stream": "true"},
        files={"image": ("input.png", b"png-bytes", "image/png")},
    )
    response = await image_edits(request, principal())

    assert response.status_code == 200
    assert response.media_type == "text/event-stream"
    events = await collect_sse_events(response)
    assert len(events) == 1
    assert events[0]["type"] == "image_generation.completed"
    assert events[0]["b64_json"] == base64.b64encode(b"edited-jpeg").decode()
    assert events[0]["usage"]["total_tokens"] == 21


@pytest.mark.asyncio
async def test_images_edits_requires_image_and_prompt_with_openai_error_shape():
    request = multipart_request(
        fields={"model": BASE_MODEL, "prompt": "没有图片"},
    )
    response = await image_edits(request, principal())
    assert response.status_code == 400
    payload = json.loads(response.body)
    assert payload["error"]["type"] == "invalid_request_error"
    assert "image" in payload["error"]["message"]

    request = multipart_request(
        fields={"model": BASE_MODEL},
        files={"image": ("input.png", b"png-bytes", "image/png")},
    )
    response = await image_edits(request, principal())
    assert response.status_code == 400
    payload = json.loads(response.body)
    assert payload["error"]["type"] == "invalid_request_error"
    assert "prompt" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_images_edits_rejects_mask_and_invalid_enums_with_openai_error_shape():
    request = multipart_request(
        fields={"model": BASE_MODEL, "prompt": "带遮罩"},
        files={
            "image": ("input.png", b"png-bytes", "image/png"),
            "mask": ("mask.png", b"mask-bytes", "image/png"),
        },
    )
    response = await image_edits(request, principal())
    assert response.status_code == 400
    payload = json.loads(response.body)
    assert payload["error"]["type"] == "invalid_request_error"
    assert "mask" in payload["error"]["message"]

    request = multipart_request(
        fields={
            "model": BASE_MODEL,
            "prompt": "非法枚举值",
            "quality": "ultra",
            "output_format": "gif",
        },
        files={"image": ("input.png", b"png-bytes", "image/png")},
    )
    response = await image_edits(request, principal())
    assert response.status_code == 400
    payload = json.loads(response.body)
    assert payload["error"]["type"] == "invalid_request_error"
    assert "quality=ultra" in payload["error"]["message"]
    assert "output_format=gif" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_images_edits_requires_multipart():
    scope = {
        "type": "http",
        "method": "POST",
        "headers": [(b"content-type", b"application/json")],
    }

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    response = await image_edits(Request(scope, receive), principal())
    assert response.status_code == 400
    payload = json.loads(response.body)
    assert payload["error"]["type"] == "invalid_request_error"
    assert "multipart" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_images_edits_validates_numeric_fields():
    request = multipart_request(
        fields={"model": BASE_MODEL, "prompt": "改图", "output_compression": "300"},
        files={"image": ("input.png", b"png-bytes", "image/png")},
    )
    response = await image_edits(request, principal())
    assert response.status_code == 400
    payload = json.loads(response.body)
    assert "output_compression" in payload["error"]["message"]

    request = multipart_request(
        fields={"model": BASE_MODEL, "prompt": "改图", "partial_images": "9"},
        files={"image": ("input.png", b"png-bytes", "image/png")},
    )
    response = await image_edits(request, principal())
    assert response.status_code == 400
    payload = json.loads(response.body)
    assert "partial_images" in payload["error"]["message"]
