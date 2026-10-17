import base64
import os

import requests

from openai import OpenAI

from .base import ImageProvider, validate_aspect_ratio


class GrokProvider(ImageProvider):
    name = "grok"

    def generate(self, prompt: str, aspect_ratio: str = "16:9", references=None, resolution=None) -> bytes:
        validate_aspect_ratio(aspect_ratio)
        api_key = os.getenv("XAI_API_KEY")
        if not api_key or not api_key.strip():
            raise RuntimeError("Falta XAI_API_KEY")
        if references:
            return self._edit(prompt, aspect_ratio, references, api_key, resolution)
        client = OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")
        model = os.getenv("XAI_IMAGE_MODEL", "grok-imagine-image-2.0")
        extra_body = {
            "aspect_ratio": aspect_ratio,
            "resolution": resolution or os.getenv("XAI_IMAGE_RESOLUTION", "1k"),
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


    def _edit(self, prompt, aspect_ratio, references, api_key, resolution=None):
        model = os.getenv("XAI_EDIT_MODEL", "grok-imagine-image-2.0")
        payload = {
            "model": model, "prompt": prompt, "aspect_ratio": aspect_ratio, "response_format": "b64_json",
            "resolution": resolution or os.getenv("XAI_IMAGE_RESOLUTION", "1k"),
            "images": [{"type": "image_url", "url": "data:image/jpeg;base64," + base64.b64encode(ref).decode()}
                       for ref in references],
        }
        if model.startswith("grok-imagine-image-2"):
            payload["quality"] = os.getenv("XAI_IMAGE_QUALITY", "medium")
        format_retried = single_retried = False
        for _ in range(3):
            response = requests.post("https://api.x.ai/v1/images/edits", json=payload,
                                     headers={"Authorization": f"Bearer {api_key}"}, timeout=180)
            if 200 <= response.status_code < 300:
                break
            error = response.text.lower()
            if response.status_code in (400, 422):
                if any(name in error for name in ("response_format", "response format")) and not format_retried:
                    format_retried = True
                    payload = {key: value for key, value in payload.items() if key != "response_format"}
                    continue
                if len(references) == 1 and not single_retried:
                    single_retried = True
                    payload = {**payload, "image": payload["images"][0]}
                    del payload["images"]
                    continue
            raise RuntimeError(f"Grok edición HTTP {response.status_code}: {response.text[:300]}")
        else:
            raise RuntimeError(f"Grok edición HTTP {response.status_code}: {response.text[:300]}")
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError("Grok no devolvió imagen (posible filtro de moderación)") from exc
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("respect_moderation", True) is not False:
            if data[0].get("b64_json"):
                return base64.b64decode(data[0]["b64_json"], validate=True)
            if data[0].get("url"):
                image = requests.get(data[0]["url"], timeout=60)
                image.raise_for_status()
                return image.content
        raise RuntimeError("Grok no devolvió imagen (posible filtro de moderación)")
