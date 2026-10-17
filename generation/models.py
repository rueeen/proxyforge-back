from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver

from decks.models import Card, Deck


class GenerationJob(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"), ("running", "Running"), ("done", "Done"),
        ("failed", "Failed"), ("cancelled", "Cancelled"),
    ]
    deck = models.ForeignKey(Deck, related_name="jobs", on_delete=models.CASCADE)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    updated_at = models.DateTimeField(auto_now=True)
    card_oracle_ids = models.JSONField(default=list)
    total_cards = models.PositiveIntegerField()
    completed_cards = models.PositiveIntegerField(default=0)
    failed_cards = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)


class GeneratedImage(models.Model):
    framing_offset_y = models.FloatField(default=0)
    framing_zoom = models.FloatField(default=100)
    framing_source = models.CharField(max_length=10, choices=[("default", "Por defecto"), ("auto", "Automático"), ("manual", "Manual")], default="default")
    face_center_y = models.FloatField(null=True, blank=True)
    cache_key = models.CharField(max_length=64, unique=True, db_index=True)
    card = models.ForeignKey(Card, on_delete=models.CASCADE)
    theme = models.TextField()
    visual_prompt = models.TextField()
    prompt_json = models.JSONField()
    image = models.ImageField(upload_to="generated/")
    provider = models.CharField(max_length=50)
    prompt_version = models.CharField(max_length=20)
    created_at = models.DateTimeField(auto_now_add=True)


class ThemeAssignment(models.Model):
    AUTO_STATUS = [("", "Sin intentar"), ("found", "Encontrada"), ("not_found", "No encontrada"),
                   ("rejected", "Descartada"), ("error", "Error"), ("blocked", "Descarga bloqueada")]
    needs_reference = models.BooleanField(default=False)
    reference_wiki = models.CharField(max_length=200, blank=True)
    reference_page = models.CharField(max_length=300, blank=True)
    auto_reference_status = models.CharField(max_length=20, choices=AUTO_STATUS, blank=True)
    auto_reference_message = models.CharField(max_length=300, blank=True)
    deck = models.ForeignKey(Deck, related_name="theme_assignments", on_delete=models.CASCADE)
    oracle_id = models.UUIDField()
    element = models.CharField(max_length=200)
    category = models.CharField(max_length=60)
    visual_brief = models.TextField()
    edited = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("deck", "oracle_id")]


class ReferenceImage(models.Model):
    origin = models.CharField(max_length=10, choices=[("manual", "Manual"), ("auto", "Automática"), ("wiki", "Elegida de la wiki")], default="manual")
    assignment = models.ForeignKey(ThemeAssignment, related_name="references", on_delete=models.CASCADE)
    image = models.ImageField(upload_to="references/")
    source_url = models.URLField(blank=True, max_length=1000)
    sha256 = models.CharField(max_length=64)
    position = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "id"]


@receiver(post_delete, sender=ReferenceImage)
def delete_reference_file(sender, instance, **kwargs):
    if instance.image:
        instance.image.delete(save=False)
