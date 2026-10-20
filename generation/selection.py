import os

from django.db.models import Case, IntegerField, Value, When

from .image_cache import generated_image_map
from .references import reference_hashes
from .prompt_builder import card_cache_key
from .providers import get_provider_name


def batch_limit():
    return max(1, int(os.getenv('MAX_CARDS_PER_JOB', '5')))


def unique_entries(deck):
    rows = deck.cards.filter(card__isnull=False).select_related('card').annotate(
        section_order=Case(When(section='commander', then=Value(0)),
                           When(section='main', then=Value(1)), default=Value(2), output_field=IntegerField())
    ).order_by('section_order', 'id')
    unique = {}
    for row in rows:
        unique.setdefault(row.card.oracle_id, row)
    return list(unique.values())


def assignment_map(deck):
    return {assignment.oracle_id: assignment for assignment in deck.theme_assignments.prefetch_related("references").all()}


def pending_entries(deck):
    from .theme_interview import deck_brief

    brief = deck_brief(deck)
    entries = unique_entries(deck)
    assignments = assignment_map(deck)
    keys = {row.card.oracle_id: card_cache_key(row.card, deck.theme, get_provider_name(),
                                             assignments.get(row.card.oracle_id), brief, deck.card_layout, reference_hashes(assignments.get(row.card.oracle_id)), original_art_mode=deck.original_art_mode) for row in entries}
    cached = generated_image_map([row.card for row in entries], deck.theme, get_provider_name(), assignments, brief, deck.card_layout, deck.original_art_mode)
    return entries, [row for row in entries if keys[row.card.oracle_id] not in cached]


def type_words(type_line):
    import re
    import unicodedata

    plain = ''.join(char for char in unicodedata.normalize('NFD', type_line.casefold())
                    if not unicodedata.combining(char))
    # Subtypes do not change the card's main category.
    return set(re.findall(r'\w+', plain.split('—', 1)[0]))


def is_basic_land(type_line):
    words = type_words(type_line)
    return bool(words & {'land', 'tierra'}) and bool(words & {'basic', 'basica', 'basico'})


def card_category(type_line):
    words = type_words(type_line)
    if words & {'land', 'tierra'}:
        return 'basic_land' if is_basic_land(type_line) else 'land'
    if type_words(type_line.replace('—', ' ')) & {'equipment', 'equipo'}:
        return 'equipment'
    # Vehicle is a subtype, so include the complete line for this check.
    if type_words(type_line.replace('—', ' ')) & {'vehicle', 'vehiculo'}:
        return 'vehicle'
    if words & {'creature', 'criatura'}:
        return 'legendary_creature' if words & {'legendary', 'legendaria', 'legendario'} else 'creature'
    for category, names in (
        ('planeswalker', {'planeswalker'}), ('artifact_other', {'artifact', 'artefacto'}),
        ('enchantment', {'enchantment', 'encantamiento'}), ('instant', {'instant', 'instantaneo'}),
        ('sorcery', {'sorcery', 'conjuro'}),
    ):
        if words & names:
            return category
    return None


def aspect_for_layout(layout):
    if layout == "borderless":
        return "2:3"
    if layout == "classic":
        return "16:9"
    raise ValueError("Formato de carta no compatible")
