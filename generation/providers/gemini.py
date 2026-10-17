import os

from google import genai
from google.genai import types

from .base import ImageProvider, validate_aspect_ratio


class GeminiProvider(ImageProvider):
    name = "gemini"

    def generate(self, prompt: str, aspect_ratio: str = "16:9", references=None) -> bytes:
        validate_aspect_ratio(aspect_ratio)
        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        if hasattr(types, "ImageConfig"):
            config = types.GenerateContentConfig(
                response_modalities=["IMAGE"], image_config=types.ImageConfig(aspect_ratio=aspect_ratio),
            )
        else:
            # google-genai 1.38 supports extra_body but predates the ImageConfig type.
            config = types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                http_options=types.HttpOptions(extra_body={
                    "generationConfig": {"imageConfig": {"aspectRatio": aspect_ratio}},
                }),
            )
        contents = [prompt, *[types.Part.from_bytes(data=ref, mime_type="image/jpeg") for ref in references]] if references else prompt
        response = client.models.generate_content(
            model="gemini-2.5-flash-image", contents=contents, config=config,
        )
        for candidate in response.candidates or []:
            for part in candidate.content.parts or []:
                if part.inline_data and part.inline_data.data:
                    return part.inline_data.data
        raise RuntimeError("Gemini no devolvió ninguna imagen")
