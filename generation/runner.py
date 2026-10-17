# Se usa threading en vez de Celery porque el entorno local no tiene privilegios de
# administrador para levantar Redis u otro broker; el progreso se persiste en SQLite.
import logging
import threading
from io import BytesIO
from uuid import uuid4

from PIL import Image

from django.core.files.base import ContentFile
from django.db import connection
from django.utils import timezone

from .errors import short_error
from .framing import apply_auto_framing
from .auto_references import enabled, fetch_auto_reference, reference_assignments
from .references import reference_hashes
from .models import GeneratedImage, GenerationJob
from .prompt_builder import PROMPT_VERSION, build_visual_prompt, make_cache_key
from .selection import unique_entries, assignment_map, aspect_for_layout
from .theme_planner import build_theme_plan
from .theme_interview import deck_brief
from .providers import get_fallback_provider, get_provider

logger = logging.getLogger(__name__)


def start_job(job: GenerationJob):
    thread = threading.Thread(target=run_job, args=(job.id,), daemon=True)
    thread.start()
    return thread


def _image_extension(image_bytes):
    with Image.open(BytesIO(image_bytes)) as image:
        extension = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}.get(image.format)
        if extension is None:
            raise ValueError("Formato de imagen no compatible")
        image.verify()
    return extension


def render_card_image(card, theme, provider, assignment=None, brief=None, layout="classic") -> tuple[dict, bytes]:
    """Llama a Claude y al proveedor; no toca la base de datos."""
    aspect_ratio = aspect_for_layout(layout)
    references = []
    if assignment is not None:
        for reference in assignment.references.all():
            with reference.image.open("rb") as image:
                references.append(image.read())
    prompt_json = build_visual_prompt(card, theme, assignment, brief, layout, reference_count=len(references))
    fallback_provider = get_fallback_provider()
    attempts = [(provider, "full"), (provider, "fallback")]
    if fallback_provider is not None:
        attempts.append((fallback_provider, "fallback"))
    failures = []
    last_error = None
    for attempt_provider, prompt_type in attempts:
        try:
            image_bytes = attempt_provider.generate(prompt_json[f"{prompt_type}_prompt"], aspect_ratio=aspect_ratio, references=references)
            _image_extension(image_bytes)
        except Exception as error:
            reason = f"{attempt_provider.name}/{prompt_type}: {error}"
            logger.warning("Falló la generación: %s", reason)
            failures.append(reason)
            last_error = error
        else:
            prompt_json["used_prompt"] = prompt_type
            prompt_json["used_provider"] = attempt_provider.name
            return prompt_json, image_bytes
    raise RuntimeError(short_error("; ".join(failures))) from last_error


def save_card_image(card, theme, provider, prompt_json, image_bytes, assignment=None, brief=None, layout="classic") -> GeneratedImage:
    """Detecta formato, guarda el archivo con nombre único y crea el registro."""
    extension = _image_extension(image_bytes)
    cache_key = make_cache_key(card.oracle_id, theme, provider.name, assignment, brief, layout, reference_hashes(assignment))
    prompt_key = "fallback_prompt" if prompt_json["used_prompt"] == "fallback" else "full_prompt"
    generated = GeneratedImage(
        cache_key=cache_key, card=card, theme=theme, visual_prompt=prompt_json[prompt_key],
        prompt_json=prompt_json, provider=prompt_json["used_provider"], prompt_version=PROMPT_VERSION,
    )
    generated.image.save(
        f"{cache_key[:16]}-{uuid4().hex}.{extension}", ContentFile(image_bytes), save=False
    )
    try:
        generated.save()
    except Exception:
        generated.image.delete(save=False)
        raise
    if layout == 'borderless' and enabled('AUTO_FRAMING'):
        try:
            apply_auto_framing(generated, layout, image_bytes)
        except Exception as exc:
            logger.warning("No se pudo aplicar el encuadre: %s", str(exc)[:200])
    return generated


def generate_card_image(card, theme, provider=None, assignment=None, brief=None, layout="classic"):
    provider = provider or get_provider()
    cache_key = make_cache_key(card.oracle_id, theme, provider.name, assignment, brief, layout, reference_hashes(assignment))
    existing = GeneratedImage.objects.filter(cache_key=cache_key).first()
    if existing:
        return existing
    prompt_json, image_bytes = render_card_image(card, theme, provider, assignment, brief, layout)
    return save_card_image(card, theme, provider, prompt_json, image_bytes, assignment, brief, layout)


def run_job(job_id):
    connection.close()
    try:
        job = GenerationJob.objects.select_related("deck").get(pk=job_id)
        job.status = "running"
        job.started_at = timezone.now()
        job.save(update_fields=["status", "started_at", "updated_at"])
        selected = set(job.card_oracle_ids)
        cards = [row.card for row in unique_entries(job.deck) if str(row.card.oracle_id) in selected]
        brief = deck_brief(job.deck)
        assignments = assignment_map(job.deck)
        if cards and not any(card.oracle_id in assignments for card in cards):
            build_theme_plan(job.deck, brief=brief, progress=lambda stage, completed, total: _job_progress(
                job, stage, job.completed_cards, job.total_cards))
            assignments = assignment_map(job.deck)
        provider = get_provider()
        for index, card in enumerate(cards, 1):
            _job_progress(job, _item_stage('Generando ', card.name, index, len(cards)), job.completed_cards, job.total_cards)
            try:
                assignment = assignments.get(card.oracle_id)
                if (enabled('AUTO_REFERENCES_IN_JOBS') and assignment is not None
                        and assignment.needs_reference and not assignment.auto_reference_status):
                    try:
                        fetch_auto_reference(assignment)
                    except Exception:
                        logger.warning("No se pudo buscar referencia para %s; generando sin ella", card.name, exc_info=True)
                generate_card_image(card, job.deck.theme, provider, assignment, brief, job.deck.card_layout)
                job.completed_cards += 1
            except Exception as exc:  # Continue so one provider failure does not abort the deck.
                logger.exception("No se pudo generar arte para %s", card.name)
                job.failed_cards += 1
                job.error_message = short_error(exc)
            job.save(update_fields=["completed_cards", "failed_cards", "error_message", "updated_at"])
        job.status = "failed" if cards and job.failed_cards == len(cards) else "done"
        job.finished_at = timezone.now()
        job.stage = "Imágenes listas" if job.status == "done" else "Generación fallida"
        job.save(update_fields=["status", "stage", "finished_at", "error_message", "updated_at"])
    except Exception as exc:
        logger.exception("El job %s falló antes de completar el procesamiento", job_id)
        GenerationJob.objects.filter(pk=job_id).update(
            status="failed", error_message=short_error(exc), finished_at=timezone.now(), updated_at=timezone.now()
        )
    finally:
        connection.close()


def start_plan_job(job):
    thread = threading.Thread(target=run_plan_job, args=(job.id,), daemon=True)
    thread.start()
    return thread


def start_references_job(job):
    thread = threading.Thread(target=run_references_job, args=(job.id,), daemon=True)
    thread.start()
    return thread


def _item_stage(prefix, name, index, total):
    suffix = f' ({index} de {total})'
    return prefix + name[:120 - len(prefix) - len(suffix)] + suffix


def _job_progress(job, stage, completed, total):
    job.stage = stage[:120]
    job.completed_cards, job.total_cards = completed, total
    job.save(update_fields=['stage', 'completed_cards', 'total_cards', 'updated_at'])


def _job_started(job):
    job.status = 'running'
    job.started_at = timezone.now()
    job.save(update_fields=['status', 'started_at', 'updated_at'])


def _job_failed(job_id, exc):
    logger.exception('El job %s falló', job_id)
    GenerationJob.objects.filter(pk=job_id).update(status='failed', stage='Error',
        error_message=short_error(exc), finished_at=timezone.now(), updated_at=timezone.now())


def run_plan_job(job_id):
    connection.close()
    try:
        job = GenerationJob.objects.select_related('deck').get(pk=job_id)
        _job_started(job)
        build_theme_plan(job.deck, overwrite_edited=job.options.get('overwrite_edited', False), brief=deck_brief(job.deck),
                         progress=lambda stage, completed, total: _job_progress(job, stage, completed, total))
        job.status, job.stage = 'done', 'Plan listo'
        job.completed_cards = job.total_cards
        job.finished_at = timezone.now()
        job.save(update_fields=['status', 'stage', 'completed_cards', 'finished_at', 'updated_at'])
    except Exception as exc:
        _job_failed(job_id, exc)
    finally:
        connection.close()


def run_references_job(job_id):
    connection.close()
    try:
        job = GenerationJob.objects.select_related('deck').get(pk=job_id)
        _job_started(job)
        assignments = reference_assignments(job.deck, job.options.get('deck_card_ids'))
        total = len(assignments)
        summary = dict(found=0, not_found=0, rejected=0, error=0, blocked=0, skipped=0)
        _job_progress(job, 'Buscando referencias', 0, total)
        for index, assignment in enumerate(assignments, 1):
            stage = _item_stage('Buscando referencias: ', assignment.element, index, total)
            _job_progress(job, stage, index - 1, total)
            result = fetch_auto_reference(assignment, force=job.options.get('force', False))
            summary[result['status']] += 1
            if result['status'] in ('error', 'blocked', 'rejected'):
                job.failed_cards += 1
            job.completed_cards = index
            job.save(update_fields=['completed_cards', 'failed_cards', 'updated_at'])
        job.options = {**job.options, 'summary': summary}
        job.status, job.stage = 'done', 'Referencias listas'
        job.finished_at = timezone.now()
        job.save(update_fields=['options', 'status', 'stage', 'finished_at', 'updated_at'])
    except Exception as exc:
        _job_failed(job_id, exc)
    finally:
        connection.close()
