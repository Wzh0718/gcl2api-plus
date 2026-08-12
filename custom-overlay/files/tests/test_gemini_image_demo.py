import base64
import unittest

import httpx


from examples.gemini_image_demo import (
    GeminiImageConfig,
    build_generate_content_payload,
    extract_generated_images,
    generate_image,
)


class GeminiImageDemoTest(unittest.TestCase):
    def test_gcli2api_payload_uses_proxy_compatible_image_config(self):
        payload = build_generate_content_payload(
            prompt="画一只戴宇航员头盔的猫",
            api_style="gcli2api",
            aspect_ratio="16:9",
            image_size="2K",
        )

        self.assertEqual(
            payload,
            {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": "画一只戴宇航员头盔的猫"}],
                    }
                ],
                "generationConfig": {
                    "response_modalities": ["TEXT", "IMAGE"],
                    "image_config": {
                        "aspect_ratio": "16:9",
                        "image_size": "2K",
                    },
                },
            },
        )

    def test_official_payload_uses_google_generate_content_field_names(self):
        payload = build_generate_content_payload(
            prompt="A watercolor mountain village",
            api_style="official",
            aspect_ratio="1:1",
            image_size="1K",
        )

        self.assertEqual(
            payload["generationConfig"],
            {
                "responseModalities": ["TEXT", "IMAGE"],
                "responseFormat": {
                    "image": {
                        "aspectRatio": "1:1",
                        "imageSize": "1K",
                    }
                },
            },
        )

    def test_extract_generated_images_reads_inline_data(self):
        png_bytes = b"\x89PNG\r\n\x1a\n"
        response = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "图片已生成"},
                            {
                                "inlineData": {
                                    "mimeType": "image/png",
                                    "data": base64.b64encode(png_bytes).decode("ascii"),
                                }
                            },
                        ]
                    }
                }
            ]
        }

        images, texts = extract_generated_images(response)

        self.assertEqual(texts, ["图片已生成"])
        self.assertEqual(images, [("image/png", png_bytes)])

    def test_generate_image_sends_key_and_model_endpoint(self):
        png_data = base64.b64encode(b"demo-image").decode("ascii")

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(
                str(request.url),
                "http://47.88.76.213:18317/antigravity/v1beta/models/"
                "gemini-3.1-flash-image:generateContent",
            )
            self.assertEqual(request.headers["x-goog-api-key"], "demo-secret")
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "inlineData": {
                                            "mimeType": "image/png",
                                            "data": png_data,
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                },
            )

        config = GeminiImageConfig(api_key="demo-secret")
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            images, texts = generate_image("画一只猫", config=config, client=client)

        self.assertEqual(texts, [])
        self.assertEqual(images, [("image/png", b"demo-image")])

    def test_config_rejects_missing_api_key(self):
        with self.assertRaisesRegex(ValueError, "GEMINI_IMAGE_API_KEY"):
            GeminiImageConfig(api_key="")


if __name__ == "__main__":
    unittest.main()
