from rest_framework import serializers

from generation.prompt_builder import make_cache_key
from generation.models import GeneratedImage
from generation.providers import get_provider_name

from .models import Card, Deck, DeckCard


class CardSerializer(serializers.ModelSerializer):
    class Meta:
        model = Card
        fields = [
            "id", "scryfall_id", "oracle_id", "name", "mana_cost", "cmc",
            "type_line", "oracle_text", "flavor_text", "colors", "color_identity",
            "rarity", "set_code", "collector_number", "original_art_url",
            "original_artist", "layout",
        ]


class DeckCardSerializer(serializers.ModelSerializer):
    card = CardSerializer(read_only=True)
    generated_image_url = serializers.SerializerMethodField()

    class Meta:
        model = DeckCard
        fields = ["id", "card", "raw_name", "quantity", "section", "resolution_error", "generated_image_url"]

    def get_generated_image_url(self, obj):
        if not obj.card_id:
            return None
        key = make_cache_key(obj.card.oracle_id, obj.deck.theme, get_provider_name())
        generated = GeneratedImage.objects.filter(cache_key=key).first()
        if not generated:
            return None
        request = self.context.get("request")
        return request.build_absolute_uri(generated.image.url) if request else generated.image.url


class DeckDetailSerializer(serializers.ModelSerializer):
    cards = DeckCardSerializer(many=True, read_only=True)

    class Meta:
        model = Deck
        fields = ["id", "name", "raw_list", "theme", "created_at", "cards"]


class DeckListSerializer(serializers.ModelSerializer):
    card_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Deck
        fields = ["id", "name", "theme", "created_at", "card_count"]


class DeckCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Deck
        fields = ["name", "raw_list", "theme"]
