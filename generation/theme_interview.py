import json
from collections import Counter

from .claude_client import call_claude_json

from .selection import card_category, type_words, unique_entries

SYSTEM_PROMPT = """Eres director de arte y haces una entrevista para personalizar un mazo real dentro del tema. Genera entre 0 y 5 preguntas de opción múltiple, nunca una plantilla genérica.
Si hay comandante, la primera pregunta define qué personaje del tema lo representa; si no hay comandante, pregunta por el protagonista del mazo. Las opciones deben encajar con su identidad de color y mecánica: negro-rojo de sacrificio sugiere personajes oscuros o agresivos, no héroes de luz.
Cubre facción/bando, época/juego/versión, estilo visual, representación de tierras o una categoría numerosa (por ejemplo equipos: reliquias legendarias o armamento de soldado), solo si el tema no lo define ya. No preguntes lo que ya responde el tema: Warcraft III — la Plaga ya define época y facción. Para una estética sin ambigüedad relevante como acuarela botánica puedes devolver 0 o 1 pregunta.
Las preguntas, help y detail van en español, pero los nombres propios de personajes, facciones y lugares dentro de label van con su nombre canónico en inglés (Arthas, the Lich King; The Forsaken).
Cada pregunta tiene 2 a 4 opciones concretas y distintas, label de máximo 80 caracteres y detail de máximo 140 explicando por qué encaja con el mazo. help opcional de máximo 160 caracteres. multiple es booleano: true solo si tiene sentido elegir varias (facciones mezcladas); por defecto false. allow_other es booleano, casi siempre true.
Devuelve solo JSON válido: {"questions": [{"question": "...", "help": "...", "multiple": false, "allow_other": true, "options": [{"label": "...", "detail": "..."}, {"label": "...", "detail": "..."}]}]}. No generes ids, el backend los asigna."""
CATEGORIES = ('creature', 'legendary_creature', 'planeswalker', 'equipment', 'vehicle',
              'artifact_other', 'enchantment', 'instant', 'sorcery', 'land', 'basic_land')


def _interview_input(deck):
    entries = unique_entries(deck)
    counts = Counter(card_category(row.card.type_line) for row in entries)
    commanders = [{key: getattr(row.card, key) for key in ('name', 'type_line', 'colors', 'mana_cost', 'oracle_text')}
                  for row in entries if row.section == 'commander']
    notable = [row.card for row in entries
               if row.section != 'commander' and card_category(row.card.type_line) not in ('land', 'basic_land')]
    notable.sort(key=lambda card: (not bool(type_words(card.type_line) & {'legendary', 'legendaria', 'legendario'}), -card.cmc))
    return {
        'theme': deck.theme, 'commanders': commanders,
        'color_identity': sorted({color for row in entries for color in row.card.color_identity}),
        'type_counts': {category: counts[category] for category in CATEGORIES},
        'notable_cards': [{**{key: getattr(card, key) for key in ('name', 'type_line', 'colors')},
                           'oracle_text': card.oracle_text[:200]} for card in notable[:12]],
    }


def _text(data, key, limit=None, required=False):
    value = data.get(key, '')
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f'{key} debe ser texto' + (' no vacío' if required else ''))
    return value.strip()[:limit] if limit else value.strip()


def _validate_questions(data):
    questions = data.get('questions') if isinstance(data, dict) else None
    if not isinstance(questions, list) or len(questions) > 5:
        raise ValueError('Se esperan de 0 a 5 preguntas')
    result = []
    for index, question in enumerate(questions, 1):
        if not isinstance(question, dict):
            raise ValueError('Pregunta inválida')
        options = question.get('options')
        if not isinstance(options, list) or not 2 <= len(options) <= 4:
            raise ValueError('Cada pregunta requiere de 2 a 4 opciones')
        multiple, allow_other = question.get('multiple', False), question.get('allow_other', True)
        if type(multiple) is not bool or type(allow_other) is not bool:
            raise ValueError('multiple y allow_other deben ser booleanos')
        clean_options = []
        for option_index, option in enumerate(options):
            if not isinstance(option, dict):
                raise ValueError('Opción inválida')
            clean_options.append({'id': 'abcd'[option_index], 'label': _text(option, 'label', 80, True),
                                  'detail': _text(option, 'detail', 140)})
        result.append({'id': f'q{index}', 'question': _text(question, 'question', required=True),
                       'help': _text(question, 'help', 160), 'multiple': multiple,
                       'allow_other': allow_other, 'options': clean_options})
    return {"questions": result}


def build_theme_questions(deck):
    messages = [{'role': 'user', 'content': json.dumps(_interview_input(deck), ensure_ascii=False)}]
    return call_claude_json(system=SYSTEM_PROMPT, messages=messages, max_tokens=3000,
                            validate=_validate_questions)['questions']


def validate_theme_answers(deck, answers):
    if not isinstance(answers, dict):
        raise ValueError('answers debe ser un objeto.')
    questions = {question['id']: question for question in deck.theme_questions}
    cleaned = {}
    for key, answer in answers.items():
        if key not in questions:
            raise ValueError(f'Pregunta desconocida: {key}.')
        if not isinstance(answer, dict):
            raise ValueError(f'Respuesta inválida para {key}.')
        question = questions[key]
        option_ids = answer.get('option_ids', [])
        other = answer.get('other', '')
        if not isinstance(option_ids, list) or any(not isinstance(value, str) for value in option_ids):
            raise ValueError(f'option_ids debe ser una lista de ids para {key}.')
        allowed_ids = {option['id'] for option in question['options']}
        if any(value not in allowed_ids for value in option_ids):
            raise ValueError(f'Opción desconocida para {key}.')
        if len(set(option_ids)) != len(option_ids):
            raise ValueError(f'Opciones duplicadas para {key}.')
        if not question.get('multiple', False) and len(option_ids) > 1:
            raise ValueError(f'{key} permite como máximo una opción.')
        if not isinstance(other, str) or len(other) > 200:
            raise ValueError(f'other debe ser texto de máximo 200 caracteres para {key}.')
        other = other.strip()
        if other and not question.get('allow_other', True):
            raise ValueError(f'{key} no permite otra respuesta.')
        if not option_ids and not other:
            raise ValueError(f'La respuesta de {key} está vacía.')
        cleaned[key] = {'option_ids': option_ids, 'other': other}
    return cleaned


def deck_brief(deck):
    brief = []
    for question in deck.theme_questions:
        answer = deck.theme_answers.get(question['id'])
        if not answer:
            continue
        labels = {option['id']: option['label'] for option in question['options']}
        parts = [labels[key] for key in answer.get('option_ids', []) if key in labels]
        other = answer.get('other', '').strip()
        if other:
            parts.append(f'Otra: {other}')
        if parts:
            brief.append({'question': question['question'], 'answer': ', '.join(parts)})
    return brief
