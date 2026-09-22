# Se usa threading en vez de Celery porque el entorno local no tiene privilegios de
# administrador para levantar Redis u otro broker; el progreso se persiste en SQLite.
import logging
import threading

from django.core.files.base import ContentFile
from django.db import connection
from django.utils import timezone

from .models import GeneratedImage, GenerationJob
from .prompt_builder import PROMPT_VERSION, build_visual_prompt, make_cache_key
from .providers import get_provider

logger = logging.getLogger(__name__)


def start_job(job: GenerationJob):
    thread = threading.Thread(target=run_job, args=(job.id,), daemon=True)
    thread.start()
    return thread


def _unique_cards(deck):
    cards = []
    seen = set()
    for deck_card in deck.cards.select_related("card").filter(card__isnull=False).order_by("id"):
        if deck_card.card.oracle_id not in seen:
            seen.add(deck_card.card.oracle_id)
            cards.append(deck_card.card)
    return cards


def generate_card_image(card, theme, provider=None):
    provider = provider or get_provider()
    cache_key = make_cache_key(card.oracle_id, theme, provider.name)
    existing = GeneratedImage.objects.filter(cache_key=cache_key).first()
    if existing:
        return existing
    prompt_json = build_visual_prompt(card, theme)
    image_bytes = provider.generate(prompt_json["full_prompt"])
    generated = GeneratedImage(
        cache_key=cache_key, card=card, theme=theme, visual_prompt=prompt_json["full_prompt"],
        prompt_json=prompt_json, provider=provider.name, prompt_version=PROMPT_VERSION,
    )
    generated.image.save(f"{cache_key}.png", ContentFile(image_bytes), save=False)
    generated.save()
    return generated


def run_job(job_id):
    connection.close()
    try:
        job = GenerationJob.objects.select_related("deck").get(pk=job_id)
        job.status = "running"
        job.started_at = timezone.now()
        job.save(update_fields=["status", "started_at"])
        cards = _unique_cards(job.deck)
        provider = get_provider()
        for card in cards:
            try:
                generate_card_image(card, job.deck.theme, provider)
                job.completed_cards += 1
            except Exception as exc:  # Continue so one provider failure does not abort the deck.
                logger.exception("No se pudo generar arte para %s", card.name)
                job.failed_cards += 1
                job.error_message = str(exc)
            job.save(update_fields=["completed_cards", "failed_cards", "error_message"])
        job.status = "failed" if cards and job.failed_cards == len(cards) else "done"
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "finished_at", "error_message"])
    except Exception as exc:
        logger.exception("El job %s falló antes de completar el procesamiento", job_id)
        GenerationJob.objects.filter(pk=job_id).update(
            status="failed", error_message=str(exc), finished_at=timezone.now()
        )
    finally:
        connection.close()
