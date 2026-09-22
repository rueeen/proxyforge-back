import os

from .gemini import GeminiProvider
from .openai import OpenAIProvider

PROVIDERS = {"gemini": GeminiProvider, "openai": OpenAIProvider}


def get_provider_name(name=None):
    return (name or os.getenv("IMAGE_PROVIDER", "gemini")).lower()


def get_provider(name=None):
    provider_name = get_provider_name(name)
    try:
        return PROVIDERS[provider_name]()
    except KeyError as exc:
        raise ValueError(f"Proveedor de imágenes desconocido: {provider_name}") from exc
