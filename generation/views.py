import os
from decimal import Decimal

from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from decks.models import Deck, DeckCard
from decks.serializers import DeckCardSerializer
from .jobs import expire_stale_jobs

from .models import GeneratedImage, GenerationJob
from .prompt_builder import make_cache_key
from .providers import get_provider, get_provider_name
from .runner import render_card_image, save_card_image, start_job
from .serializers import GenerationJobSerializer


def _unique_cards(deck):
    unique = {}
    for card in deck.cards.filter(card__isnull=False).select_related("card"):
        unique.setdefault(card.card.oracle_id, card.card)
    return list(unique.values())


class GenerateDeckView(APIView):
    def post(self, request, deck_id):
        deck = get_object_or_404(Deck, pk=deck_id)
        expire_stale_jobs(deck)
        if deck.jobs.filter(status__in=["pending", "running"]).exists():
            return Response({"detail": "Ya hay un job activo para este mazo."}, status=status.HTTP_409_CONFLICT)
        job = GenerationJob.objects.create(deck=deck, total_cards=len(_unique_cards(deck)))
        start_job(job)
        return Response(GenerationJobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class JobDetailView(APIView):
    def get(self, request, job_id):
        return Response(GenerationJobSerializer(get_object_or_404(GenerationJob, pk=job_id)).data)


class RegenerateCardView(APIView):
    def post(self, request, card_id):
        deck_card = get_object_or_404(DeckCard.objects.select_related("card", "deck"), pk=card_id)
        if not deck_card.card:
            return Response({"detail": "La carta no fue resuelta."}, status=status.HTTP_400_BAD_REQUEST)
        expire_stale_jobs(deck_card.deck)
        if deck_card.deck.jobs.filter(status__in=["pending", "running"]).exists():
            return Response({"detail": "Hay una generación en curso para este mazo."}, status=status.HTTP_409_CONFLICT)
        theme = deck_card.deck.theme
        try:
            provider = get_provider()
            key = make_cache_key(deck_card.card.oracle_id, theme, provider.name)
            prompt_json, image_bytes = render_card_image(deck_card.card, theme, provider)
            with transaction.atomic():
                old = GeneratedImage.objects.select_for_update().filter(cache_key=key).first()
                old_file = old.image if old else None
                if old:
                    old.delete()
                generated = save_card_image(deck_card.card, theme, provider, prompt_json, image_bytes)
                if old_file:
                    transaction.on_commit(lambda: old_file.delete(save=False))
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(DeckCardSerializer(deck_card, context={"request": request, "generated_images": {key: generated}}).data)


class DeckEstimateView(APIView):
    def get(self, request, deck_id):
        deck = get_object_or_404(Deck, pk=deck_id)
        cards = _unique_cards(deck)
        provider_name = get_provider_name()
        keys = [make_cache_key(card.oracle_id, deck.theme, provider_name) for card in cards]
        cached = GeneratedImage.objects.filter(cache_key__in=keys).count()
        to_generate = len(cards) - cached
        cost = Decimal(os.getenv("COST_PER_IMAGE_USD", "0.04")) * to_generate
        return Response({
            "unique_cards": len(cards), "cached": cached, "to_generate": to_generate,
            "estimated_cost_usd": float(cost.quantize(Decimal("0.01"))),
        })
