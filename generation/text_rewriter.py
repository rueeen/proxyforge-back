import json
from uuid import UUID

from django.db import transaction

from .card_text import card_text_instructions, themed_name_limit, shorten_themed_name
from .claude_client import call_claude_json
from .models import GeneratedImage
from .prompt_builder import card_cache_key
from .providers import get_provider_name
from .references import reference_hashes

SYSTEM_PROMPT = '''Reescribe únicamente el nombre temático y flavor de cada carta según los datos y la asignación del tema. No cambies los datos jugables. Responde solo JSON: {"cards": [{"oracle_id": "...", "themed_name": "...", "themed_flavor": "..."}]}.'''


def _validate_texts(data, requested, name_limits=None, only_long_names=False):
    if not isinstance(data.get('cards'), list):
        raise ValueError('Se esperaba una lista de cartas')
    result = []
    seen = set()
    for item in data['cards']:
        if not isinstance(item, dict):
            raise ValueError('Texto de carta inválido')
        try:
            oracle_id = str(UUID(str(item.get('oracle_id'))))
        except ValueError:
            continue
        if oracle_id not in requested:
            continue
        fields = ('themed_name',) if only_long_names else ('themed_name', 'themed_flavor')
        if oracle_id in seen or any(not isinstance(item.get(key), str) for key in fields):
            raise ValueError('Textos o ids de cartas inválidos')
        seen.add(oracle_id)
        result.append({'oracle_id': oracle_id,
                       'themed_name': shorten_themed_name(item['themed_name'], (name_limits or {}).get(oracle_id, 28)),
                       **({'themed_flavor': item['themed_flavor'][:140]} if not only_long_names else {})})
    return {'cards': result}


def rewrite_deck_texts(deck, context, only_long_names=False):
    targets = {}
    for entry in deck.cards.all():
        if not entry.card_id:
            continue
        card = entry.card
        assignment = context['assignments'].get(card.oracle_id)
        key = card_cache_key(card, deck.theme, get_provider_name(), assignment,
                             context['brief'], deck.card_layout, reference_hashes(assignment), original_art_mode=deck.original_art_mode)
        image = context['generated_images'].get(key)
        if image is None or image.pk in targets:
            continue
        limit = themed_name_limit(card)
        if only_long_names and len(image.prompt_json.get('themed_name', '')) <= limit:
            continue
        payload = {field: getattr(card, field) for field in ('name', 'type_line', 'oracle_text')}
        payload.update(oracle_id=str(card.oracle_id), max_name_chars=limit, assignment={field: getattr(assignment, field) for field in
                       ('element', 'category')} if assignment else None,
                       themed_name=image.prompt_json.get('themed_name', ''), themed_flavor=image.prompt_json.get('themed_flavor', ''))
        targets[image.pk] = (image, payload)
    pairs = list(targets.values())
    updates = []
    system = SYSTEM_PROMPT + '\n\n' + card_text_instructions()
    if only_long_names:
        system += '\nReescribe SOLO themed_name para que quepa en max_name_chars. No reescribas el flavor; omite themed_flavor de la respuesta.'
    for offset in range(0, len(pairs), 20):
        batch = pairs[offset:offset + 20]
        by_oracle = {payload['oracle_id']: image for image, payload in batch}
        data = call_claude_json(
            system=system, max_tokens=4000,
            messages=[{'role': 'user', 'content': json.dumps({'theme': deck.theme, 'cards': [payload for _, payload in batch]}, ensure_ascii=False)}],
            validate=lambda data: _validate_texts(data, by_oracle, {payload['oracle_id']: payload['max_name_chars'] for _, payload in batch}, only_long_names),
        )
        for item in data['cards']:
            image = by_oracle[item['oracle_id']]
            updates.append((image, {**image.prompt_json, 'themed_name': item['themed_name'],
                                   **({'themed_flavor': item['themed_flavor']} if not only_long_names else {})}))
    with transaction.atomic():
        for image, prompt_json in updates:
            image.prompt_json = prompt_json
        if updates:
            GeneratedImage.objects.bulk_update([image for image, _ in updates], ['prompt_json'])
    return {'updated': len(updates), 'skipped': len(pairs) - len(updates),
            **({'shortened': len(updates)} if only_long_names else {})}
