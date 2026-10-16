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

from .models import GeneratedImage, GenerationJob
from .prompt_builder import PROMPT_VERSION, build_visual_prompt, make_cache_key
from .providers import get_fallback_provider, get_provider

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


def _image_extension(image_bytes):
    with Image.open(BytesIO(image_bytes)) as image:
        extension = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}.get(image.format)
        if extension is None:
            raise ValueError("Formato de imagen no compatible")
        image.verify()
    return extension


def render_card_image(card, theme, provider) -> tuple[dict, bytes]:
    """Llama a Claude y al proveedor; no toca la base de datos."""
    prompt_json = build_visual_prompt(card, theme)
    fallback_provider = get_fallback_provider()
    attempts = [(provider, "full"), (provider, "fallback")]
    if fallback_provider is not None:
        attempts.append((fallback_provider, "fallback"))
    failures = []
    last_error = None
    for attempt_provider, prompt_type in attempts:
        try:
            image_bytes = attempt_provider.generate(prompt_json[f"{prompt_type}_prompt"])
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
    raise RuntimeError("; ".join(failures)) from last_error


def save_card_image(card, theme, provider, prompt_json, image_bytes) -> GeneratedImage:
    """Detecta formato, guarda el archivo con nombre único y crea el registro."""
    extension = _image_extension(image_bytes)
    cache_key = make_cache_key(card.oracle_id, theme, provider.name)
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
    return generated


def generate_card_image(card, theme, provider=None):
    provider = provider or get_provider()
    cache_key = make_cache_key(card.oracle_id, theme, provider.name)
    existing = GeneratedImage.objects.filter(cache_key=cache_key).first()
    if existing:
        return existing
    prompt_json, image_bytes = render_card_image(card, theme, provider)
    return save_card_image(card, theme, provider, prompt_json, image_bytes)


def run_job(job_id):
    connection.close()
    try:
        job = GenerationJob.objects.select_related("deck").get(pk=job_id)
        job.status = "running"
        job.started_at = timezone.now()
        job.save(update_fields=["status", "started_at", "updated_at"])
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
            job.save(update_fields=["completed_cards", "failed_cards", "error_message", "updated_at"])
        job.status = "failed" if cards and job.failed_cards == len(cards) else "done"
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "finished_at", "error_message", "updated_at"])
    except Exception as exc:
        logger.exception("El job %s falló antes de completar el procesamiento", job_id)
        GenerationJob.objects.filter(pk=job_id).update(
            status="failed", error_message=str(exc), finished_at=timezone.now(), updated_at=timezone.now()
        )
    finally:
        connection.close()
