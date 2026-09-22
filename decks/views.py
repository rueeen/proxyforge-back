from django.db import transaction
from django.db.models import Sum
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from scryfall.client import resolve_cards

from .models import Deck, DeckCard
from .parser import parse_decklist
from .serializers import DeckCreateSerializer, DeckDetailSerializer, DeckListSerializer


class DeckCollectionView(APIView):
    def get(self, request):
        decks = Deck.objects.annotate(card_count=Sum("cards__quantity")).order_by("-created_at")
        return Response(DeckListSerializer(decks, many=True).data)

    def post(self, request):
        serializer = DeckCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        entries = parse_decklist(serializer.validated_data["raw_list"])
        if not entries:
            return Response({"raw_list": ["La lista no contiene cartas."]}, status=status.HTTP_400_BAD_REQUEST)
        resolution = resolve_cards(entries)
        with transaction.atomic():
            deck = serializer.save()
            DeckCard.objects.bulk_create([
                DeckCard(
                    deck=deck, card=card, raw_name=entry["raw_name"], quantity=entry["quantity"],
                    section=entry["section"], resolution_error="" if card else "Carta no encontrada en Scryfall",
                )
                for entry, card in zip(entries, resolution["resolved"])
            ])
        return Response(DeckDetailSerializer(deck, context={"request": request}).data, status=status.HTTP_201_CREATED)


class DeckDetailView(APIView):
    def get(self, request, deck_id):
        deck = get_object_or_404(Deck.objects.prefetch_related("cards__card"), pk=deck_id)
        return Response(DeckDetailSerializer(deck, context={"request": request}).data)

    def delete(self, request, deck_id):
        get_object_or_404(Deck, pk=deck_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
