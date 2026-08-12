import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from src.converter.gemini_fix import normalize_gemini_request
from src.converter.openai2gemini import convert_openai_to_gemini_request
from src.converter.gemini_fix import prepare_image_generation_request
from src.models import GeminiRequest, model_to_dict


BASE_MODEL = "gemini-3.1-flash-image"
OFFICIAL_ASPECT_RATIOS = (
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
)
OFFICIAL_IMAGE_SIZES = ("512", "1K", "2K", "4K")


def normalize_native_request(payload, model=BASE_MODEL):
    parsed = model_to_dict(GeminiRequest.model_validate(payload))
    parsed["model"] = model
    return prepare_image_generation_request(parsed, model)


def test_official_response_format_controls_base_image_model():
    result = normalize_native_request(
        {
            "contents": [{"parts": [{"text": "Create a portrait poster"}]}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "responseFormat": {
                    "image": {"aspectRatio": "9:16", "imageSize": "2K"}
                },
                "seed": 42,
            },
        }
    )

    assert result["model"] == BASE_MODEL
    assert result["contents"][0]["role"] == "user"
    assert result["generationConfig"] == {
        "responseModalities": ["IMAGE"],
        "seed": 42,
        "candidateCount": 1,
        "imageConfig": {"aspectRatio": "9:16", "imageSize": "2K"},
    }


def test_official_response_format_takes_priority_over_legacy_size_and_suffix():
    result = normalize_native_request(
        {
            "contents": [{"role": "user", "parts": [{"text": "Create an image"}]}],
            "size": "1024x1024",
            "generationConfig": {
                "imageConfig": {"aspectRatio": "3:2", "imageSize": "1K"},
                "responseFormat": {
                    "image": {"aspectRatio": "1:8", "imageSize": "4K"}
                },
            },
        },
        model="gemini-3.1-flash-image-2k-16x9",
    )

    assert result["generationConfig"]["imageConfig"] == {
        "aspectRatio": "1:8",
        "imageSize": "4K",
    }


def test_legacy_camel_case_image_config_is_supported():
    result = normalize_native_request(
        {
            "contents": [{"role": "user", "parts": [{"text": "Create an image"}]}],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {"aspectRatio": "4:5", "imageSize": "1K"},
            },
        }
    )

    assert result["generationConfig"]["responseModalities"] == ["TEXT", "IMAGE"]
    assert result["generationConfig"]["imageConfig"] == {
        "aspectRatio": "4:5",
        "imageSize": "1K",
    }


def test_google_sdk_snake_case_image_config_is_supported():
    result = normalize_native_request(
        {
            "contents": [{"role": "user", "parts": [{"text": "Create an image"}]}],
            "generationConfig": {
                "response_modalities": ["IMAGE"],
                "image_config": {"aspect_ratio": "3:2", "image_size": "512"},
            },
        }
    )

    assert result["generationConfig"]["responseModalities"] == ["IMAGE"]
    assert result["generationConfig"]["imageConfig"] == {
        "aspectRatio": "3:2",
        "imageSize": "512",
    }


def test_openai_style_size_supports_official_512_tier():
    result = prepare_image_generation_request(
        {
            "model": BASE_MODEL,
            "contents": [{"role": "user", "parts": [{"text": "Create an icon"}]}],
            "size": "512x512",
        },
        BASE_MODEL,
    )

    assert result["generationConfig"]["imageConfig"] == {
        "aspectRatio": "1:1",
        "imageSize": "512",
    }


def test_old_model_suffix_remains_available_as_fallback():
    result = prepare_image_generation_request(
        {
            "model": "gemini-3.1-flash-image-2k-9x16",
            "contents": [{"role": "user", "parts": [{"text": "Create a wallpaper"}]}],
        },
        "gemini-3.1-flash-image-2k-9x16",
    )

    assert result["model"] == BASE_MODEL
    assert result["generationConfig"]["imageConfig"] == {
        "aspectRatio": "9:16",
        "imageSize": "2K",
    }


def test_image_model_forces_single_candidate():
    result = normalize_native_request(
        {
            "contents": [{"parts": [{"text": "Create two images"}]}],
            "generationConfig": {
                "candidateCount": 2,
                "responseFormat": {
                    "image": {"aspectRatio": "1:1", "imageSize": "512"}
                },
            },
        }
    )

    assert result["generationConfig"]["candidateCount"] == 1


def test_official_image_request_keeps_tools_and_system_instruction():
    result = normalize_native_request(
        {
            "contents": [{"role": "user", "parts": [{"text": "Create an image"}]}],
            "systemInstruction": {"parts": [{"text": "Use a clean editorial style"}]},
            "tools": [{"google_search": {}}],
            "generationConfig": {
                "responseFormat": {
                    "image": {"aspectRatio": "16:9", "imageSize": "1K"}
                }
            },
        }
    )

    assert result["systemInstruction"] == {
        "parts": [{"text": "Use a clean editorial style"}]
    }
    assert result["tools"] == [{"google_search": {}}]


@pytest.mark.parametrize("aspect_ratio", OFFICIAL_ASPECT_RATIOS)
@pytest.mark.parametrize("image_size", OFFICIAL_IMAGE_SIZES)
def test_all_official_ratio_and_size_values_are_preserved(aspect_ratio, image_size):
    result = normalize_native_request(
        {
            "contents": [{"parts": [{"text": "Create an image"}]}],
            "generationConfig": {
                "responseFormat": {
                    "image": {
                        "aspectRatio": aspect_ratio,
                        "imageSize": image_size,
                    }
                }
            },
        }
    )

    assert result["generationConfig"]["imageConfig"] == {
        "aspectRatio": aspect_ratio,
        "imageSize": image_size,
    }


@pytest.mark.parametrize(
    ("size", "expected"),
    (
        ("512x512", {"aspectRatio": "1:1", "imageSize": "512"}),
        ("1024x1024", {"aspectRatio": "1:1", "imageSize": "1K"}),
        ("1024x1536", {"aspectRatio": "2:3", "imageSize": "2K"}),
        ("1536x1024", {"aspectRatio": "3:2", "imageSize": "2K"}),
        ("4096x1024", {"aspectRatio": "4:1", "imageSize": "4K"}),
    ),
)
def test_openai_size_mapping(size, expected):
    result = prepare_image_generation_request(
        {
            "model": BASE_MODEL,
            "contents": [{"role": "user", "parts": [{"text": "Create an image"}]}],
            "size": size,
        },
        BASE_MODEL,
    )

    assert result["generationConfig"]["imageConfig"] == expected


@pytest.mark.asyncio
async def test_native_request_passes_through_full_antigravity_normalizer():
    request = GeminiRequest.model_validate(
        {
            "contents": [{"parts": [{"text": "Create a banner"}]}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "responseFormat": {
                    "image": {"aspectRatio": "21:9", "imageSize": "2K"}
                },
            },
        }
    )
    payload = model_to_dict(request)
    payload["model"] = BASE_MODEL

    result = await normalize_gemini_request(payload, mode="antigravity")

    assert result["model"] == BASE_MODEL
    assert result["contents"][0]["role"] == "user"
    assert result["generationConfig"] == {
        "responseModalities": ["IMAGE"],
        "candidateCount": 1,
        "imageConfig": {"aspectRatio": "21:9", "imageSize": "2K"},
    }


@pytest.mark.asyncio
async def test_openai_chat_size_passes_through_full_antigravity_normalizer():
    payload = await convert_openai_to_gemini_request(
        {
            "model": BASE_MODEL,
            "messages": [{"role": "user", "content": "Create a portrait"}],
            "size": "1024x1536",
        }
    )
    payload["model"] = BASE_MODEL

    result = await normalize_gemini_request(payload, mode="antigravity")

    assert result["model"] == BASE_MODEL
    assert result["generationConfig"]["imageConfig"] == {
        "aspectRatio": "2:3",
        "imageSize": "2K",
    }


@pytest.mark.asyncio
async def test_path_style_key_suffix_routes_to_antigravity_image_handler(monkeypatch):
    captured = {}

    async def fake_get_api_password():
        return "compat-test-key"

    async def fake_non_stream_request(*, body, api_key_id):
        captured["body"] = body
        captured["api_key_id"] = api_key_id
        return JSONResponse(content={"response": {"candidates": []}})

    monkeypatch.setattr("src.utils.get_api_password", fake_get_api_password)
    monkeypatch.setattr(
        "src.api.antigravity.non_stream_request", fake_non_stream_request
    )

    from src.router.antigravity.gemini import router

    app = FastAPI()
    app.include_router(router)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/antigravity/v1beta/models/"
            "gemini-3.1-flash-image-preview:generateContent/"
            "key=compat-test-key",
            json={
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": "生成一张海滩图片"}],
                    }
                ],
                "generationConfig": {
                    "responseModalities": ["IMAGE"],
                    "imageConfig": {"aspectRatio": "16:9", "imageSize": "1K"},
                },
            },
        )

    assert response.status_code == 200
    assert response.json() == {"candidates": []}
    assert captured["api_key_id"] == "env"
    assert captured["body"]["model"] == BASE_MODEL
    assert captured["body"]["request"]["generationConfig"]["imageConfig"] == {
        "aspectRatio": "16:9",
        "imageSize": "1K",
    }
