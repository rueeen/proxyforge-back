from django.urls import path

from .views import DeckCollectionView, DeckDetailView

urlpatterns = [
    path("decks/", DeckCollectionView.as_view(), name="deck-collection"),
    path("decks/<int:deck_id>/", DeckDetailView.as_view(), name="deck-detail"),
]
