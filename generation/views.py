import os
from decimal import Decimal

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from decks.models import Deck, DeckCard

from .models import GeneratedImage, GenerationJob
from .prompt_builder import make_cache_key
from .providers import get_provider, get_provider_name
from .runner import generate_card_image, start_job
from .serializers import GeneratedImageSerializer, GenerationJobSerializer


def _unique_cards(deck):
    unique = {}
    for card in deck.cards.filter(card__isnull=False).select_related("card"):
        unique.setdefault(card.card.oracle_id, card.card)
    return list(unique.values())


class GenerateDeckView(APIView):
    def post(self, request, deck_id):
        deck = get_object_or_404(Deck, pk=deck_id)
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
        theme = request.data.get("theme", deck_card.deck.theme)
        if not isinstance(theme, str) or not theme.strip():
            return Response({"theme": ["Debe ser un texto no vacío."]}, status=status.HTTP_400_BAD_REQUEST)
        provider = get_provider()
        key = make_cache_key(deck_card.card.oracle_id, theme, provider.name)
        old = GeneratedImage.objects.filter(cache_key=key).first()
        if old:
            old.image.delete(save=False)
            old.delete()
        try:
            generated = generate_card_image(deck_card.card, theme, provider)
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(GeneratedImageSerializer(generated, context={"request": request}).data)


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
