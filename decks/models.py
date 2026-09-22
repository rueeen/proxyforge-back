from django.db import models


class Card(models.Model):
    scryfall_id = models.UUIDField(unique=True)
    oracle_id = models.UUIDField(db_index=True)
    name = models.CharField(max_length=300)
    mana_cost = models.CharField(max_length=100, blank=True)
    cmc = models.FloatField()
    type_line = models.CharField(max_length=300)
    oracle_text = models.TextField(blank=True)
    flavor_text = models.TextField(blank=True)
    colors = models.JSONField(default=list)
    color_identity = models.JSONField(default=list)
    rarity = models.CharField(max_length=20)
    set_code = models.CharField(max_length=10)
    collector_number = models.CharField(max_length=20)
    original_art_url = models.URLField(blank=True)
    original_artist = models.CharField(max_length=200, blank=True)
    layout = models.CharField(max_length=50)
    scryfall_payload = models.JSONField()
    cached_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Deck(models.Model):
    name = models.CharField(max_length=200)
    raw_list = models.TextField()
    theme = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class DeckCard(models.Model):
    MAIN = "main"
    SIDEBOARD = "sideboard"
    COMMANDER = "commander"
    SECTION_CHOICES = [(MAIN, "Main"), (SIDEBOARD, "Sideboard"), (COMMANDER, "Commander")]

    deck = models.ForeignKey(Deck, related_name="cards", on_delete=models.CASCADE)
    card = models.ForeignKey(Card, null=True, blank=True, on_delete=models.SET_NULL)
    raw_name = models.CharField(max_length=300)
    quantity = models.PositiveIntegerField()
    section = models.CharField(max_length=20, choices=SECTION_CHOICES)
    resolution_error = models.CharField(max_length=300, blank=True)
