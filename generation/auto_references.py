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
from .wiki_images import WikiImageError, WikiImageNotFound, find_wiki_image, normalize_wiki_reference, LAST_RESULTS

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


MANUAL_LIMIT_MESSAGE = 'Ya hay 3 referencias manuales; quita una para agregar la de la wiki.'


def _result(status, message='', skipped_reason=''):
    return {'status': status, 'message': message, 'skipped_reason': skipped_reason}


def _skip_reason(assignment, references, force):
    if not force and not assignment.needs_reference:
        return 'no_reference_needed'
    if not assignment.reference_wiki or not assignment.reference_page:
        return 'missing_wiki_or_page'
    if not force and any(item.origin == 'auto' for item in references):
        return 'already_has_auto_reference'
    if not force and assignment.auto_reference_status:
        return 'already_attempted'
    return ''


def _status(assignment, status, message=''):
    assignment.auto_reference_status = status
    assignment.auto_reference_message = message[:300]
    assignment.save(update_fields=['auto_reference_status', 'auto_reference_message'])
    return _result(status, assignment.auto_reference_message)


def fetch_auto_reference(assignment, force=False):
    references = list(assignment.references.all())
    # Legacy rows may contain a complete URL in either field, including page-only URLs.
    has_url = any(value.lower().startswith(('http://', 'https://', '//')) for value in
                  (assignment.reference_wiki, assignment.reference_page))
    if (force or assignment.needs_reference) and ((assignment.reference_wiki and assignment.reference_page) or has_url):
        try:
            wiki, page = normalize_wiki_reference(assignment.reference_wiki, assignment.reference_page)
        except ValueError as exc:
            return _status(assignment, 'error', str(exc))
        if (wiki, page) != (assignment.reference_wiki, assignment.reference_page):
            assignment.reference_wiki, assignment.reference_page = wiki, page
            assignment.save(update_fields=['reference_wiki', 'reference_page'])
    reason = _skip_reason(assignment, references, force)
    if reason:
        messages = {
            'no_reference_needed': 'Esta asignación no necesita referencia.',
            'missing_wiki_or_page': 'Falta la wiki o la página de referencia.',
            'already_has_auto_reference': 'Ya hay una referencia automática; usa force para reemplazarla.',
            'already_attempted': 'La búsqueda ya se intentó; usa force para reintentar.',
        }
        return _result('skipped', messages[reason], reason)
    if force:
        assignment.references.filter(origin='auto').delete()
        getattr(assignment, '_prefetched_objects_cache', {}).pop('references', None)
        references = [item for item in references if item.origin != 'auto']
    if sum(item.origin == 'manual' for item in references) >= 3:
        return _status(assignment, 'error', MANUAL_LIMIT_MESSAGE)
    reference = None
    try:
        raw, url = find_wiki_image(assignment.reference_wiki, assignment.reference_page)
        image, digest = normalize_reference(raw)
        matched = LAST_RESULTS.get((assignment.reference_wiki, assignment.reference_page), {}).get('matched_title', '')
        message = f'Encontrada como "{matched}"' if matched else ''
        if assignment.references.filter(sha256=digest).exists():
            return _status(assignment, 'found', message)
        if enabled('AUTO_REFERENCE_VERIFY'):
            ok, reason = verify_reference(image, assignment)
            if not ok:
                return _status(assignment, 'rejected', reason)
        with transaction.atomic():
            current = ThemeAssignment.objects.select_for_update().get(pk=assignment.pk)
            existing = list(current.references.all())
            if any(item.origin == 'auto' for item in existing):
                return _result('skipped', 'Ya hay una referencia automática.', 'already_has_auto_reference')
            if len(existing) >= 3:
                return _status(assignment, 'error', MANUAL_LIMIT_MESSAGE)
            if any(item.sha256 == digest for item in existing):
                return _status(assignment, 'found', message)
            reference = ReferenceImage(assignment=current, origin='auto', source_url=url, sha256=digest,
                                       position=max((item.position for item in existing), default=-1) + 1)
            reference.image.save(f'{uuid4().hex}.jpg', ContentFile(image), save=False)
            reference.save()
            result = _status(assignment, 'found', message)
        getattr(assignment, '_prefetched_objects_cache', {}).pop('references', None)
        return result
    except WikiImageNotFound as exc:
        return _status(assignment, 'not_found', str(exc) or f'No hay imagen en {assignment.reference_wiki}/{assignment.reference_page}')
    except (WikiImageError, ValueError) as exc:
        if reference is not None and reference.image:
            reference.image.delete(save=False)
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
        result = fetch_auto_reference(assignment, force=force)
        summary[result['status']] += 1
    return summary
