from pathlib import Path

import pytest

from examples.gemini_sdk_image_edit import (
    GeminiSdkImageEditConfig,
    edit_image,
    split_sdk_api_base,
)


class FakeImageConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeGenerateContentConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeHttpOptions:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeTypes:
    ImageConfig = FakeImageConfig
    GenerateContentConfig = FakeGenerateContentConfig
    HttpOptions = FakeHttpOptions


class FakeInputImage:
    pass


class FakePillowImage:
    opened_path = None

    @classmethod
    def open(cls, path):
        cls.opened_path = Path(path)
        return FakeInputImage()


class FakeInlineData:
    mime_type = "image/jpeg"
    data = b"edited-jpeg"


class FakePart:
    inline_data = FakeInlineData()
    text = None


class FakeResponse:
    parts = [FakePart()]


class FakeModels:
    def __init__(self):
        self.call = None

    def generate_content(self, **kwargs):
        self.call = kwargs
        return FakeResponse()


class FakeClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.models = FakeModels()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class FakeGenai:
    client = None

    @classmethod
    def Client(cls, **kwargs):
        cls.client = FakeClient(**kwargs)
        return cls.client


def test_split_sdk_api_base_removes_version_suffix():
    assert split_sdk_api_base("http://example.test/antigravity/v1") == (
        "http://example.test/antigravity",
        "v1",
    )
    assert split_sdk_api_base("http://example.test/antigravity/v1beta/") == (
        "http://example.test/antigravity",
        "v1beta",
    )


def test_edit_image_uses_gemini_sdk_multimodal_edit_request(tmp_path, monkeypatch):
    input_path = tmp_path / "input.png"
    input_path.write_bytes(b"input-png")
    output_path = tmp_path / "edited.png"

    monkeypatch.setattr(
        "examples.gemini_sdk_image_edit._load_sdk_dependencies",
        lambda: (FakeGenai, FakeTypes, FakePillowImage),
    )

    saved = edit_image(
        prompt="保留主体，只把背景改成海边",
        input_path=input_path,
        output_path=output_path,
        config=GeminiSdkImageEditConfig(
            api_key="test-key",
            api_base="http://example.test/antigravity/v1",
        ),
        aspect_ratio="1:1",
        image_size="512",
    )

    assert saved == [tmp_path / "edited.jpg"]
    assert saved[0].read_bytes() == b"edited-jpeg"
    assert FakePillowImage.opened_path == input_path

    client = FakeGenai.client
    assert client.kwargs["api_key"] == "test-key"
    assert client.kwargs["http_options"].kwargs == {
        "base_url": "http://example.test/antigravity",
        "api_version": "v1",
        "timeout": 180000,
    }
    call = client.models.call
    assert call["model"] == "gemini-3.1-flash-image"
    assert call["contents"] == [
        "保留主体，只把背景改成海边",
        call["contents"][1],
    ]
    assert isinstance(call["contents"][1], FakeInputImage)
    assert call["config"].kwargs["response_modalities"] == ["IMAGE"]
    assert call["config"].kwargs["image_config"].kwargs == {
        "aspect_ratio": "1:1",
        "image_size": "512",
    }


def test_edit_image_rejects_response_without_image(tmp_path, monkeypatch):
    input_path = tmp_path / "input.png"
    input_path.write_bytes(b"input-png")

    class TextOnlyResponse:
        parts = []

    monkeypatch.setattr(FakeModels, "generate_content", lambda self, **kwargs: TextOnlyResponse())
    monkeypatch.setattr(
        "examples.gemini_sdk_image_edit._load_sdk_dependencies",
        lambda: (FakeGenai, FakeTypes, FakePillowImage),
    )

    with pytest.raises(RuntimeError, match="没有返回图片"):
        edit_image(
            prompt="编辑图片",
            input_path=input_path,
            output_path=tmp_path / "edited.png",
            config=GeminiSdkImageEditConfig(
                api_key="test-key",
                api_base="http://example.test/antigravity/v1",
            ),
        )
