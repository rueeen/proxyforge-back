import hashlib
import logging
import json
import os
import re

from anthropic import APIStatusError

from .claude_client import call_claude_json, parse_json
from .card_text import card_text_instructions, themed_name_limit, shorten_themed_name
from .original_art import use_original_art
from .references import reference_hashes as assignment_reference_hashes

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v3"
EXPECTED_FIELDS = {"subject", "action", "setting", "palette", "style_notes", "composition", "full_prompt", "fallback_prompt", "concept_source", "themed_name", "themed_flavor", "frame_style", "source_details"}
SYSTEM_PROMPT = """Actúa como director de arte. Traduce los datos de una carta de Magic: The Gathering a una descripción visual original dentro del tema pedido. Conserva siempre su flavor mecánico: una remoción muestra destrucción, un buff potenciación y un contrahechizo interrupción.

Responde únicamente JSON válido, sin preámbulo ni bloques Markdown, con exactamente estas claves: subject, action, setting, palette (lista de tres colores hex), style_notes, composition, full_prompt (un solo párrafo listo para un generador de imágenes), fallback_prompt, concept_source, themed_name, themed_flavor, frame_style y source_details.

Límites de extensión: concept_source máximo 60 palabras; subject 25; action 40; setting 40; style_notes 40; composition 40; full_prompt 150; fallback_prompt 150. themed_name máximo max_name_chars caracteres y themed_flavor máximo 140 caracteres. Escribe concept_source primero en el JSON y sé breve en él: es contexto interno, no el prompt.
full_prompt y fallback_prompt se escriben en inglés. Mantén los nombres propios de personajes, lugares y armas como en la franquicia. Los campos descriptivos internos pueden quedar en español.

Si recibes la ilustración original, describe primero en concept_source qué concepto representa: sujeto, acción, emoción y relación con la mecánica. Luego reinterpreta ese concepto dentro del tema.

Respeta el idioma configurado para themed_name y themed_flavor según las instrucciones finales de este mensaje.

frame_style es un objeto con border, background, text_box y accent (colores hex #RRGGBB), y texture (texto corto, como papel acuarela o metal cepillado). Deriva los colores del maná y del tema, con suficiente contraste para texto oscuro sobre text_box. Este objeto describe el marco que se compondrá aparte, no debe aparecer en la ilustración.

Nunca reescribas el coste de maná, tipo, texto de reglas, fuerza/resistencia ni lealtad. Esos datos no forman parte de la respuesta.

Deriva la paleta de sus colores de maná: blanco=luz/orden, azul=agua/conocimiento, negro=sombra/decadencia, rojo=fuego/caos, verde=naturaleza/crecimiento, incoloro=metal/vacío; para multicolor crea una mezcla coherente. La composición será horizontal, apaisada 16:9 y apta para recorte art_crop. La imagen contendrá solo ilustración: nunca texto, letras, números, logos, marcos de carta ni símbolos de maná.

Si original_art_reference no es true y reference_images es mayor que 0, el generador recibirá esas imágenes como referencia de identidad del personaje o elemento asignado. Escribe full_prompt en inglés comenzando con: «Use the character shown in the reference image(s) as the exact identity reference: keep their face, species, proportions, hairstyle, colors, outfit and signature gear. Create a NEW illustration (do not copy the reference composition or pose):» y continúa con la escena. fallback_prompt sigue la misma estructura, pero sin nombres propios. Respeta las reglas de composición del formato.

Cuando el sujeto sea un personaje, criatura o raza de una franquicia, describe en full_prompt y fallback_prompt los rasgos anatómicos que lo distinguen de especies parecidas con las que un generador de imágenes podría confundirlo, y nómbralos en contraste cuando exista riesgo real. Troll de Warcraft (no orco): alto y delgado, postura encorvada, orejas largas y puntiagudas, colmillos largos curvados hacia arriba, nariz larga, dos dedos grandes en los pies, cresta de pelo, piel azul o verde según la tribu. Orco de Warcraft (no troll): corpulento, mandíbula ancha, colmillos inferiores cortos, orejas pequeñas. Elfo de sangre (no elfo de la noche): piel clara, ojos verdes brillantes. Elfo de la noche: piel violeta o azul, ojos plateados sin pupila. Hazlo también para variantes del mismo personaje en distintas épocas, por ejemplo Arthas príncipe vs. Rey Exánime.

Si recibes brief, contiene decisiones obligatorias del usuario. En especial, el estilo visual elegido define la técnica, el acabado y el tono tanto de full_prompt como de fallback_prompt.

Si recibes assignment, representa exactamente ese elemento y nómbralo explícitamente en full_prompt, con sus rasgos canónicos: diseño, colores, vestuario, armas y escenario característicos de la franquicia, siguiendo visual_brief. Combina assignment con la mecánica de la carta y la ilustración original como referencia conceptual. fallback_prompt describe ese mismo elemento sin nombres propios, conservando sus rasgos visuales. themed_name debe partir del element asignado.

Sigue el tema del usuario de forma literal y con máxima fidelidad. Si nombra franquicias, personajes, obras, estudios o artistas, úsalos por su nombre en full_prompt y describe con precisión sus rasgos característicos: diseño de personajes, vestuario, paleta, técnica y época.

En temas libres conserva la composición general y todos los source_details de la ilustración original, reinterpretados dentro del tema. En franquicias, combina los detalles del original con la identidad canónica del elemento asignado. Si el tema pide el estilo de un artista concreto, incluido el artista original de la carta, aplícalo.

fallback_prompt describe la misma escena que full_prompt, con el mismo nivel de detalle, pero sin nombres propios de franquicias, personajes, marcas ni artistas. Reemplaza cada nombre por una descripción visual precisa de sus rasgos para conservar el resultado buscado. Se usa solo si el proveedor de imágenes rechaza el prompt original. Si full_prompt no contiene nombres propios, fallback_prompt puede ser idéntico."""


SOURCE_DETAILS_INSTRUCTIONS = """source_details lista de 3 a 6 elementos visuales concretos de la ilustración original, en inglés y de 12 palabras o menos cada uno: criaturas secundarias, objetos, decoraciones, ambientación y motivos que deben sobrevivir a la reinterpretación. Si no puedes identificar detalles, usa []. En temas libres (estéticas o géneros, no una franquicia con personajes concretos), full_prompt debe conservar el concepto, la composición general y TODOS los source_details, reinterpretados en el estilo del tema. Por ejemplo, una reina de una colmena con huevos en una caverna debe conservar la colmena, los huevos y las crías en su versión anime, no solo un personaje aislado. Sin arte original de referencia, incluye igualmente los source_details como elementos a mostrar.
Si original_art_reference es true, full_prompt debe empezar indicando el rol de cada imagen. Con N referencias de personaje: "Reference images 1–N: identity of the character (keep face, species, outfit, signature gear). Last reference image: the ORIGINAL card artwork — keep its composition, key elements and decorative details, but fully restyle it." Sustituye N por reference_images. Si solo está el arte original: "Use the reference image as the ORIGINAL card artwork: keep its subject, composition, secondary creatures, props, setting and decorative details listed below, but completely restyle it as <theme>. Do not copy its painting style or line work." Sustituye <theme> por el tema. Luego describe la escena y enumera explícitamente todos los source_details. fallback_prompt sigue las mismas reglas y orden, sin nombres propios."""

SYSTEM_PROMPT += "\n\n" + SOURCE_DETAILS_INSTRUCTIONS

CLASSIC_COMPOSITION = "La composición será horizontal, apaisada 16:9 y apta para recorte art_crop."
BORDERLESS_COMPOSITION = """Composición VERTICAL 2:3 (más alta que ancha), ilustración a sangre completa para una carta sin borde. El rostro o la cabeza del sujeto principal entre el 18% y el 40% del alto, nunca en el 12% superior; deja aire visible sobre la cabeza. El sujeto principal debe quedar COMPLETO (incluida la pose y lo que sostiene) dentro del 62% superior de la imagen. El 38% inferior es una extensión del escenario (suelo, huevos, niebla, energía, reflejos) con menos contraste, porque ahí va una caja de texto translúcida: puede tener elementos de ambientación, pero no rostros ni la acción principal. Prefiere plano medio o tres cuartos, no primeros planos que corten la cabeza ni planos en los que las piernas o el cuerpo queden bajo la caja de texto. En formato sin borde, termina full_prompt y fallback_prompt con: «Vertical 2:3 composition. Head in the upper third with clear space above it; the whole main subject fits in the top 62% of the frame; the bottom 38% is calmer scenery for a text overlay.»"""


def normalize_theme(theme: str) -> str:
    return " ".join(theme.lower().split())


def make_cache_key(oracle_id, theme: str, provider: str, assignment=None, brief=None, layout=None, reference_hashes=None, original_art_id=None) -> str:
    value = f"{oracle_id}|{normalize_theme(theme)}|{PROMPT_VERSION}|{provider}"
    if assignment is not None:
        value += f"|{normalize_theme(assignment.element)}|{normalize_theme(assignment.visual_brief)}"
    if brief:
        value += "|brief:" + json.dumps(brief, sort_keys=True, ensure_ascii=False).lower()
    if layout == "borderless":
        value += "|layout:borderless"
    if reference_hashes:
        value += "|refs:" + ",".join(sorted(reference_hashes))
    if original_art_id:
        value += f"|origart:{original_art_id}"
    return hashlib.sha256(value.encode()).hexdigest()


def card_cache_key(card, theme, provider, assignment=None, brief=None, layout=None, character_refs=None, original_art_mode='auto'):
    if character_refs is None:
        character_refs = assignment_reference_hashes(assignment)
    return make_cache_key(card.oracle_id, theme, provider, assignment, brief, layout, character_refs,
                          original_art_id=card.scryfall_id if use_original_art(card, original_art_mode, character_refs) else None)


def _clean_json(raw: str, name_limit=30) -> dict:
    return _validate_prompt(parse_json(raw), name_limit)


def _validate_prompt(result, name_limit=30):
    missing = (EXPECTED_FIELDS - {"source_details"}) - result.keys() if isinstance(result, dict) else EXPECTED_FIELDS
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
    for key, limit in (("themed_name", name_limit), ("themed_flavor", 140)):
        if not isinstance(result[key], str):
            raise ValueError(f"{key} debe ser texto")
        result[key] = shorten_themed_name(result[key], limit) if key == "themed_name" else result[key][:limit]
    details = result.get('source_details', [])
    if not isinstance(details, list) or any(not isinstance(item, str) for item in details):
        raise ValueError('source_details debe ser una lista de textos')
    result['source_details'] = [item[:100] for item in details[:6]]
    return result


def build_visual_prompt(card, theme: str, assignment=None, brief=None, layout="classic", reference_count=0, original_art_reference=False) -> dict:
    card_data = {
        "layout": layout, "max_name_chars": themed_name_limit(card),
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
    if original_art_reference:
        card_data["original_art_reference"] = True
    text_block = {"type": "text", "text": json.dumps(card_data, ensure_ascii=False)}
    content = [text_block]
    if card.original_art_url:
        content.insert(0, {"type": "image", "source": {"type": "url", "url": card.original_art_url}})
    messages = [{"role": "user", "content": content}]
    system = SYSTEM_PROMPT.replace(CLASSIC_COMPOSITION, BORDERLESS_COMPOSITION) if layout == "borderless" else SYSTEM_PROMPT
    kwargs = dict(system=system + "\n\n" + card_text_instructions(), max_tokens=int(os.getenv("ANTHROPIC_CARD_MAX_TOKENS", "4000")),
                  validate=lambda result: _validate_prompt(result, themed_name_limit(card)))
    try:
        return call_claude_json(messages=messages, **kwargs)
    except APIStatusError as exc:
        error = str(exc).lower()
        if not card.original_art_url or not any(word in error for word in ("image", "download", "url")):
            raise
        logger.warning("Claude no pudo obtener la ilustración original; reintentando sin imagen")
        return call_claude_json(messages=[{"role": "user", "content": [text_block]}], **kwargs)
