from .models import GeneratedImage
from .prompt_builder import card_cache_key
from .references import reference_hashes


def generated_image_map(cards, theme, provider, assignments, brief=None, layout=None, original_art_mode='auto'):
    """Resolve requested keys, including an explicitly recorded download failure fallback."""
    requested = {}
    for card in cards:
        assignment = assignments.get(card.oracle_id)
        hashes = reference_hashes(assignment)
        primary = card_cache_key(card, theme, provider, assignment, brief, layout, hashes, original_art_mode)
        legacy = card_cache_key(card, theme, provider, assignment, brief, layout, hashes, 'off')
        requested[primary] = (legacy, str(card.scryfall_id))
    keys = set(requested) | {legacy for legacy, _ in requested.values()}
    images = {image.cache_key: image for image in GeneratedImage.objects.filter(cache_key__in=keys).select_related('card')}
    result = {}
    for key, (legacy, scryfall_id) in requested.items():
        if key in images:
            result[key] = images[key]
        elif legacy in images:
            image = images[legacy]
            if (image.prompt_json.get('original_art_requested_id') == scryfall_id
                    and image.prompt_json.get('original_art_reference') is False):
                result[key] = image
    return result
