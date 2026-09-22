import base64
import os

from openai import OpenAI

from .base import ImageProvider


class OpenAIProvider(ImageProvider):
    name = "openai"

    def generate(self, prompt: str) -> bytes:
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        result = client.images.generate(model="gpt-image-1", prompt=prompt, size="1536x1024")
        return base64.b64decode(result.data[0].b64_json)
