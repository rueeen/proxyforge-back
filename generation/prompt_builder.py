import hashlib
import logging
import json
import os
import re

from anthropic import APIStatusError

from .claude_client import call_claude_json, parse_json
from .card_text import card_text_instructions

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v3"
EXPECTED_FIELDS = {"subject", "action", "setting", "palette", "style_notes", "composition", "full_prompt", "fallback_prompt", "concept_source", "themed_name", "themed_flavor", "frame_style"}
SYSTEM_PROMPT = """Actúa como director de arte. Traduce los datos de una carta de Magic: The Gathering a una descripción visual original dentro del tema pedido. Conserva siempre su flavor mecánico: una remoción muestra destrucción, un buff potenciación y un contrahechizo interrupción.

Responde únicamente JSON válido, sin preámbulo ni bloques Markdown, con exactamente estas claves: subject, action, setting, palette (lista de tres colores hex), style_notes, composition, full_prompt (un solo párrafo listo para un generador de imágenes), fallback_prompt, concept_source, themed_name, themed_flavor y frame_style.

Límites de extensión: concept_source máximo 60 palabras; subject 25; action 40; setting 40; style_notes 40; composition 40; full_prompt 150; fallback_prompt 150. themed_name máximo 30 caracteres y themed_flavor máximo 140 caracteres. Escribe concept_source primero en el JSON y sé breve en él: es contexto interno, no el prompt.
full_prompt y fallback_prompt se escriben en inglés. Mantén los nombres propios de personajes, lugares y armas como en la franquicia. Los campos descriptivos internos pueden quedar en español.

Si recibes la ilustración original, describe primero en concept_source qué concepto representa: sujeto, acción, emoción y relación con la mecánica. Luego reinterpreta ese concepto dentro del tema.

Respeta el idioma configurado para themed_name y themed_flavor según las instrucciones finales de este mensaje.

frame_style es un objeto con border, background, text_box y accent (colores hex #RRGGBB), y texture (texto corto, como papel acuarela o metal cepillado). Deriva los colores del maná y del tema, con suficiente contraste para texto oscuro sobre text_box. Este objeto describe el marco que se compondrá aparte, no debe aparecer en la ilustración.

Nunca reescribas el coste de maná, tipo, texto de reglas, fuerza/resistencia ni lealtad. Esos datos no forman parte de la respuesta.

Deriva la paleta de sus colores de maná: blanco=luz/orden, azul=agua/conocimiento, negro=sombra/decadencia, rojo=fuego/caos, verde=naturaleza/crecimiento, incoloro=metal/vacío; para multicolor crea una mezcla coherente. La composición será horizontal, apaisada 16:9 y apta para recorte art_crop. La imagen contendrá solo ilustración: nunca texto, letras, números, logos, marcos de carta ni símbolos de maná.

Si reference_images es mayor que 0, el generador recibirá esas imágenes como referencia de identidad del personaje o elemento asignado. Escribe full_prompt en inglés comenzando con: «Use the character shown in the reference image(s) as the exact identity reference: keep their face, species, proportions, hairstyle, colors, outfit and signature gear. Create a NEW illustration (do not copy the reference composition or pose):» y continúa con la escena. fallback_prompt sigue la misma estructura, pero sin nombres propios. Respeta las reglas de composición del formato.

Cuando el sujeto sea un personaje, criatura o raza de una franquicia, describe en full_prompt y fallback_prompt los rasgos anatómicos que lo distinguen de especies parecidas con las que un generador de imágenes podría confundirlo, y nómbralos en contraste cuando exista riesgo real. Troll de Warcraft (no orco): alto y delgado, postura encorvada, orejas largas y puntiagudas, colmillos largos curvados hacia arriba, nariz larga, dos dedos grandes en los pies, cresta de pelo, piel azul o verde según la tribu. Orco de Warcraft (no troll): corpulento, mandíbula ancha, colmillos inferiores cortos, orejas pequeñas. Elfo de sangre (no elfo de la noche): piel clara, ojos verdes brillantes. Elfo de la noche: piel violeta o azul, ojos plateados sin pupila. Hazlo también para variantes del mismo personaje en distintas épocas, por ejemplo Arthas príncipe vs. Rey Exánime.

Si recibes brief, contiene decisiones obligatorias del usuario. En especial, el estilo visual elegido define la técnica, el acabado y el tono tanto de full_prompt como de fallback_prompt.

Si recibes assignment, representa exactamente ese elemento y nómbralo explícitamente en full_prompt, con sus rasgos canónicos: diseño, colores, vestuario, armas y escenario característicos de la franquicia, siguiendo visual_brief. Combina assignment con la mecánica de la carta y la ilustración original como referencia conceptual. fallback_prompt describe ese mismo elemento sin nombres propios, conservando sus rasgos visuales. themed_name debe partir del element asignado.

Sigue el tema del usuario de forma literal y con máxima fidelidad. Si nombra franquicias, personajes, obras, estudios o artistas, úsalos por su nombre en full_prompt y describe con precisión sus rasgos característicos: diseño de personajes, vestuario, paleta, técnica y época.

Con la ilustración original puedes conservar su composición, encuadre o pose si eso sirve al tema. Puedes también reinterpretarla por completo; decide lo que dé el mejor resultado. Si el tema pide el estilo de un artista concreto, incluido el artista original de la carta, aplícalo.

fallback_prompt describe la misma escena que full_prompt, con el mismo nivel de detalle, pero sin nombres propios de franquicias, personajes, marcas ni artistas. Reemplaza cada nombre por una descripción visual precisa de sus rasgos para conservar el resultado buscado. Se usa solo si el proveedor de imágenes rechaza el prompt original. Si full_prompt no contiene nombres propios, fallback_prompt puede ser idéntico."""


CLASSIC_COMPOSITION = "La composición será horizontal, apaisada 16:9 y apta para recorte art_crop."
BORDERLESS_COMPOSITION = "Composición VERTICAL 3:4, ilustración a sangre completa para una carta sin borde. El rostro o la cabeza del sujeto principal debe quedar entre el 20% y el 45% del alto de la imagen, medido desde arriba. NUNCA en el 15% superior. Deja espacio libre visible por encima de la cabeza (cielo, niebla, luz, fondo). Si el personaje lleva casco, corona, cuernos o arma alzada, también deben caber completos bajo ese 15% superior. Prefiere un plano medio o tres cuartos con aire superior antes que un primer plano que corte la cabeza. Deja el 10% superior con fondo poco cargado (cielo, niebla, luz) porque ahí va la placa del nombre. Deja el 40% inferior con menos detalle y menos contraste (suelo, humo, energía difusa, extensión del escenario) porque ahí va una caja de texto translúcida; puede haber elementos, pero no rostros ni puntos focales. El encuadre debe sentirse como un póster vertical dramático, no como una escena horizontal recortada. En formato sin borde, termina full_prompt y fallback_prompt con: «Vertical 3:4 composition, head positioned in the upper-middle of the frame with clear empty space above the head, nothing important in the top 15%, lower 40% calmer for text overlay.»"


def normalize_theme(theme: str) -> str:
    return " ".join(theme.lower().split())


def make_cache_key(oracle_id, theme: str, provider: str, assignment=None, brief=None, layout=None, reference_hashes=None) -> str:
    value = f"{oracle_id}|{normalize_theme(theme)}|{PROMPT_VERSION}|{provider}"
    if assignment is not None:
        value += f"|{normalize_theme(assignment.element)}|{normalize_theme(assignment.visual_brief)}"
    if brief:
        value += "|brief:" + json.dumps(brief, sort_keys=True, ensure_ascii=False).lower()
    if layout == "borderless":
        value += "|layout:borderless"
    if reference_hashes:
        value += "|refs:" + ",".join(sorted(reference_hashes))
    return hashlib.sha256(value.encode()).hexdigest()


def _clean_json(raw: str) -> dict:
    return _validate_prompt(parse_json(raw))


def _validate_prompt(result):
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


def build_visual_prompt(card, theme: str, assignment=None, brief=None, layout="classic", reference_count=0) -> dict:
    card_data = {
        "layout": layout,
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
    if reference_count > 0:
        card_data["reference_images"] = reference_count
    text_block = {"type": "text", "text": json.dumps(card_data, ensure_ascii=False)}
    content = [text_block]
    if card.original_art_url:
        content.insert(0, {"type": "image", "source": {"type": "url", "url": card.original_art_url}})
    messages = [{"role": "user", "content": content}]
    system = SYSTEM_PROMPT.replace(CLASSIC_COMPOSITION, BORDERLESS_COMPOSITION) if layout == "borderless" else SYSTEM_PROMPT
    kwargs = dict(system=system + "\n\n" + card_text_instructions(), max_tokens=int(os.getenv("ANTHROPIC_CARD_MAX_TOKENS", "4000")),
                  validate=_validate_prompt)
    try:
        return call_claude_json(messages=messages, **kwargs)
    except APIStatusError as exc:
        error = str(exc).lower()
        if not card.original_art_url or not any(word in error for word in ("image", "download", "url")):
            raise
        logger.warning("Claude no pudo obtener la ilustración original; reintentando sin imagen")
        return call_claude_json(messages=[{"role": "user", "content": [text_block]}], **kwargs)
