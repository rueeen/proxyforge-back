import os

from django.db.models import Case, IntegerField, Value, When

from .models import GeneratedImage
from .prompt_builder import make_cache_key
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
    return {assignment.oracle_id: assignment for assignment in deck.theme_assignments.all()}


def pending_entries(deck):
    entries = unique_entries(deck)
    assignments = assignment_map(deck)
    keys = {row.card.oracle_id: make_cache_key(row.card.oracle_id, deck.theme, get_provider_name(),
                                             assignments.get(row.card.oracle_id)) for row in entries}
    cached = set(GeneratedImage.objects.filter(cache_key__in=keys.values()).values_list('cache_key', flat=True))
    return entries, [row for row in entries if keys[row.card.oracle_id] not in cached]
