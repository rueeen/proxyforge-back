import base64
import json
import logging
import os
from uuid import uuid4

from django.core.files.base import ContentFile
from django.db import transaction

from .claude_client import call_claude_json
from .models import ReferenceImage, ThemeAssignment
from .references import normalize_reference
from .selection import assignment_map
from .wiki_images import WikiImageError, WikiImageNotFound, find_wiki_image

logger = logging.getLogger(__name__)
VERIFY_PROMPT = '''Verifica si la imagen sirve como referencia de identidad visual del elemento indicado. Sirve si muestra claramente a ese personaje u objeto (cuerpo completo, busto o primer plano), aunque sea arte conceptual, render, captura del juego o ilustración oficial. NO sirve si es un logo, un ícono diminuto, un mapa, una interfaz o pantalla de menú, un texto, una imagen grupal donde no se distingue al sujeto, o claramente otro personaje. Responde solo JSON: {"ok": true|false, "reason": "máx. 120 caracteres en español"}.'''


def enabled(name):
    return os.getenv(name, 'true').strip().lower() in ('true', '1', 'yes')


def _validate_verification(data):
    if type(data.get('ok')) is not bool or not isinstance(data.get('reason'), str):
        raise ValueError('Verificación inválida')
    return {'ok': data['ok'], 'reason': data['reason'][:120]}


def verify_reference(image_jpeg, assignment):
    try:
        data = call_claude_json(
            system=VERIFY_PROMPT, max_tokens=300,
            model=os.getenv('ANTHROPIC_VERIFY_MODEL') or os.getenv('ANTHROPIC_MODEL', 'claude-sonnet-4-6'),
            messages=[{'role': 'user', 'content': [
                {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg',
                                           'data': base64.b64encode(image_jpeg).decode()}},
                {'type': 'text', 'text': json.dumps({key: getattr(assignment, key) for key in
                                                    ('element', 'category', 'visual_brief')}, ensure_ascii=False)},
            ]}], validate=_validate_verification,
        )
        return data['ok'], data['reason']
    except Exception as exc:
        logger.warning('No se pudo verificar la referencia: %s', str(exc)[:200])
        return True, ('sin verificar: ' + str(exc))[:120]


def _skipped(assignment, force):
    return (not assignment.needs_reference or not assignment.reference_wiki or not assignment.reference_page
            or bool(list(assignment.references.all())) or (not force and bool(assignment.auto_reference_status)))


def _status(assignment, status, message=''):
    assignment.auto_reference_status = status
    assignment.auto_reference_message = message[:300]
    # Fetching a reference does not constitute rebuilding or editing the theme plan.
    assignment.save(update_fields=['auto_reference_status', 'auto_reference_message'])
    return status


def fetch_auto_reference(assignment, force=False):
    if _skipped(assignment, force):
        return assignment.auto_reference_status
    reference = None
    try:
        raw, url = find_wiki_image(assignment.reference_wiki, assignment.reference_page)
        image, digest = normalize_reference(raw)
        if assignment.references.filter(sha256=digest).exists():
            return _status(assignment, 'found')
        if enabled('AUTO_REFERENCE_VERIFY'):
            ok, reason = verify_reference(image, assignment)
            if not ok:
                return _status(assignment, 'rejected', reason)
        with transaction.atomic():
            current = ThemeAssignment.objects.select_for_update().get(pk=assignment.pk)
            if current.references.exists():
                return assignment.auto_reference_status
            reference = ReferenceImage(assignment=current, origin='auto', source_url=url, sha256=digest, position=0)
            reference.image.save(f'{uuid4().hex}.jpg', ContentFile(image), save=False)
            reference.save()
            _status(assignment, 'found')
        # The runner may have prefetched an empty relation before fetching this image.
        getattr(assignment, '_prefetched_objects_cache', {}).pop('references', None)
        return assignment.auto_reference_status
    except WikiImageNotFound:
        return _status(assignment, 'not_found', f'No hay imagen en {assignment.reference_wiki}/{assignment.reference_page}')
    except (WikiImageError, ValueError) as exc:
        return _status(assignment, 'error', str(exc))
    except Exception as exc:
        if reference is not None and reference.image:
            reference.image.delete(save=False)
        logger.warning('Error buscando referencia automática: %s', str(exc)[:200])
        return _status(assignment, 'error', str(exc))


def fetch_auto_references(deck, deck_card_ids=None, force=False):
    assignments = assignment_map(deck)
    if deck_card_ids is not None:
        oracle_ids = set(deck.cards.filter(id__in=deck_card_ids, card__isnull=False).values_list('card__oracle_id', flat=True))
        assignments = {key: item for key, item in assignments.items() if key in oracle_ids}
    summary = dict(found=0, not_found=0, rejected=0, error=0, skipped=0)
    for assignment in assignments.values():
        if _skipped(assignment, force):
            summary['skipped'] += 1
        else:
            status = fetch_auto_reference(assignment, force=force)
            summary[status if status in summary else 'skipped'] += 1
    return summary
