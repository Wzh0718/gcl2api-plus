import base64
import json

import pytest
from fastapi import Response

from src.api_keys import ApiKeyPrincipal
from src.models import OpenAIImageGenerationRequest
from src.router.antigravity.openai import image_generations


BASE_MODEL = "gemini-3.1-flash-image"


def principal():
    return ApiKeyPrincipal(
        api_key_id="managed-key",
        name="Test key",
        kind="managed",
        key_prefix="test",
    )


def gemini_image_response(image_bytes=b"jpeg-image"):
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
                        "promptTokenCount": 5,
                        "candidatesTokenCount": 7,
                        "totalTokenCount": 12,
                    },
                }
            }
        ),
        status_code=200,
        media_type="application/json",
    )


@pytest.mark.asyncio
async def test_images_generations_uses_openai_shape_and_official_base_model(monkeypatch):
    from src.api import antigravity

    calls = []

    async def fake_non_stream_request(body, api_key_id):
        calls.append((body, api_key_id))
        return gemini_image_response()

    monkeypatch.setattr(antigravity, "non_stream_request", fake_non_stream_request)

    response = await image_generations(
        OpenAIImageGenerationRequest(
            model=BASE_MODEL,
            prompt="Create a portrait poster",
            size="1024x1536",
            response_format="b64_json",
        ),
        principal(),
    )

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["data"] == [
        {"b64_json": base64.b64encode(b"jpeg-image").decode()}
    ]
    assert payload["usage"] == {
        "input_tokens": 5,
        "output_tokens": 7,
        "total_tokens": 12,
    }
    assert payload["size"] == "1024x1536"
    assert payload["quality"] == "auto"
    assert payload["output_format"] == "jpeg"
    assert payload["background"] == "auto"
    assert calls[0][1] == "managed-key"
    assert calls[0][0]["model"] == BASE_MODEL
    assert calls[0][0]["request"]["generationConfig"]["imageConfig"] == {
        "aspectRatio": "2:3",
        "imageSize": "2K",
    }


@pytest.mark.asyncio
async def test_images_generations_accepts_any_protocol_model_name(monkeypatch):
    from src.api import antigravity

    calls = []

    async def fake_non_stream_request(body, api_key_id):
        calls.append(body)
        return gemini_image_response()

    monkeypatch.setattr(antigravity, "non_stream_request", fake_non_stream_request)

    response = await image_generations(
        OpenAIImageGenerationRequest(
            model="dall-e-3",
            prompt="Create an image",
        ),
        principal(),
    )

    assert response.status_code == 200
    # 协议层 model 不影响上游；规范化统一使用默认图片模型
    assert calls[0]["model"] == "gemini-3.1-flash-image"


@pytest.mark.asyncio
async def test_images_generations_supports_n_by_making_single_candidate_calls(monkeypatch):
    from src.api import antigravity

    calls = []

    async def fake_non_stream_request(body, api_key_id):
        calls.append(body)
        return gemini_image_response(f"image-{len(calls)}".encode())

    monkeypatch.setattr(antigravity, "non_stream_request", fake_non_stream_request)

    response = await image_generations(
        OpenAIImageGenerationRequest(
            model=BASE_MODEL,
            prompt="Create two icons",
            n=2,
            size="512x512",
        ),
        principal(),
    )

    payload = json.loads(response.body)
    assert len(calls) == 2
    assert [item["b64_json"] for item in payload["data"]] == [
        base64.b64encode(b"image-1").decode(),
        base64.b64encode(b"image-2").decode(),
    ]
    assert all(
        call["request"]["generationConfig"]["candidateCount"] == 1
        for call in calls
    )


@pytest.mark.asyncio
async def test_images_generations_auto_size_leaves_backend_default(monkeypatch):
    from src.api import antigravity

    calls = []

    async def fake_non_stream_request(body, api_key_id):
        calls.append(body)
        return gemini_image_response()

    monkeypatch.setattr(antigravity, "non_stream_request", fake_non_stream_request)

    await image_generations(
        OpenAIImageGenerationRequest(
            model=BASE_MODEL,
            prompt="Create an image",
            size="auto",
        ),
        principal(),
    )

    assert calls[0]["request"]["generationConfig"]["imageConfig"] == {}


@pytest.mark.asyncio
async def test_images_generations_rejects_png_output_format_with_openai_error_shape():
    response = await image_generations(
        OpenAIImageGenerationRequest(
            model=BASE_MODEL,
            prompt="Create an image",
            output_format="png",
        ),
        principal(),
    )

    assert response.status_code == 400
    payload = json.loads(response.body)
    assert payload["error"]["type"] == "invalid_request_error"
    assert "output_format='png'" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_images_generations_rejects_output_compression_with_openai_error_shape():
    response = await image_generations(
        OpenAIImageGenerationRequest(
            model=BASE_MODEL,
            prompt="Create an image",
            output_compression=80,
        ),
        principal(),
    )

    assert response.status_code == 400
    payload = json.loads(response.body)
    assert payload["error"]["type"] == "invalid_request_error"
    assert "output_compression" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_images_generations_stream_emits_completed_events(monkeypatch):
    from src.api import antigravity

    async def fake_non_stream_request(body, api_key_id):
        return gemini_image_response()

    monkeypatch.setattr(antigravity, "non_stream_request", fake_non_stream_request)

    response = await image_generations(
        OpenAIImageGenerationRequest(
            model=BASE_MODEL,
            prompt="Create two icons",
            n=2,
            stream=True,
            partial_images=2,
        ),
        principal(),
    )

    assert response.status_code == 200
    assert response.media_type == "text/event-stream"
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    events = [
        json.loads(line[6:])
        for line in b"".join(chunks).decode().splitlines()
        if line.startswith("data: ")
    ]
    assert len(events) == 2
    assert all(event["type"] == "image_generation.completed" for event in events)
    assert all("b64_json" in event for event in events)
    assert "usage" not in events[0]
    assert events[1]["usage"] == {
        "input_tokens": 10,
        "output_tokens": 14,
        "total_tokens": 24,
    }


@pytest.mark.asyncio
async def test_images_generations_rejects_url_response_format_with_openai_error_shape():
    response = await image_generations(
        OpenAIImageGenerationRequest(
            model=BASE_MODEL,
            prompt="Create an image",
            response_format="url",
        ),
        principal(),
    )

    assert response.status_code == 400
    payload = json.loads(response.body)
    assert payload["error"]["type"] == "invalid_request_error"
    assert "b64_json" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_images_generations_rejects_invalid_enum_values_with_openai_error_shape():
    response = await image_generations(
        OpenAIImageGenerationRequest(
            model=BASE_MODEL,
            prompt="Create an image",
            quality="ultra",
            output_format="gif",
        ),
        principal(),
    )

    assert response.status_code == 400
    payload = json.loads(response.body)
    assert payload["error"]["type"] == "invalid_request_error"
    assert "quality=ultra" in payload["error"]["message"]
    assert "output_format=gif" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_images_generations_accepts_spec_enums_as_noop(monkeypatch):
    from src.api import antigravity

    calls = []

    async def fake_non_stream_request(body, api_key_id):
        calls.append(body)
        return gemini_image_response()

    monkeypatch.setattr(antigravity, "non_stream_request", fake_non_stream_request)

    response = await image_generations(
        OpenAIImageGenerationRequest(
            model=BASE_MODEL,
            prompt="Create an image",
            quality="high",
            background="transparent",
            moderation="low",
            style="vivid",
        ),
        principal(),
    )

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["quality"] == "high"
    assert payload["background"] == "transparent"
    assert len(calls) == 1
