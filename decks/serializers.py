from django.db.models import prefetch_related_objects
from django.urls import reverse
from rest_framework import serializers

from generation.prompt_builder import card_cache_key
from generation.references import reference_hashes
from generation.wiki_images import reference_page_url
from generation.serializers import ReferenceImageSerializer
from generation.image_cache import generated_image_map
from generation.selection import assignment_map
from generation.theme_interview import deck_brief
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
    original_art_local_url = serializers.SerializerMethodField()

    def get_original_art_local_url(self, obj):
        if not obj.card_id or not obj.card.original_art_url:
            return None
        url = reverse('original-art', args=[obj.pk])
        request = self.context.get('request')
        return request.build_absolute_uri(url) if request else url

    card = CardSerializer(read_only=True)
    generated_image = serializers.SerializerMethodField()
    theme_assignment = serializers.SerializerMethodField()

    def get_theme_assignment(self, obj):
        assignment = self.context.get("assignments", {}).get(obj.card.oracle_id) if obj.card_id else None
        if assignment is None:
            return None
        return {**{key: getattr(assignment, key) for key in ("element", "category", "visual_brief", "edited", "needs_reference", "reference_wiki", "reference_wiki_alt", "reference_page", "reference_page_status", "auto_reference_status", "auto_reference_message")},
                "reference_page_url": reference_page_url(assignment.reference_wiki, assignment.reference_page),
                "references": ReferenceImageSerializer(assignment.references.all(), many=True, context=self.context).data}

    class Meta:
        model = DeckCard
        fields = ["id", "card", "raw_name", "quantity", "section", "resolution_error", "generated_image", "theme_assignment", "original_art_local_url"]

    def get_generated_image(self, obj):
        if not obj.card_id:
            return None
        key = card_cache_key(obj.card, obj.deck.theme, get_provider_name(), self.context.get("assignments", {}).get(obj.card.oracle_id), self.context.get("brief"), obj.deck.card_layout, reference_hashes(self.context.get("assignments", {}).get(obj.card.oracle_id)), original_art_mode=obj.deck.original_art_mode)
        generated = self.context.get("generated_images", {}).get(key)
        if not generated:
            return None
        return GeneratedImageSummarySerializer(generated, context=self.context).data


class DeckDetailSerializer(serializers.ModelSerializer):
    cards = DeckCardSerializer(many=True, read_only=True)
    auto_reference_counts = serializers.SerializerMethodField()

    def get_auto_reference_counts(self, obj):
        needed = [item for item in self.context.get("assignments", {}).values() if item.needs_reference]
        with_reference = sum(bool(list(item.references.all())) for item in needed)
        pending = sum(not item.auto_reference_status and not list(item.references.all()) for item in needed)
        return {"needed": len(needed), "with_reference": with_reference, "pending": pending}

    theme_brief = serializers.SerializerMethodField()
    theme_plan_stale = serializers.SerializerMethodField()
    has_theme_plan = serializers.SerializerMethodField()

    def get_theme_brief(self, obj):
        return self.context.get("brief", [])

    def get_theme_plan_stale(self, obj):
        if not obj.theme_answers_updated_at or not self.get_has_theme_plan(obj):
            return False
        latest = max((item.updated_at for item in self.context.get("assignments", {}).values()
                      if not item.edited), default=None)
        return latest is not None and obj.theme_answers_updated_at > latest

    def get_has_theme_plan(self, obj):
        assignments = self.context.get("assignments", {})
        return all(entry.card.oracle_id in assignments for entry in obj.cards.all() if entry.card_id)

    active_job = serializers.SerializerMethodField()
    card_count = serializers.SerializerMethodField()

    def get_active_job(self, obj):
        return GenerationJobSerializer(self.context["active_job"]).data if self.context.get("active_job") else None

    def get_card_count(self, obj):
        return sum(entry.quantity for entry in obj.cards.all())

    class Meta:
        model = Deck
        fields = ["id", "name", "raw_list", "theme", "card_layout", "created_at", "cards", "active_job", "card_count", "has_theme_plan",
                  "theme_questions", "theme_answers", "theme_brief", "theme_plan_stale", "auto_reference_counts",
                  "reference_wiki", "reference_wiki_alt", "reference_wiki_confirmed", "wiki_candidates", "auto_reference_count", "image_resolution", "original_art_mode"]


class DeckListSerializer(serializers.ModelSerializer):
    card_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Deck
        fields = ["id", "name", "theme", "card_layout", "created_at", "card_count"]


class DeckCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Deck
        fields = ["name", "raw_list", "theme", "card_layout"]


def deck_detail_context(deck, request, brief=None):
    prefetch_related_objects([deck], "cards__card")
    expire_stale_jobs(deck)
    assignments = assignment_map(deck)
    if brief is None:
        brief = deck_brief(deck)
    return {
        "request": request,
        "assignments": assignments,
        "brief": brief,
        "generated_images": generated_image_map([entry.card for entry in deck.cards.all() if entry.card_id],
            deck.theme, get_provider_name(), assignments, brief, deck.card_layout, deck.original_art_mode),
        "active_job": deck.jobs.filter(status__in=["pending", "running"]).order_by("-id").first(),
    }


class DeckLayoutSerializer(serializers.ModelSerializer):
    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("Envía card_layout, auto_reference_count, image_resolution u original_art_mode.")
        return attrs

    auto_reference_count = serializers.IntegerField(min_value=1, max_value=3, required=False)

    class Meta:
        model = Deck
        fields = ["card_layout", "auto_reference_count", "image_resolution", "original_art_mode"]
        extra_kwargs = {"card_layout": {"required": False}}
