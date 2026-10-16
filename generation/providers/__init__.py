import os

from .gemini import GeminiProvider
from .grok import GrokProvider
from .openai import OpenAIProvider

PROVIDERS = {"gemini": GeminiProvider, "openai": OpenAIProvider, "grok": GrokProvider}


def get_provider_name(name=None):
    return (name or os.getenv("IMAGE_PROVIDER", "gemini")).lower()


def get_provider(name=None):
    provider_name = get_provider_name(name)
    try:
        return PROVIDERS[provider_name]()
    except KeyError as exc:
        raise ValueError(f"Proveedor de imágenes desconocido: {provider_name}") from exc


def get_fallback_provider():
    fallback_name = os.getenv("IMAGE_FALLBACK_PROVIDER", "").strip().lower()
    if not fallback_name or fallback_name == get_provider_name():
        return None
    return get_provider(fallback_name)
