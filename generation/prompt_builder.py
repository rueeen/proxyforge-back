import hashlib
import json
import os
import re

from anthropic import Anthropic

PROMPT_VERSION = "v1"
EXPECTED_FIELDS = {"subject", "action", "setting", "palette", "style_notes", "composition", "full_prompt"}
SYSTEM_PROMPT = """Actúa como director de arte. Traduce los datos de una carta de Magic: The Gathering a una descripción visual original dentro del tema pedido. Conserva siempre su flavor mecánico: una remoción muestra destrucción, un buff potenciación y un contrahechizo interrupción.

Responde únicamente JSON válido, sin preámbulo ni bloques Markdown, con exactamente estas claves: subject, action, setting, palette (lista de tres colores hex), style_notes, composition y full_prompt (un solo párrafo listo para un generador de imágenes).

Deriva la paleta de sus colores de maná: blanco=luz/orden, azul=agua/conocimiento, negro=sombra/decadencia, rojo=fuego/caos, verde=naturaleza/crecimiento, incoloro=metal/vacío; para multicolor crea una mezcla coherente. La composición será horizontal, apaisada 16:9 y apta para recorte art_crop. La imagen contendrá solo ilustración: nunca texto, letras, números, logos, marcos de carta ni símbolos de maná.

No nombres franquicias, marcas registradas ni personajes protegidos, aunque el tema los pida. Traduce esas referencias a rasgos estéticos genéricos; por ejemplo, «Megaman» pasa a «robots azules retro-futuristas, ciencia ficción japonesa de los 80 y cel shading de colores planos»."""


def normalize_theme(theme: str) -> str:
    return " ".join(theme.lower().split())


def make_cache_key(oracle_id, theme: str, provider: str) -> str:
    value = f"{oracle_id}|{normalize_theme(theme)}|{PROMPT_VERSION}|{provider}"
    return hashlib.sha256(value.encode()).hexdigest()


def _clean_json(raw: str) -> dict:
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    result = json.loads(cleaned)
    missing = EXPECTED_FIELDS - result.keys() if isinstance(result, dict) else EXPECTED_FIELDS
    if missing:
        raise ValueError(f"Faltan campos JSON: {', '.join(sorted(missing))}")
    return result


def build_visual_prompt(card, theme: str) -> dict:
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    card_data = {
        "name": card.name, "type_line": card.type_line, "oracle_text": card.oracle_text,
        "flavor_text": card.flavor_text, "colors": card.colors,
        "color_identity": card.color_identity, "theme": theme,
    }
    messages = [{"role": "user", "content": json.dumps(card_data, ensure_ascii=False)}]
    raw_responses = []
    for attempt in range(2):
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=1000, system=SYSTEM_PROMPT, messages=messages
        )
        raw = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        raw_responses.append(raw)
        try:
            return _clean_json(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            if attempt == 0:
                messages.extend([
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": "Devuelve únicamente JSON válido con todas las claves solicitadas, sin fences Markdown."},
                ])
    raise ValueError(f"Claude devolvió JSON inválido tras dos intentos. Respuestas crudas: {raw_responses!r}")
