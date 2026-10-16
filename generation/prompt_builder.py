import hashlib
import logging
import json
import os
import re

from anthropic import Anthropic, APIStatusError

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v3"
EXPECTED_FIELDS = {"subject", "action", "setting", "palette", "style_notes", "composition", "full_prompt", "fallback_prompt", "concept_source", "themed_name", "themed_flavor", "frame_style"}
SYSTEM_PROMPT = """Actúa como director de arte. Traduce los datos de una carta de Magic: The Gathering a una descripción visual original dentro del tema pedido. Conserva siempre su flavor mecánico: una remoción muestra destrucción, un buff potenciación y un contrahechizo interrupción.

Responde únicamente JSON válido, sin preámbulo ni bloques Markdown, con exactamente estas claves: subject, action, setting, palette (lista de tres colores hex), style_notes, composition, full_prompt (un solo párrafo listo para un generador de imágenes), fallback_prompt, concept_source, themed_name, themed_flavor y frame_style.

Si recibes la ilustración original, describe primero en concept_source qué concepto representa: sujeto, acción, emoción y relación con la mecánica. Luego reinterpreta ese concepto dentro del tema.

themed_name es un nombre alternativo en español de 30 caracteres o menos, coherente con el tema y reconocible respecto del original. themed_flavor es flavor text en español de 140 caracteres o menos, ambientado en el tema y sin comillas envolventes.

frame_style es un objeto con border, background, text_box y accent (colores hex #RRGGBB), y texture (texto corto, como papel acuarela o metal cepillado). Deriva los colores del maná y del tema, con suficiente contraste para texto oscuro sobre text_box. Este objeto describe el marco que se compondrá aparte, no debe aparecer en la ilustración.

Nunca reescribas el coste de maná, tipo, texto de reglas, fuerza/resistencia ni lealtad. Esos datos no forman parte de la respuesta.

Deriva la paleta de sus colores de maná: blanco=luz/orden, azul=agua/conocimiento, negro=sombra/decadencia, rojo=fuego/caos, verde=naturaleza/crecimiento, incoloro=metal/vacío; para multicolor crea una mezcla coherente. La composición será horizontal, apaisada 16:9 y apta para recorte art_crop. La imagen contendrá solo ilustración: nunca texto, letras, números, logos, marcos de carta ni símbolos de maná.

Si recibes brief, contiene decisiones obligatorias del usuario. En especial, el estilo visual elegido define la técnica, el acabado y el tono tanto de full_prompt como de fallback_prompt.

Si recibes assignment, representa exactamente ese elemento y nómbralo explícitamente en full_prompt, con sus rasgos canónicos: diseño, colores, vestuario, armas y escenario característicos de la franquicia, siguiendo visual_brief. Combina assignment con la mecánica de la carta y la ilustración original como referencia conceptual. fallback_prompt describe ese mismo elemento sin nombres propios, conservando sus rasgos visuales. themed_name debe partir del element asignado.

Sigue el tema del usuario de forma literal y con máxima fidelidad. Si nombra franquicias, personajes, obras, estudios o artistas, úsalos por su nombre en full_prompt y describe con precisión sus rasgos característicos: diseño de personajes, vestuario, paleta, técnica y época.

Con la ilustración original puedes conservar su composición, encuadre o pose si eso sirve al tema. Puedes también reinterpretarla por completo; decide lo que dé el mejor resultado. Si el tema pide el estilo de un artista concreto, incluido el artista original de la carta, aplícalo.

fallback_prompt describe la misma escena que full_prompt, con el mismo nivel de detalle, pero sin nombres propios de franquicias, personajes, marcas ni artistas. Reemplaza cada nombre por una descripción visual precisa de sus rasgos para conservar el resultado buscado. Se usa solo si el proveedor de imágenes rechaza el prompt original. Si full_prompt no contiene nombres propios, fallback_prompt puede ser idéntico."""


def normalize_theme(theme: str) -> str:
    return " ".join(theme.lower().split())


def make_cache_key(oracle_id, theme: str, provider: str, assignment=None, brief=None) -> str:
    value = f"{oracle_id}|{normalize_theme(theme)}|{PROMPT_VERSION}|{provider}"
    if assignment is not None:
        value += f"|{normalize_theme(assignment.element)}|{normalize_theme(assignment.visual_brief)}"
    if brief:
        value += "|brief:" + json.dumps(brief, sort_keys=True, ensure_ascii=False).lower()
    return hashlib.sha256(value.encode()).hexdigest()


def _clean_json(raw: str) -> dict:
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    result = json.loads(cleaned)
    missing = EXPECTED_FIELDS - result.keys() if isinstance(result, dict) else EXPECTED_FIELDS
    if missing:
        raise ValueError(f"Faltan campos JSON: {', '.join(sorted(missing))}")
    if not isinstance(result["fallback_prompt"], str) or not result["fallback_prompt"].strip():
        raise ValueError("fallback_prompt debe ser texto no vacío")
    frame = result["frame_style"]
    if not isinstance(frame, dict) or not {"border", "background", "text_box", "accent", "texture"} <= frame.keys():
        raise ValueError("frame_style incompleto")
    for key in ("border", "background", "text_box", "accent"):
        if not isinstance(frame[key], str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", frame[key]):
            raise ValueError(f"Color inválido: {key}")
    for key, limit in (("themed_name", 30), ("themed_flavor", 140)):
        if not isinstance(result[key], str):
            raise ValueError(f"{key} debe ser texto")
        result[key] = result[key][:limit]
    return result


def build_visual_prompt(card, theme: str, assignment=None, brief=None) -> dict:
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    card_data = {
        "mana_cost": card.mana_cost, "power": card.power, "toughness": card.toughness,
        "loyalty": card.loyalty, "keywords": card.keywords,
        "name": card.name, "type_line": card.type_line, "oracle_text": card.oracle_text,
        "flavor_text": card.flavor_text, "colors": card.colors,
        "color_identity": card.color_identity, "theme": theme,
    }
    if assignment is not None:
        card_data["assignment"] = {key: getattr(assignment, key) for key in ("element", "category", "visual_brief")}
    if brief:
        card_data["brief"] = brief
    text_block = {"type": "text", "text": json.dumps(card_data, ensure_ascii=False)}
    content = [text_block]
    if card.original_art_url:
        content.insert(0, {"type": "image", "source": {"type": "url", "url": card.original_art_url}})
    messages = [{"role": "user", "content": content}]
    image_retry_available = bool(card.original_art_url)
    raw_responses = []
    for attempt in range(2):
        kwargs = dict(model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
                      max_tokens=1500, system=SYSTEM_PROMPT)
        try:
            response = client.messages.create(messages=messages, **kwargs)
        except APIStatusError as exc:
            error = str(exc).lower()
            if not image_retry_available or not any(word in error for word in ("image", "download", "url")):
                raise
            image_retry_available = False
            logger.warning("Claude no pudo obtener la ilustración original; reintentando sin imagen")
            messages = [{**message, "content": [text_block]} if i == 0 else message
                        for i, message in enumerate(messages)]
            response = client.messages.create(messages=messages, **kwargs)
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
