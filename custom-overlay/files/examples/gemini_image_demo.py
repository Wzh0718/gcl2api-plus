#!/usr/bin/env python3
"""Generate an image with gemini-3.1-flash-image.

Quick start:
    # Copy the variables from examples/gemini_image_demo.env.example into .env,
    # then replace GEMINI_IMAGE_API_KEY with your key.
    .venv/bin/python examples/gemini_image_demo.py "画一只戴宇航员头盔的猫"

The default configuration targets this project's Antigravity Gemini endpoint
using Google's official generateContent request field names. The legacy
gcli2api snake_case request remains available for backward compatibility.
"""

from __future__ import annotations

import argparse
import base64
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import httpx
from dotenv import load_dotenv


ApiStyle = Literal["gcli2api", "official"]
DEFAULT_MODEL = "gemini-3.1-flash-image"
DEFAULT_API_BASE = "http://47.88.76.213:18317/antigravity/v1beta"


@dataclass(frozen=True)
class GeminiImageConfig:
    api_key: str
    api_base: str = DEFAULT_API_BASE
    model: str = DEFAULT_MODEL
    api_style: ApiStyle = "official"
    timeout_seconds: float = 180.0

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ValueError("请设置 GEMINI_IMAGE_API_KEY")
        if self.api_style not in ("gcli2api", "official"):
            raise ValueError("GEMINI_IMAGE_API_STYLE 只能是 gcli2api 或 official")
        if not self.api_base.strip():
            raise ValueError("请设置 GEMINI_IMAGE_API_BASE")

    @classmethod
    def from_env(cls) -> "GeminiImageConfig":
        load_dotenv()
        return cls(
            api_key=os.getenv("GEMINI_IMAGE_API_KEY", ""),
            api_base=os.getenv("GEMINI_IMAGE_API_BASE", DEFAULT_API_BASE),
            model=os.getenv("GEMINI_IMAGE_MODEL", DEFAULT_MODEL),
            api_style=os.getenv("GEMINI_IMAGE_API_STYLE", "official"),  # type: ignore[arg-type]
            timeout_seconds=float(os.getenv("GEMINI_IMAGE_TIMEOUT_SECONDS", "180")),
        )

    @property
    def endpoint(self) -> str:
        encoded_model = quote(self.model, safe="-._~")
        return f"{self.api_base.rstrip('/')}/models/{encoded_model}:generateContent"


def build_generate_content_payload(
    prompt: str,
    api_style: ApiStyle,
    aspect_ratio: str,
    image_size: str,
) -> dict[str, Any]:
    if not prompt.strip():
        raise ValueError("图片提示词不能为空")

    if api_style == "official":
        # Official GenerateContent image schema:
        # https://ai.google.dev/gemini-api/docs/generate-content/image-generation
        generation_config = {
            "responseModalities": ["TEXT", "IMAGE"],
            "responseFormat": {
                "image": {
                    "aspectRatio": aspect_ratio,
                    "imageSize": image_size,
                }
            },
        }
    else:
        generation_config = {
            "response_modalities": ["TEXT", "IMAGE"],
            "image_config": {
                "aspect_ratio": aspect_ratio,
                "image_size": image_size,
            },
        }

    return {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt.strip()}],
            }
        ],
        "generationConfig": generation_config,
    }


def extract_generated_images(
    response_data: dict[str, Any],
) -> tuple[list[tuple[str, bytes]], list[str]]:
    images: list[tuple[str, bytes]] = []
    texts: list[str] = []

    for candidate in response_data.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            text = part.get("text")
            if text:
                texts.append(text)

            inline_data = part.get("inlineData") or part.get("inline_data")
            if not inline_data or not inline_data.get("data"):
                continue

            mime_type = inline_data.get("mimeType") or inline_data.get("mime_type")
            images.append(
                (
                    mime_type or "image/png",
                    base64.b64decode(inline_data["data"], validate=True),
                )
            )

    return images, texts


def generate_image(
    prompt: str,
    config: GeminiImageConfig,
    *,
    aspect_ratio: str = "1:1",
    image_size: str = "1K",
    client: httpx.Client | None = None,
) -> tuple[list[tuple[str, bytes]], list[str]]:
    payload = build_generate_content_payload(
        prompt=prompt,
        api_style=config.api_style,
        aspect_ratio=aspect_ratio,
        image_size=image_size,
    )
    owns_client = client is None
    active_client = client or httpx.Client(timeout=config.timeout_seconds)

    try:
        response = active_client.post(
            config.endpoint,
            headers={
                "Content-Type": "application/json",
                # Supported by both Google's API and this project's Gemini auth.
                "x-goog-api-key": config.api_key,
            },
            json=payload,
        )
        response.raise_for_status()
        response_data = response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:1000]
        raise RuntimeError(
            f"图片生成请求失败: HTTP {exc.response.status_code}: {detail}"
        ) from exc
    finally:
        if owns_client:
            active_client.close()

    images, texts = extract_generated_images(response_data)
    if not images:
        raise RuntimeError(f"接口响应中没有图片数据。响应摘要: {str(response_data)[:1000]}")
    return images, texts


def save_generated_images(
    images: list[tuple[str, bytes]], output_path: Path
) -> list[Path]:
    saved_paths: list[Path] = []
    output_path.parent.mkdir(parents=True, exist_ok=True)

    for index, (mime_type, image_bytes) in enumerate(images, start=1):
        suffix = _extension_for_mime_type(mime_type)
        if len(images) == 1:
            destination = output_path.with_suffix(output_path.suffix or suffix)
        else:
            destination = output_path.with_name(f"{output_path.stem}-{index}{suffix}")
        destination.write_bytes(image_bytes)
        saved_paths.append(destination)

    return saved_paths


def _extension_for_mime_type(mime_type: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(mime_type.lower(), ".png")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="调用 Gemini 3.1 Flash Image 生成图片")
    parser.add_argument("prompt", help="图片生成提示词")
    parser.add_argument("--output", default="generated_image.png", help="输出图片路径")
    parser.add_argument("--aspect-ratio", default="1:1", help="宽高比，例如 1:1、16:9")
    parser.add_argument("--image-size", default="1K", help="图片尺寸，例如 1K、2K、4K")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = GeminiImageConfig.from_env()
        images, texts = generate_image(
            args.prompt,
            config,
            aspect_ratio=args.aspect_ratio,
            image_size=args.image_size,
        )
        for text in texts:
            print(text)
        for saved_path in save_generated_images(images, Path(args.output)):
            print(f"图片已保存: {saved_path.resolve()}")
        return 0
    except (ValueError, RuntimeError, httpx.HTTPError) as exc:
        print(f"错误: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
