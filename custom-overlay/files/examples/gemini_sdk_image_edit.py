#!/usr/bin/env python3
"""Edit an image through gcli2api with the official google-genai SDK.

Install the example-only dependencies and run:

    uv run --with google-genai --with pillow \
      python examples/gemini_sdk_image_edit.py \
      input.png "保留主体，只把背景改成海边"

The API key is read from ``GEMINI_IMAGE_API_KEY``. The configured API base may
include ``/v1`` or ``/v1beta``; this module separates that suffix because the
SDK appends the API version itself.
"""

from __future__ import annotations

import argparse
import base64
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


DEFAULT_API_BASE = "http://127.0.0.1:7861/antigravity/v1"
DEFAULT_MODEL = "gemini-3.1-flash-image"


@dataclass(frozen=True)
class GeminiSdkImageEditConfig:
    api_key: str
    api_base: str = DEFAULT_API_BASE
    model: str = DEFAULT_MODEL
    timeout_seconds: float = 180.0

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ValueError("请设置 GEMINI_IMAGE_API_KEY")
        if not self.api_base.strip():
            raise ValueError("请设置 GEMINI_IMAGE_API_BASE")
        if not self.model.strip():
            raise ValueError("请设置 GEMINI_IMAGE_MODEL")
        if self.timeout_seconds <= 0:
            raise ValueError("GEMINI_IMAGE_TIMEOUT_SECONDS 必须大于 0")

    @classmethod
    def from_env(cls) -> "GeminiSdkImageEditConfig":
        load_dotenv()
        return cls(
            api_key=os.getenv("GEMINI_IMAGE_API_KEY", ""),
            api_base=os.getenv("GEMINI_IMAGE_API_BASE", DEFAULT_API_BASE),
            model=os.getenv("GEMINI_IMAGE_MODEL", DEFAULT_MODEL),
            timeout_seconds=float(os.getenv("GEMINI_IMAGE_TIMEOUT_SECONDS", "180")),
        )


def split_sdk_api_base(api_base: str) -> tuple[str, str]:
    """Return ``(base_url, api_version)`` for ``google.genai.Client``."""
    normalized = api_base.strip().rstrip("/")
    if not normalized:
        raise ValueError("GEMINI_IMAGE_API_BASE 不能为空")
    for suffix, version in (("/v1beta", "v1beta"), ("/v1", "v1")):
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)], version
    return normalized, "v1beta"


def _load_sdk_dependencies():
    try:
        from google import genai
        from google.genai import types
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "缺少示例依赖，请使用: uv run --with google-genai --with pillow "
            "python examples/gemini_sdk_image_edit.py ..."
        ) from exc
    return genai, types, Image


def _extension_for_mime_type(mime_type: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(str(mime_type or "").lower(), ".png")


def _decode_inline_bytes(data: Any) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return base64.b64decode(data, validate=True)
    raise RuntimeError("Gemini SDK 返回了无法识别的图片数据")


def edit_image(
    prompt: str,
    input_path: Path,
    output_path: Path,
    config: GeminiSdkImageEditConfig,
    *,
    aspect_ratio: str = "1:1",
    image_size: str = "1K",
) -> list[Path]:
    """Send ``prompt + input image`` and save all edited image parts."""
    if not prompt.strip():
        raise ValueError("图片编辑提示词不能为空")
    input_path = Path(input_path)
    output_path = Path(output_path)
    if not input_path.is_file():
        raise ValueError(f"输入图片不存在: {input_path}")

    genai, types, image_module = _load_sdk_dependencies()
    sdk_base_url, api_version = split_sdk_api_base(config.api_base)

    sdk_config = types.GenerateContentConfig(
        # 与 OpenAI /images/edits 一致：明确要求图片输出。
        response_modalities=["IMAGE"],
        image_config=types.ImageConfig(
            aspect_ratio=aspect_ratio,
            image_size=image_size,
        ),
    )

    with genai.Client(
        api_key=config.api_key,
        http_options=types.HttpOptions(
            base_url=sdk_base_url,
            api_version=api_version,
            timeout=int(config.timeout_seconds * 1000),
        ),
    ) as client:
        response = client.models.generate_content(
            model=config.model,
            # Gemini 图片编辑没有独立 edit 方法；图片与指令共同作为 contents。
            contents=[prompt.strip(), image_module.open(input_path)],
            config=sdk_config,
        )

    image_parts = [
        part
        for part in (getattr(response, "parts", None) or [])
        if getattr(part, "inline_data", None) is not None
    ]
    if not image_parts:
        text = "\n".join(
            str(part.text)
            for part in (getattr(response, "parts", None) or [])
            if getattr(part, "text", None)
        )
        summary = text[:500] if text else "空响应"
        raise RuntimeError(f"Gemini SDK 响应中没有返回图片: {summary}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    saved_paths: list[Path] = []
    for index, part in enumerate(image_parts, start=1):
        inline_data = part.inline_data
        suffix = _extension_for_mime_type(getattr(inline_data, "mime_type", ""))
        if len(image_parts) == 1:
            destination = output_path.with_suffix(suffix)
        else:
            destination = output_path.with_name(f"{output_path.stem}-{index}{suffix}")
        destination.write_bytes(_decode_inline_bytes(inline_data.data))
        saved_paths.append(destination)

    return saved_paths


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="使用 Gemini SDK 编辑输入图片")
    parser.add_argument("input", type=Path, help="需要编辑的输入图片")
    parser.add_argument("prompt", help="图片编辑指令")
    parser.add_argument("--output", type=Path, default=Path("edited_image.png"))
    parser.add_argument("--aspect-ratio", default="1:1")
    parser.add_argument("--image-size", default="1K")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        saved_paths = edit_image(
            prompt=args.prompt,
            input_path=args.input,
            output_path=args.output,
            config=GeminiSdkImageEditConfig.from_env(),
            aspect_ratio=args.aspect_ratio,
            image_size=args.image_size,
        )
        for path in saved_paths:
            print(f"编辑后的图片已保存: {path.resolve()}")
        return 0
    except (ValueError, RuntimeError) as exc:
        print(f"错误: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
