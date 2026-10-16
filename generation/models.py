from django.db import models

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
    deck = models.ForeignKey(Deck, related_name="theme_assignments", on_delete=models.CASCADE)
    oracle_id = models.UUIDField()
    element = models.CharField(max_length=200)
    category = models.CharField(max_length=60)
    visual_brief = models.TextField()
    edited = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("deck", "oracle_id")]
