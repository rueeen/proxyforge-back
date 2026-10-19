import logging
from pathlib import Path
from uuid import uuid4

import requests
from django.conf import settings

from scryfall.client import HEADERS
from .references import normalize_reference

logger = logging.getLogger(__name__)


def use_original_art(card, mode='auto', character_refs=None):
    return bool(card.original_art_url) and (mode == 'always' or (mode == 'auto' and not character_refs))


def get_original_art(card):
    temporary = None
    try:
        if not card.original_art_url:
            return None
        path = Path(settings.MEDIA_ROOT) / 'cache' / 'original_art' / f'{card.scryfall_id}.jpg'
        if path.exists():
            return path.read_bytes()
        response = requests.get(card.original_art_url, timeout=20, headers=HEADERS)
        response.raise_for_status()
        normalized, _ = normalize_reference(response.content)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f'{path.stem}-{uuid4().hex}.tmp')
        temporary.write_bytes(normalized)
        temporary.replace(path)
        return normalized
    except Exception as exc:
        logger.warning('No se pudo obtener el arte original de %s: %s', card.name, str(exc)[:200])
        return None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
