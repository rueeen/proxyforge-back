import base64
import os
import logging

from openai import OpenAI

from .base import ImageProvider, validate_aspect_ratio

logger = logging.getLogger(__name__)


class OpenAIProvider(ImageProvider):
    name = "openai"

    def generate(self, prompt: str, aspect_ratio: str = "16:9", references=None, resolution=None) -> bytes:
        validate_aspect_ratio(aspect_ratio)
        if references:
            logger.info("OpenAI ignora las imágenes de referencia; generando solo con texto")
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        result = client.images.generate(model="gpt-image-1", prompt=prompt, size="1024x1536" if aspect_ratio == "3:4" else "1536x1024")
        return base64.b64decode(result.data[0].b64_json)
