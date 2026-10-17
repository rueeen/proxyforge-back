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
from .wiki_images import WikiDownloadBlocked, WikiImageError, WikiImageNotFound, list_wiki_images, _download, _is_fandom, normalize_wiki_reference

logger = logging.getLogger(__name__)
VERIFY_PROMPT = '''Verifica si la imagen sirve como referencia de identidad visual del elemento indicado. Sirve si muestra claramente a ese personaje u objeto (cuerpo completo, busto o primer plano), aunque sea arte conceptual, render, captura del juego o ilustración oficial. NO sirve si es un logo, un ícono diminuto, un mapa, una interfaz o pantalla de menú, un texto, una imagen grupal donde no se distingue al sujeto, o claramente otro personaje. Si la imagen es claramente un fan art con un diseño no canónico del personaje (por ejemplo, otra armadura, otros colores característicos o le falta su pieza distintiva, como el yelmo de un personaje que siempre lo usa), responde ok=false indicando por qué. Responde solo JSON: {"ok": true|false, "reason": "máx. 120 caracteres en español"}.'''


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


class VerificationResults(list):
    def __init__(self, values, best_order):
        super().__init__(values)
        self.best_order = best_order


def verify_references(images, assignment):
    def validate(data):
        results, order = data.get('results'), data.get('best_order')
        expected = set(range(1, len(images) + 1))
        if not isinstance(results, list) or not isinstance(order, list):
            raise ValueError('Verificación inválida')
        if any(type(index) is not int for index in order) or len(order) != len(images) or set(order) != expected:
            raise ValueError('Orden de referencias inválido')
        indexed = {}
        for item in results:
            if not isinstance(item, dict) or type(item.get('index')) is not int or item['index'] not in expected or item['index'] in indexed:
                raise ValueError('Índice de referencia inválido')
            indexed[item['index']] = _validate_verification(item)
        if set(indexed) != expected:
            raise ValueError('Faltan resultados de verificación')
        return VerificationResults([(indexed[index]['ok'], indexed[index]['reason']) for index in range(1, len(images) + 1)],
                                   [index - 1 for index in order])

    content = []
    for index, image in enumerate(images, 1):
        content.extend([{'type': 'text', 'text': f'Imagen {index}'},
                        {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg',
                                                    'data': base64.b64encode(image).decode()}}])
    content.append({'type': 'text', 'text': json.dumps({key: getattr(assignment, key) for key in
                                                       ('element', 'category', 'visual_brief')}, ensure_ascii=False)})
    try:
        return call_claude_json(system=VERIFY_PROMPT.split('Responde solo JSON:')[0] + ' Evalúa cada imagen numerada. Responde solo JSON: {"results": [{"index": 1, "ok": true, "reason": "..."}], "best_order": [índices ordenados de mejor a peor referencia]}. Incluye todos los índices una sola vez. Prefiere como mejores las que muestren claramente el rostro y el diseño canónico completo; evita repetir casi la misma imagen.',
            messages=[{'role': 'user', 'content': content}], max_tokens=1000, validate=validate,
            model=os.getenv('ANTHROPIC_VERIFY_MODEL') or os.getenv('ANTHROPIC_MODEL', 'claude-sonnet-4-6'))
    except Exception as exc:
        logger.warning('No se pudieron verificar las referencias: %s', str(exc)[:200])
        return VerificationResults([(True, ('sin verificar: ' + str(exc))[:120]) for _ in images], list(range(len(images))))


def _reference_candidates(assignment, wiki, target, existing):
    listing = list_wiki_images(wiki, assignment.reference_page)
    images = listing['images']
    preferred = lambda image: not image['maybe_fan_art'] and max(image['width'], image['height']) >= 600
    ordered = [image for image in images if preferred(image)] + [image for image in images if not preferred(image)]
    candidates, hashes, last_error = [], {item.sha256 for item in existing}, None
    for image in ordered[:target + 3]:
        try:
            raw, source = _download(image['full_url'] or image['thumb_url'], wiki, image['thumb_url'])
            normalized, digest = normalize_reference(raw)
        except WikiDownloadBlocked as exc:
            if _is_fandom(wiki, image['full_url']):
                raise
            last_error = exc
            continue
        except (WikiImageError, WikiImageNotFound, ValueError) as exc:
            last_error = exc
            continue
        if digest not in hashes:
            hashes.add(digest)
            candidates.append((normalized, digest, source, image))
    if not candidates:
        if last_error:
            raise last_error
        if existing and images:
            return [], False
        raise WikiImageNotFound('La página no tiene imágenes de referencia utilizables.')
    avoided = any(image['is_page_image'] and image['maybe_fan_art'] for image in images)
    return candidates, avoided


def fetch_auto_reference(assignment, force=False):
    references = list(assignment.references.all())
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
    target = min(assignment.deck.auto_reference_count, 3 - sum(item.origin != 'auto' for item in references))
    if target <= 0:
        return _result('skipped', MANUAL_LIMIT_MESSAGE, 'references_full')
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
    saved = []
    try:
        try:
            candidates, avoided = _reference_candidates(assignment, assignment.reference_wiki, target, references)
        except (WikiImageNotFound, WikiDownloadBlocked):
            alternate = assignment.reference_wiki_alt or assignment.deck.reference_wiki_alt
            if not alternate or alternate == assignment.reference_wiki:
                raise
            candidates, avoided = _reference_candidates(assignment, alternate, target, references)
        if not candidates:
            return _status(assignment, 'found', 'Se agregaron 0 referencias (ya estaban agregadas).')
        verdicts = verify_references([item[0] for item in candidates], assignment) if enabled('AUTO_REFERENCE_VERIFY') else [(True, '')] * len(candidates)
        order = getattr(verdicts, 'best_order', list(range(len(candidates))))
        accepted = [index for index in order if verdicts[index][0]][:target]
        if not accepted:
            return _status(assignment, 'rejected', verdicts[0][1])
        with transaction.atomic():
            current = ThemeAssignment.objects.select_for_update().get(pk=assignment.pk)
            existing = list(current.references.all())
            if any(item.origin == 'auto' for item in existing):
                return _result('skipped', 'Ya hay una referencia automática.', 'already_has_auto_reference')
            hashes = {item.sha256 for item in existing}
            position = max((item.position for item in existing), default=-1) + 1
            for index in accepted[:max(0, 3 - len(existing))]:
                image, digest, url, _ = candidates[index]
                if digest in hashes:
                    continue
                reference = ReferenceImage(assignment=current, origin='auto', source_url=url, sha256=digest, position=position)
                reference.image.save(f'{uuid4().hex}.jpg', ContentFile(image), save=False)
                saved.append(reference)
                reference.save()
                hashes.add(digest)
                position += 1
            message = f'Se agregaron {len(saved)} referencias'
            if avoided and any(not candidates[index][3]['maybe_fan_art'] for index in accepted) and not any(
                    candidates[index][3].get('is_page_image') and candidates[index][3]['maybe_fan_art'] for index in accepted):
                message += '; se evitó la principal (posible fan art)'
            result = _status(assignment, 'found', message)
        getattr(assignment, '_prefetched_objects_cache', {}).pop('references', None)
        return result
    except WikiImageNotFound as exc:
        return _status(assignment, 'not_found', str(exc) or 'No se encontró una imagen de referencia.')
    except WikiDownloadBlocked as exc:
        return _status(assignment, 'blocked', str(exc))
    except Exception as exc:
        for reference in saved:
            if reference.image:
                reference.image.delete(save=False)
        logger.warning('Error buscando referencias automáticas: %s', str(exc)[:200])
        return _status(assignment, 'error', str(exc))


def fetch_auto_references(deck, deck_card_ids=None, force=False):
    assignments = assignment_map(deck)
    if deck_card_ids is not None:
        oracle_ids = set(deck.cards.filter(id__in=deck_card_ids, card__isnull=False).values_list('card__oracle_id', flat=True))
        assignments = {key: item for key, item in assignments.items() if key in oracle_ids}
    summary = dict(found=0, not_found=0, rejected=0, error=0, blocked=0, skipped=0)
    for assignment in assignments.values():
        result = fetch_auto_reference(assignment, force=force)
        summary[result['status']] += 1
    return summary
