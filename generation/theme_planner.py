import json
from uuid import UUID

from django.db import transaction

from .claude_client import call_claude_json

from .models import ThemeAssignment
from .prompt_builder import normalize_theme
from .selection import unique_entries
from .theme_interview import deck_brief
from .wiki_images import valid_wiki

SYSTEM_PROMPT = """Planifica un mazo de Magic dentro del tema solicitado. Usa referencias canónicas y específicas: personajes, enemigos, jefes, armas, ítems, vehículos, lugares, escenarios y eventos reales de la franquicia. Prefiere variedad y profundidad, no solo protagonistas.
Asigna según función: comandante y criaturas legendarias a personajes principales o icónicos; criaturas según color, coste, fuerza/resistencia y habilidades (grandes y caras: jefes; baratas: enemigos comunes); equipos y armas a armas, mejoras o power-ups (armas de jefes o partes de armadura); vehículos a monturas o vehículos como Ride Armor; otros artefactos a ítems, tecnología y dispositivos como Sub Tank o Heart Tank; tierras a lugares y escenarios distintos para cada tipo de básica; instantáneos y conjuros a ataques, técnicas o momentos de historia; encantamientos a estados, poderes, tecnologías o fenómenos persistentes; planeswalkers a personajes clave con poder de mando.
Verifica la facción, tribu y raza canónicas de cada personaje antes de asignarlo (por ejemplo, Zul'jin es un troll Amani, no Gurubashi). Si dudas, prefiere una descripción que no afirme un dato incorrecto.
El elemento debe encajar con la mecánica: remoción es ataque destructivo, contrahechizo es interrupción, robo de cartas es inteligencia o información. No repitas element entre cartas ni elementos ya usados. Si faltan elementos, varía escena, momento o aspecto (Zero — Z-Saber, primer plano; Zero — dash aéreo).
Si el tema es una estética, asigna sujetos concretos y variados con la misma lógica. visual_brief debe describir en máximo 40 palabras y 1-3 frases qué se ve, qué hace y dónde: Storm Eagle en picada sobre la cubierta de la aeronave, soltando ráfagas que arrastran a un Reploid.
brief contiene decisiones obligatorias del usuario. Si define quién representa al comandante, asigna exactamente ese elemento a la carta de sección commander. Si define facción, época o versión, todas las asignaciones deben respetarlas; permite excepciones puntuales coherentes como enemigos de otra facción en remociones. Respeta la representación de tierras o de otras categorías elegida. El estilo visual no cambia element, pero debe reflejarse en visual_brief.
Devuelve reference solo cuando el parecido visual importe: personajes o jefes con nombre, armas, vehículos, objetos icónicos con diseño propio y lugares famosos reconocibles. No para enemigos genéricos, conceptos, hechizos o tierras genéricas; en 100 cartas suelen ser 10 a 25. Si el tema no es una franquicia, reference siempre es null. reference es null o {"wiki": "warcraft.wiki.gg", "page": "Vol'jin"}. wiki es el dominio en minúsculas de la wiki comunitaria más completa y actual, sin https:// ni rutas (warcraft.wiki.gg, megaman.fandom.com, finalfantasy.fandom.com, starwars.fandom.com). page es el título exacto tal como aparece en su URL, pero con espacios (Vol'jin, Sylvanas Windrunner, Frostmourne), máximo 300 caracteres.
Responde solo JSON válido: {"assignments": [{"oracle_id": "...", "element": "...", "category": "...", "visual_brief": "...", "reference": null}]}. element máximo 200 caracteres, category máximo 60."""


def build_theme_plan(deck, overwrite_edited=False, brief=None):
    if brief is None:
        brief = deck_brief(deck)
    entries = unique_entries(deck)
    ids = {row.card.oracle_id for row in entries}
    existing = {item.oracle_id: item for item in deck.theme_assignments.all()}
    preserved = {key: item for key, item in existing.items() if key in ids and item.edited and not overwrite_edited}
    pending = [row for row in entries if row.card.oracle_id not in preserved]
    used = {normalize_theme(item.element) for item in preserved.values()}
    used_elements = [item.element for item in preserved.values()]
    results = {}
    for _ in range(2):
        if not pending:
            break
        for offset in range(0, len(pending), 30):
            batch = pending[offset:offset + 30]
            cards = [{**{key: getattr(row.card, key) for key in (
                'name', 'type_line', 'mana_cost', 'colors', 'oracle_text', 'power', 'toughness', 'keywords')},
                      'oracle_id': str(row.card.oracle_id), 'section': row.section} for row in batch]
            requested = {row.card.oracle_id for row in batch}
            data = call_claude_json(
                system=SYSTEM_PROMPT, max_tokens=8000, retry_max_tokens=16000,
                messages=[{'role': 'user', 'content': json.dumps(
                    {'theme': deck.theme, 'cards': cards, 'used_elements': used_elements, 'brief': brief}, ensure_ascii=False)}],
                validate=lambda data: _validate_plan(data, requested),
            )
            for item in data['assignments']:
                key = item['oracle_id']
                element = normalize_theme(item['element'])
                if element in used:
                    continue
                results[key] = {field: item[field].strip() for field in ('element', 'category', 'visual_brief')}
                reference = item.get('reference')
                valid = (isinstance(reference, dict) and valid_wiki(reference.get('wiki'))
                         and isinstance(reference.get('page'), str) and bool(reference['page'].strip())
                         and len(reference['page']) <= 300)
                results[key].update(needs_reference=bool(valid), reference_wiki=reference['wiki'] if valid else '',
                                    reference_page=reference['page'].strip() if valid else '')
                used.add(element)
                used_elements.append(item['element'])
        pending = [row for row in pending if row.card.oracle_id not in results]
    if pending:
        raise ValueError('Faltan asignaciones para: ' + ', '.join(row.card.name for row in pending))
    with transaction.atomic():
        for key, values in results.items():
            changed = key in existing and existing[key].element != values['element']
            defaults = {**values, 'edited': False}
            if changed:
                defaults.update(auto_reference_status='', auto_reference_message='')
            assignment, _ = ThemeAssignment.objects.update_or_create(deck=deck, oracle_id=key, defaults=defaults)
            if changed:
                assignment.references.filter(origin='auto').delete()
        deck.theme_assignments.exclude(oracle_id__in=ids).delete()
    return list(deck.theme_assignments.all())


def _validate_plan(data, requested):
    if not isinstance(data.get('assignments'), list):
        raise ValueError('Plan temático inválido')
    result = []
    seen = set()
    for item in data['assignments']:
        try:
            key = UUID(str(item.get('oracle_id')))
        except (ValueError, AttributeError):
            continue
        if key not in requested:
            continue
        if key in seen:
            raise ValueError('oracle_id duplicado en el plan temático')
        seen.add(key)
        if any(not isinstance(item.get(field), str) or (field != 'category' and not item[field].strip())
               for field in ('element', 'category', 'visual_brief')):
            raise ValueError('Asignación temática inválida')
        if len(item['element']) > 200 or len(item['category']) > 60:
            raise ValueError('Asignación temática demasiado larga')
        result.append({**item, 'oracle_id': key})
    return {'assignments': result}
