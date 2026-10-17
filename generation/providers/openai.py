import base64
import os

from openai import OpenAI

from .base import ImageProvider, validate_aspect_ratio


class OpenAIProvider(ImageProvider):
    name = "openai"

    def generate(self, prompt: str, aspect_ratio: str = "16:9") -> bytes:
        validate_aspect_ratio(aspect_ratio)
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        result = client.images.generate(model="gpt-image-1", prompt=prompt, size="1024x1536" if aspect_ratio == "3:4" else "1536x1024")
        return base64.b64decode(result.data[0].b64_json)
