from rest_framework import serializers

from generation.prompt_builder import make_cache_key
from generation.models import GeneratedImage
from generation.serializers import GeneratedImageSummarySerializer, GenerationJobSerializer
from generation.jobs import expire_stale_jobs
from generation.providers import get_provider_name

from .models import Card, Deck, DeckCard


class CardSerializer(serializers.ModelSerializer):
    class Meta:
        model = Card
        fields = [
            "id", "scryfall_id", "oracle_id", "name", "mana_cost", "cmc",
            "type_line", "oracle_text", "flavor_text", "colors", "color_identity",
            "rarity", "set_code", "collector_number", "original_art_url",
            "original_artist", "layout", "power", "toughness", "loyalty", "keywords", "card_faces",
        ]


class DeckCardSerializer(serializers.ModelSerializer):
    card = CardSerializer(read_only=True)
    generated_image = serializers.SerializerMethodField()

    class Meta:
        model = DeckCard
        fields = ["id", "card", "raw_name", "quantity", "section", "resolution_error", "generated_image"]

    def get_generated_image(self, obj):
        if not obj.card_id:
            return None
        key = make_cache_key(obj.card.oracle_id, obj.deck.theme, get_provider_name())
        generated = self.context.get("generated_images", {}).get(key)
        if not generated:
            return None
        return GeneratedImageSummarySerializer(generated, context=self.context).data


class DeckDetailSerializer(serializers.ModelSerializer):
    cards = DeckCardSerializer(many=True, read_only=True)
    active_job = serializers.SerializerMethodField()
    card_count = serializers.SerializerMethodField()

    def get_active_job(self, obj):
        return GenerationJobSerializer(self.context["active_job"]).data if self.context.get("active_job") else None

    def get_card_count(self, obj):
        return sum(entry.quantity for entry in obj.cards.all())

    class Meta:
        model = Deck
        fields = ["id", "name", "raw_list", "theme", "created_at", "cards", "active_job", "card_count"]


class DeckListSerializer(serializers.ModelSerializer):
    card_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Deck
        fields = ["id", "name", "theme", "created_at", "card_count"]


class DeckCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Deck
        fields = ["name", "raw_list", "theme"]


def deck_detail_context(deck, request):
    expire_stale_jobs(deck)
    keys = [make_cache_key(entry.card.oracle_id, deck.theme, get_provider_name())
            for entry in deck.cards.all() if entry.card_id]
    return {
        "request": request,
        "generated_images": {image.cache_key: image for image in GeneratedImage.objects.filter(cache_key__in=keys)},
        "active_job": deck.jobs.filter(status__in=["pending", "running"]).order_by("-id").first(),
    }
