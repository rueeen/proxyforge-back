import os
from datetime import timedelta

from django.utils import timezone

from .models import GenerationJob


def expire_stale_jobs(deck=None):
    now = timezone.now()
    jobs = GenerationJob.objects.filter(
        status__in=["pending", "running"],
        updated_at__lt=now - timedelta(minutes=int(os.getenv("STALE_JOB_MINUTES", "10"))),
    )
    if deck is not None:
        jobs = jobs.filter(deck=deck)
    return jobs.update(status="failed", error_message="Job interrumpido (el servidor se reinició).",
                       finished_at=now, updated_at=now)
