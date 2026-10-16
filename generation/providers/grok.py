import base64
import os

from openai import OpenAI

from .base import ImageProvider


class GrokProvider(ImageProvider):
    name = "grok"

    def generate(self, prompt: str) -> bytes:
        api_key = os.getenv("XAI_API_KEY")
        if not api_key or not api_key.strip():
            raise RuntimeError("Falta XAI_API_KEY")
        client = OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")
        model = os.getenv("XAI_IMAGE_MODEL", "grok-imagine-image-2.0")
        extra_body = {
            "aspect_ratio": "16:9",
            "resolution": os.getenv("XAI_IMAGE_RESOLUTION", "1k"),
        }
        if model.startswith("grok-imagine-image-2"):
            extra_body["quality"] = os.getenv("XAI_IMAGE_QUALITY", "medium")
        result = client.images.generate(
            model=model, prompt=prompt, n=1, response_format="b64_json", extra_body=extra_body,
        )
        if not result.data:
            raise RuntimeError("Grok no devolvió imagen (posible filtro de moderación)")
        image = result.data[0]
        if not getattr(image, "b64_json", None) or getattr(image, "respect_moderation", True) is False:
            raise RuntimeError("Grok no devolvió imagen (posible filtro de moderación)")
        return base64.b64decode(image.b64_json, validate=True)
