from django.urls import path

from .views import DeckEstimateView, GenerateDeckView, JobDetailView, RegenerateCardView

urlpatterns = [
    path("decks/<int:deck_id>/generate/", GenerateDeckView.as_view(), name="generate-deck"),
    path("decks/<int:deck_id>/estimate/", DeckEstimateView.as_view(), name="deck-estimate"),
    path("jobs/<int:job_id>/", JobDetailView.as_view(), name="job-detail"),
    path("cards/<int:card_id>/regenerate/", RegenerateCardView.as_view(), name="regenerate-card"),
]
